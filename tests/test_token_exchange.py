"""The manager's own exchange task: shutdown waits for it, removal waits for it, a cancelled caller logs nothing."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from .conftest import ISSUER, SUB, TOKEN_ENDPOINT, entry_data, make_jwt
from .fake_server import FakeServer, add_v1_routes, serving_amzettel
from .test_lifecycle import RotatingTokenEndpoint


def _errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


async def test_shutdown_waits_for_a_running_exchange(hass: HomeAssistant, socket_enabled, no_realtime, caplog) -> None:
    """Real local server: R0 is redeemed, R1 answers after 200 ms; HA stops meanwhile — R1 is stored."""
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from custom_components.amzettel.const import CONF_SERVER, CONF_TOKEN_ENDPOINT

    redeemed: list[str] = []
    arrived = asyncio.Event()

    async def token(request: web.Request) -> web.Response:
        form = await request.post()
        redeemed.append(str(form["refresh_token"]))
        arrived.set()
        await asyncio.sleep(0.2)
        return web.json_response({"access_token": make_jwt(n=1), "refresh_token": "refresh-r1", "expires_in": 900})

    app = web.Application()
    add_v1_routes(app)
    app.router.add_post("/token", token)
    server = TestServer(app)
    await server.start_server()
    try:
        base = str(server.make_url("")).rstrip("/")
        entry = MockConfigEntry(
            domain="amzettel",
            unique_id=SUB,
            data=entry_data(**{CONF_SERVER: base, CONF_TOKEN_ENDPOINT: f"{base}/token"}),
        )
        with serving_amzettel(base):
            entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

            refresh = asyncio.get_running_loop().create_task(
                entry.runtime_data.tokens.async_get_access_token(force_refresh=True)
            )
            await asyncio.wait_for(arrived.wait(), 5)
            await asyncio.sleep(0.003)
            await hass.async_stop()
        assert redeemed == ["refresh-1"]
        assert entry.data["refresh_token"] == "refresh-r1"  # stored before the final write
        assert refresh.done() and not refresh.exception()
    finally:
        await server.close()
    assert not _errors(caplog)


async def test_removal_waits_for_the_exchange_and_revokes_the_current_token(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, no_realtime, caplog
) -> None:
    """Remove while R0's exchange runs: wait for it, revoke R1 (not the spent R0), no write into the gone entry."""
    import custom_components.amzettel as integration

    FakeServer(aioclient_mock)
    revoked: list[str] = []

    async def revoke(method, url, data):
        revoked.append(data["token"])
        return AiohttpClientMockResponse(method, url, json={})

    aioclient_mock.get(
        f"{ISSUER}/.well-known/openid-configuration", json={"revocation_endpoint": f"{ISSUER}/connect/revoke"}
    )
    aioclient_mock.post(f"{ISSUER}/connect/revoke", side_effect=revoke)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    endpoint = RotatingTokenEndpoint(aioclient_mock)
    endpoint.gate = asyncio.Event()
    refresh = asyncio.get_running_loop().create_task(
        config_entry.runtime_data.tokens.async_get_access_token(force_refresh=True)
    )
    await asyncio.sleep(0)
    real_unload = integration.async_unload_entry

    async def unload(hass, entry):
        result = await real_unload(hass, entry)
        hass.loop.call_soon(endpoint.gate.set)  # the answer arrives while the removal goes on
        return result

    with patch.object(integration, "async_unload_entry", unload):
        await hass.config_entries.async_remove(config_entry.entry_id)
    await refresh
    assert endpoint.redeemed == ["refresh-1"]
    assert revoked == ["refresh-r1"]
    assert hass.config_entries.async_get_entry(config_entry.entry_id) is None
    assert not _errors(caplog)


async def test_cancelled_caller_then_failing_exchange_logs_no_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, caplog
) -> None:
    """The caller is cancelled, the exchange then fails with 503: nothing is logged as an error."""
    import time

    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from custom_components.amzettel.auth import MemoryTokenStore, TokenManager, TokenSet

    gate = asyncio.Event()

    async def unavailable(method, url, data):
        await gate.wait()
        return AiohttpClientMockResponse(method, url, status=503, json={})

    aioclient_mock.post(TOKEN_ENDPOINT, side_effect=unavailable)
    store = MemoryTokenStore(TOKEN_ENDPOINT, TokenSet("t", "r", time.time() - 10))
    manager = TokenManager(async_get_clientsession(hass), store)
    caller = asyncio.get_running_loop().create_task(manager.async_get_access_token())
    await asyncio.sleep(0)
    caller.cancel()
    await asyncio.sleep(0)
    gate.set()
    await manager.async_wait_idle(5)
    for _ in range(5):  # let done callbacks and exception handlers run
        await asyncio.sleep(0)
    assert caller.cancelled()
    assert not _errors(caplog)
