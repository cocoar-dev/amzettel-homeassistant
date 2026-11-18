"""Life cycle of the realtime task: runs with the entry, stops on unload, refusal → reauth only when the
credentials are gone."""

from __future__ import annotations

import asyncio
import re
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from .conftest import TOKEN_ENDPOINT, make_jwt
from .fake_server import FEED, FakeServer, add_v1_routes, serving_amzettel


async def _until(condition, timeout: float = 2.0) -> None:
    """Background tasks are not awaited by async_block_till_done; wait for an observable effect."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def _reauth_flows(hass: HomeAssistant) -> list:
    return [f for f in hass.config_entries.flow.async_progress() if f["context"]["source"] == SOURCE_REAUTH]


async def test_realtime_runs_and_unload_stops_it(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry
) -> None:
    FakeServer(aioclient_mock)
    connected = asyncio.Event()

    async def open_connection(self, refresh: bool) -> str:
        connected.set()
        await asyncio.Event().wait()  # an open stream
        return "retry"

    with patch("custom_components.amzettel.feed_stream.FeedStream._once", open_connection):
        config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await asyncio.wait_for(connected.wait(), 2)
        realtime = config_entry.runtime_data.realtime
        assert realtime.task is not None
        assert await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED
    assert realtime.task is None and not realtime.connected


async def test_refused_stream_with_a_refreshed_token_starts_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry
) -> None:
    """The stream refuses the token, and the freshly refreshed one too: the grant is gone."""
    FakeServer(aioclient_mock)
    tokens = []

    async def changes(method, url, data):
        tokens.append(aioclient_mock.mock_calls[-1][3]["Authorization"])
        return AiohttpClientMockResponse(method, url, status=401, json={})

    aioclient_mock.get(re.compile(f"^{re.escape(FEED)}/changes"), side_effect=changes)
    aioclient_mock.post(TOKEN_ENDPOINT, json={"access_token": make_jwt(n=2), "expires_in": 900})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await _until(lambda: bool(_reauth_flows(hass)))
    assert len(tokens) == 2 and tokens[0] != tokens[1]  # refreshed once before giving up
    coordinator = config_entry.runtime_data.coordinator
    assert coordinator.revoked and not coordinator.data.authoritative and not coordinator.data.lists
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_refused_stream_with_valid_credentials_starts_no_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry
) -> None:
    """A refusal of the grant's rights (403) is no sign-in problem: hide the data, no reauth."""
    FakeServer(aioclient_mock)
    answered = []

    async def changes(method, url, data):
        answered.append(url)
        return AiohttpClientMockResponse(method, url, status=403, json={"title": "integration_not_permitted"})

    aioclient_mock.get(re.compile(f"^{re.escape(FEED)}/changes"), side_effect=changes)
    aioclient_mock.post(TOKEN_ENDPOINT, status=500)  # never asked: the token is valid
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await _until(lambda: bool(answered))
    await hass.async_block_till_done()
    coordinator = config_entry.runtime_data.coordinator
    await _until(lambda: not coordinator.data.authoritative)
    assert not _reauth_flows(hass)
    assert not coordinator.revoked
    assert config_entry.runtime_data.realtime.last_error == "403"
    assert config_entry.state is ConfigEntryState.LOADED
    await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_unload_ends_socket_realtime_task_and_cache_tasks(hass: HomeAssistant, socket_enabled) -> None:
    """The real thing: the SSE stream against a local aiohttp server that also serves the snapshot;
    after unload the connection, the realtime task and the calendar cache tasks are gone."""
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.amzettel.const import CONF_SERVER

    from .conftest import SUB, entry_data

    streaming = asyncio.Event()
    socket_closed = asyncio.Event()
    seen: list[tuple[str, str, list[str]]] = []

    async def changes(request: web.Request) -> web.StreamResponse:
        seen.append((request.headers["Authorization"], request.headers["Last-Event-ID"], request.query.getall("area")))
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b'event: checkpoint\nid: cursor-2\ndata: {"Version":1}\n\n')
        streaming.set()
        try:
            while True:  # heartbeats until the client closes (the write fails or the handler is cancelled)
                await asyncio.sleep(0.02)
                await response.write(b": ping\n\n")
        except ConnectionError:
            pass
        finally:
            socket_closed.set()
        return response

    app = web.Application()
    add_v1_routes(app, changes)
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    try:
        with serving_amzettel(base):
            entry = MockConfigEntry(domain="amzettel", unique_id=SUB, data=entry_data(**{CONF_SERVER: base}))
            entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(entry.entry_id)
            await asyncio.wait_for(streaming.wait(), 5)
            await hass.async_block_till_done()
            realtime = entry.runtime_data.realtime
            coordinator = entry.runtime_data.coordinator
            await _until(lambda: coordinator.cursor == "cursor-2")  # the checkpoint was taken
            calendars = [
                e for e in hass.data["entity_components"]["calendar"].entities if e.platform.config_entry is entry
            ]
            assert calendars and realtime.connected and realtime.ready_count == 1
            assert seen == [(seen[0][0], "cursor-1", ["Calendar", "Lists", "MealPlans"])]
            assert seen[0][0].startswith("Bearer ")
            assert entry._background_tasks  # the realtime task runs

            assert await hass.config_entries.async_unload(entry.entry_id)
            await asyncio.wait_for(socket_closed.wait(), 5)
            await hass.async_block_till_done()

            assert entry.state is ConfigEntryState.NOT_LOADED
            assert realtime.task is None and not realtime.connected
            assert not entry._background_tasks  # realtime task gone
            assert not entry._tasks  # cache tasks gone
            assert all(c._cache_task is None or c._cache_task.done() for c in calendars)
    finally:
        await server.close()


async def test_reauth_reloads_once_through_the_listener_without_warning(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, no_realtime, caplog
) -> None:
    """HA 2026.10 warns when a flow reloads an entry that has an update listener; the listener does it."""
    import time
    from unittest.mock import AsyncMock

    from custom_components.amzettel.auth import AuthEndpoints, DeviceCode, TokenSet

    from .conftest import DEVICE_ENDPOINT, ISSUER

    FakeServer(aioclient_mock)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    new = TokenSet(make_jwt(reauth=True), "refresh-reauth", time.time() + 900)
    device = DeviceCode("dev", "CODE", f"{ISSUER}/device", None, 600, 1)
    real_reload = hass.config_entries.async_reload
    with (
        patch(
            "custom_components.amzettel.config_flow.async_discover",
            AsyncMock(return_value=AuthEndpoints(ISSUER, DEVICE_ENDPOINT, TOKEN_ENDPOINT)),
        ),
        patch("custom_components.amzettel.config_flow.async_request_device_code", AsyncMock(return_value=device)),
        patch("custom_components.amzettel.config_flow.async_poll_device_token", AsyncMock(return_value=new)),
        patch("custom_components.amzettel.api.AmZettelApi.async_get_me", AsyncMock(return_value={"Id": "u"})),
        patch("custom_components.amzettel.api.AmZettelApi.async_register_self", AsyncMock(return_value={})),
        patch.object(hass.config_entries, "async_reload", AsyncMock(side_effect=real_reload)) as reload,
    ):
        result = await config_entry.start_reauth_flow(hass)
        flow_id = result["flow_id"]
        await hass.config_entries.flow.async_configure(flow_id, {})
        await hass.async_block_till_done()  # the device poll ends; HA follows the progress to the end
    assert not [f for f in hass.config_entries.flow.async_progress() if f["flow_id"] == flow_id]
    assert reload.await_count == 1
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.data["refresh_token"] == "refresh-reauth"
    assert config_entry.runtime_data.tokens.tokens.refresh_token == "refresh-reauth"  # the new setup uses it
    assert "should use it for scheduling a reload" not in caplog.text
    await hass.config_entries.async_unload(config_entry.entry_id)


async def _setup_loaded(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry) -> None:
    FakeServer(aioclient_mock)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


def _reauth_patches(hass: HomeAssistant, new):
    """The device flow of a reauth, mocked up to the tokens it yields."""
    from contextlib import ExitStack
    from unittest.mock import AsyncMock

    from custom_components.amzettel.auth import AuthEndpoints, DeviceCode

    from .conftest import DEVICE_ENDPOINT, ISSUER

    device = DeviceCode("dev", "CODE", f"{ISSUER}/device", None, 600, 1)
    stack = ExitStack()
    flow = "custom_components.amzettel.config_flow."
    endpoints = AuthEndpoints(ISSUER, DEVICE_ENDPOINT, TOKEN_ENDPOINT)
    stack.enter_context(patch(flow + "async_discover", AsyncMock(return_value=endpoints)))
    stack.enter_context(patch(flow + "async_request_device_code", AsyncMock(return_value=device)))
    stack.enter_context(patch(flow + "async_poll_device_token", AsyncMock(return_value=new)))
    api = "custom_components.amzettel.api.AmZettelApi."
    stack.enter_context(patch(api + "async_get_me", AsyncMock(return_value={"Id": "u"})))
    stack.enter_context(patch(api + "async_register_self", AsyncMock(return_value={})))
    return stack


async def _reauth(hass: HomeAssistant, config_entry) -> None:
    result = await config_entry.start_reauth_flow(hass)
    await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()


async def test_options_change_and_reauth_during_its_unload_reload_once(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, no_realtime
) -> None:
    """A reauth landing while the options reload still unloads is read by that reload's setup."""
    import time
    from unittest.mock import AsyncMock

    import custom_components.amzettel as integration
    from custom_components.amzettel.auth import TokenSet


    await _setup_loaded(hass, aioclient_mock, config_entry)
    new = TokenSet(make_jwt(reauth=True), "refresh-reauth", time.time() + 900)
    real_unload = integration.async_unload_entry
    real_reload = hass.config_entries.async_reload
    reauthed = False

    async def unload_with_a_reauth_meanwhile(hass, entry):
        nonlocal reauthed
        if not reauthed:
            reauthed = True
            with _reauth_patches(hass, new):
                result = await entry.start_reauth_flow(hass)
                await hass.config_entries.flow.async_configure(result["flow_id"], {})
                for _ in range(20):  # let the device poll finish and the flow write entry.data
                    await asyncio.sleep(0)
            assert entry.data["refresh_token"] == "refresh-reauth"
        return await real_unload(hass, entry)

    options = {"lists": [], "auto_add_new": True, "known": []}
    with (
        patch.object(integration, "async_unload_entry", unload_with_a_reauth_meanwhile),
        patch.object(hass.config_entries, "async_reload", AsyncMock(side_effect=real_reload)) as reload,
    ):
        hass.config_entries.async_update_entry(config_entry, options=options)
        await hass.async_block_till_done()
    assert reauthed
    assert reload.await_count == 1
    assert config_entry.state is ConfigEntryState.LOADED
    assert dict(config_entry.options) == options
    assert config_entry.runtime_data.tokens.tokens.refresh_token == "refresh-reauth"
    await hass.config_entries.async_unload(config_entry.entry_id)


# ── one token manager across reloads (rotating refresh tokens) ──────────────────────────────


class RotatingTokenEndpoint:
    """modgud's refresh grant: every refresh token works ONCE (redeemed when the request arrives).

    A redeemed token answers `invalid_grant`, so a second redemption shows. While `gate` is set, the
    answer of the FIRST request waits for it (the request itself has already redeemed the token).
    """

    def __init__(self, aioclient_mock: AiohttpClientMocker, valid: str = "refresh-1") -> None:
        self.valid = {valid}
        self.redeemed: list[str] = []
        self.gate: asyncio.Event | None = None
        self._count = 0
        aioclient_mock.post(TOKEN_ENDPOINT, side_effect=self._answer)

    async def _answer(self, method, url, data):

        token = data["refresh_token"]
        self.redeemed.append(token)
        if token not in self.valid:
            return AiohttpClientMockResponse(method, url, status=400, json={"error": "invalid_grant"})
        self.valid.discard(token)
        self._count += 1
        new = f"refresh-r{self._count}"
        self.valid.add(new)
        if self.gate is not None and self._count == 1:
            await self.gate.wait()
        return AiohttpClientMockResponse(
            method, url, json={"access_token": make_jwt(n=self._count), "refresh_token": new, "expires_in": 900}
        )


async def test_old_answer_during_the_next_setup_reaches_the_active_manager(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, no_realtime
) -> None:
    """(a) The old refresh answers while the next setup waits for its snapshot: the manager uses R1."""
    endpoint = RotatingTokenEndpoint(aioclient_mock)
    endpoint.gate = asyncio.Event()
    calls = {"snapshots": 0}
    old_refresh: asyncio.Task | None = None

    async def snapshot_answer(method, url, data):
        calls["snapshots"] += 1
        if calls["snapshots"] == 2:  # the next setup's snapshot: now the old answer arrives
            endpoint.gate.set()
            await old_refresh
        return await server._snapshot(method, url, data)

    aioclient_mock.post(f"{FEED}/snapshots", side_effect=snapshot_answer)  # registered first: it answers
    server = FakeServer(aioclient_mock)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    old_refresh = hass.async_create_task(config_entry.runtime_data.tokens.async_get_access_token(force_refresh=True))
    await asyncio.sleep(0)
    hass.config_entries.async_update_entry(config_entry, options={"lists": [], "auto_add_new": True, "known": []})
    await hass.async_block_till_done()
    assert old_refresh.done() and not old_refresh.exception()
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.data["refresh_token"] == "refresh-r1"

    await config_entry.runtime_data.tokens.async_get_access_token(force_refresh=True)  # the NEXT refresh
    assert config_entry.data["refresh_token"] == "refresh-r2"
    assert endpoint.redeemed == ["refresh-1", "refresh-r1"]  # each token once, no invalid_grant
    await hass.config_entries.async_unload(config_entry.entry_id)


def _almost_expired():
    """The stored access token (valid one hour) looks 30 s before expiry: the next use refreshes."""
    import time
    from types import SimpleNamespace

    from custom_components.amzettel import auth

    real = time.time
    return patch.object(auth, "time", SimpleNamespace(time=lambda: real() + 3600 - 30))


async def _options_reload_while_refreshing(
    hass: HomeAssistant, entry, endpoint: RotatingTokenEndpoint, *, cancel_caller: bool
) -> asyncio.Task:
    """A proactive refresh with R0 hangs; an options reload builds the next setup meanwhile."""
    import custom_components.amzettel as integration

    endpoint.gate = asyncio.Event()
    refresh = hass.async_create_task(entry.runtime_data.tokens.async_get_access_token())
    await asyncio.sleep(0)
    assert endpoint.redeemed == ["refresh-1"]
    real_unload = integration.async_unload_entry

    async def unload(hass, entry):
        if cancel_caller:
            refresh.cancel()  # like the coordinator's pending work, cancelled by the unload
        result = await real_unload(hass, entry)
        hass.loop.call_soon(endpoint.gate.set)  # the answer arrives while the next setup starts
        return result

    with patch.object(integration, "async_unload_entry", unload):
        hass.config_entries.async_update_entry(entry, options={"lists": [], "auto_add_new": True, "known": []})
        await hass.async_block_till_done()
    return refresh


async def test_reload_during_a_refresh_redeems_its_token_once(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, no_realtime
) -> None:
    """(b) The next setup does not redeem R0 again; it waits for the running refresh and uses R1."""
    await _setup_loaded(hass, aioclient_mock, config_entry)
    endpoint = RotatingTokenEndpoint(aioclient_mock)
    with _almost_expired():
        refresh = await _options_reload_while_refreshing(hass, config_entry, endpoint, cancel_caller=False)
    assert refresh.done() and not refresh.exception()
    assert config_entry.state is ConfigEntryState.LOADED
    assert endpoint.redeemed == ["refresh-1"]  # exactly one redemption of R0
    assert config_entry.data["refresh_token"] == "refresh-r1"
    assert config_entry.runtime_data.tokens.tokens.refresh_token == "refresh-r1"
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_refresh_whose_caller_the_unload_cancels_still_hands_over(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, no_realtime
) -> None:
    """Options reload with a refresh running: its caller is cancelled, the exchange is not — R0 once."""
    await _setup_loaded(hass, aioclient_mock, config_entry)
    endpoint = RotatingTokenEndpoint(aioclient_mock)
    with _almost_expired():
        refresh = await _options_reload_while_refreshing(hass, config_entry, endpoint, cancel_caller=True)
    assert refresh.cancelled()
    assert config_entry.state is ConfigEntryState.LOADED
    assert endpoint.redeemed == ["refresh-1"]
    assert config_entry.data["refresh_token"] == "refresh-r1"
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_reauth_while_an_old_refresh_hangs_keeps_the_new_credentials(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, no_realtime
) -> None:
    """The old refresh answers only after the reauth: entry.data and the manager keep the reauth's tokens."""
    import time

    from custom_components.amzettel.auth import TokenSet


    await _setup_loaded(hass, aioclient_mock, config_entry)
    endpoint = RotatingTokenEndpoint(aioclient_mock)
    endpoint.gate = asyncio.Event()
    # A plain task: HA's block_till_done must not wait for the answer that comes only after the reauth.
    old_refresh = asyncio.get_running_loop().create_task(
        config_entry.runtime_data.tokens.async_get_access_token(force_refresh=True)
    )
    await asyncio.sleep(0)
    new = TokenSet(make_jwt(reauth=True), "refresh-reauth", time.time() + 900)
    with _reauth_patches(hass, new):
        await _reauth(hass, config_entry)
    endpoint.gate.set()
    await old_refresh
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.data["refresh_token"] == "refresh-reauth"
    assert config_entry.runtime_data.tokens.tokens.refresh_token == "refresh-reauth"
    assert await config_entry.runtime_data.tokens.async_get_access_token() == new.access_token
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_reauth_replaces_the_access_token_at_once(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, no_realtime
) -> None:
    """Between the reauth and its reload the manager hands out the NEW access token, not the cached old one."""
    import time
    from unittest.mock import MagicMock

    from custom_components.amzettel.auth import TokenSet


    await _setup_loaded(hass, aioclient_mock, config_entry)
    manager = config_entry.runtime_data.tokens
    new = TokenSet(make_jwt(reauth=True), "refresh-reauth", time.time() + 900)
    with (
        _reauth_patches(hass, new),
        patch.object(hass.config_entries, "async_schedule_reload", MagicMock()) as reload,
    ):
        await _reauth(hass, config_entry)
    reload.assert_called_once()  # the reload is due, but has not run
    assert await manager.async_get_access_token() == new.access_token
    await hass.config_entries.async_unload(config_entry.entry_id)


# ── setup on a server without v1 ───────────────────────────────────────────────────────────


async def test_missing_v1_api_retries_setup_without_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, no_realtime
) -> None:
    """`capabilities` answers 404: not ready (HA retries), no sign-in problem, no legacy fallback."""
    server = FakeServer(aioclient_mock)
    server.capabilities_error = (404, "not_found")
    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert "v1 API is not available on this server" in config_entry.reason  # the translated explanation
    assert not _reauth_flows(hass)
    assert server.snapshots == 0
    assert all("/api/integrations/v1/" in str(c[1]) or str(c[1]).endswith("/api/auth/me")
               for c in aioclient_mock.mock_calls)


async def test_an_entry_on_another_server_is_not_set_up(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .conftest import SUB, entry_data

    entry = MockConfigEntry(domain="amzettel", unique_id=SUB, data=entry_data(server="https://app-dev.amzettel.at"))
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert "Only the official amZettel server" in entry.reason
    assert aioclient_mock.mock_calls == []  # nothing asked, not even the profile
    assert not _reauth_flows(hass)
    assert entry.data["refresh_token"] == "refresh-1"  # kept for a later build that supports the server
