"""Public v1 contract, ordered cursor consumption and preconditioned mutations."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.amzettel.const import DEFAULT_SERVER, DOMAIN
from custom_components.amzettel.coordinator import AmZettelCoordinator, AmZettelData
from custom_components.amzettel.exceptions import ApiError, AuthFailed, CannotConnect
from custom_components.amzettel.feed_api import (
    PREFIX,
    FeedApi,
    FeedHttpError,
    FeedProtocolError,
    Snapshot,
    map_object,
)
from custom_components.amzettel.feed_stream import FeedStream, frames
from custom_components.amzettel.naming import display_name
from custom_components.amzettel.writes import async_item_mutation

from .conftest import SCOPES, SUB, entry_data

LIST = "00000000-0000-0000-0000-000000000001"
CAL = "00000000-0000-0000-0000-000000000002"
PLAN = "00000000-0000-0000-0000-000000000003"
ITEM = "00000000-0000-0000-0000-000000000004"
ETAG = '"opaque-tag"'
LIST_OBJ = {
    "Area": "Lists", "Kind": "List", "Id": LIST, "ETag": ETAG,
    "Data": {
        "Name": "Shopping", "Type": "Shopping", "IsOwner": False, "OwnerName": "Anna",
        "Items": [], "RecentPurchases": [{"CheckId": ITEM, "ByUserId": SUB}],
    },
}
CAL_OBJ = {
    "Area": "Calendar", "Kind": "Calendar", "Id": CAL, "ETag": ETAG,
    "Data": {
        "Name": "Test calendar", "IsOwner": True, "EventCategories": [], "Persons": [],
        "Events": [{"Id": ITEM, "Recurrence": {"Rule": "FREQ=DAILY", "RDates": ["A"], "ExDates": ["B"]}}],
    },
}
PLAN_OBJ = {
    "Area": "MealPlans", "Kind": "MealPlan", "Id": PLAN, "ETag": ETAG,
    "Data": {
        "Name": "Meals", "IsOwner": False, "Slots": [],
        "Entries": [{"Id": ITEM, "PlannedTime": "18:00", "PlannedZone": "Europe/Vienna", "ShowInCalendar": False}],
    },
}
CAPS = {
    "Version": 1,
    "Areas": [
        {"Area": area, "Read": True, "Write": area == "Lists", "Operations": ["AddItem", "Buy"]}
        for area in ("Lists", "Calendar", "Recipes", "MealPlans")
    ],
}


def snapshot() -> Snapshot:
    return Snapshot(
        ("Calendar", "Lists", "MealPlans"), {"Lists": frozenset(("AddItem", "Buy"))},
        {LIST: map_object(LIST_OBJ, ("Lists",)), CAL: map_object(CAL_OBJ, ("Calendar",))},
        {PLAN: map_object(PLAN_OBJ, ("MealPlans",))}, "snapshot",
    )


def test_mapping_and_display_names() -> None:
    data = snapshot()
    assert data.lists[CAL]["Type"] == "Events"
    assert data.lists[CAL]["Items"][0]["Recurrence"] == {
        "Rule": "FREQ=DAILY", "RDateLines": ["A"], "ExDateLines": ["B"],
    }
    assert data.lists[LIST]["RecentPurchases"][0]["ByUserId"] == SUB
    assert data.plans[PLAN]["Entries"][0]["PlannedZone"] == "Europe/Vienna"
    assert display_name("X", data.lists[LIST], None, "de") == "X (von Anna)"
    assert display_name("X", data.plans[PLAN], None, "de") == "X (geteilt)"
    assert display_name("X", data.lists[CAL], None, "de") == "X"
    with pytest.raises(FeedProtocolError):
        map_object({**CAL_OBJ, "Id": "../capabilities"}, ("Calendar",))
    with pytest.raises(FeedProtocolError):
        map_object(CAL_OBJ, ("Lists",))


@pytest.mark.parametrize("error", [None, "cursor", "expired", "duplicate"])
async def test_snapshot_pages(error: str | None) -> None:
    api = FeedApi(MagicMock(), DEFAULT_SERVER, MagicMock())
    first = {"Version": 1, "Cursor": "abc", "Objects": [CAL_OBJ], "Complete": False, "NextPageToken": "next/token"}
    second = {
        "Version": 1, "Cursor": "mismatch" if error == "cursor" else "abc", "Complete": True,
        "Objects": [CAL_OBJ, PLAN_OBJ] if error == "duplicate" else [LIST_OBJ, PLAN_OBJ],
    }
    api.request = AsyncMock(side_effect=[
        CAPS, first, FeedHttpError(410, "snapshot_expired", None) if error == "expired" else second, None,
    ])
    if error:
        with pytest.raises(FeedHttpError if error == "expired" else FeedProtocolError):
            await api.async_snapshot()
    else:
        data = await api.async_snapshot()
        assert data.cursor == "abc" and set(data.lists) == {LIST, CAL} and set(data.plans) == {PLAN}
    assert api.request.await_args_list[1].args == ("POST", "/snapshots", {"Areas": ["Calendar", "Lists", "MealPlans"]})
    assert api.request.await_args_list[2].args == ("GET", "/snapshots/next%2Ftoken")
    api.request.assert_awaited_with("DELETE", "/snapshots/next%2Ftoken")


async def test_complete_first_page_does_not_release() -> None:
    api = FeedApi(MagicMock(), DEFAULT_SERVER, MagicMock())
    api.request = AsyncMock(side_effect=[CAPS, {
        "Version": 1, "Cursor": "abc", "Objects": [CAL_OBJ], "Complete": True,
    }])
    assert (await api.async_snapshot()).cursor == "abc"
    assert api.request.await_count == 2


@pytest.mark.parametrize("failure", [CannotConnect(), AuthFailed()])
async def test_release_failure_does_not_lose_snapshot_unless_auth_is_gone(failure) -> None:
    api = FeedApi(MagicMock(), DEFAULT_SERVER, MagicMock())
    api.request = AsyncMock(side_effect=[
        CAPS,
        {"Version": 1, "Cursor": "abc", "Objects": [CAL_OBJ], "Complete": False, "NextPageToken": "first"},
        {"Version": 1, "Cursor": "abc", "Objects": [LIST_OBJ], "Complete": False, "NextPageToken": "last"},
        {"Version": 1, "Cursor": "abc", "Objects": [PLAN_OBJ], "Complete": True},
        failure,
    ])
    if isinstance(failure, AuthFailed):
        with pytest.raises(AuthFailed):
            await api.async_snapshot()
    else:
        assert (await api.async_snapshot()).cursor == "abc"
    api.request.assert_awaited_with("DELETE", "/snapshots/first")


async def test_no_read_areas_does_not_request_unrestricted_snapshot() -> None:
    api = FeedApi(MagicMock(), DEFAULT_SERVER, MagicMock())
    api.request = AsyncMock(return_value={"Version": 1, "Areas": [CAPS["Areas"][2]]})
    assert (await api.async_snapshot()).cursor is None
    api.request.assert_awaited_once_with("GET", "/capabilities")
    with pytest.raises(FeedProtocolError):
        FeedApi(MagicMock(), "https://app-dev.amzettel.at", MagicMock())


async def test_fragmented_frames_discard_partial_frame() -> None:
    content = MagicMock()

    async def chunks(size):
        yield b': heartbeat\r\n\r\nevent: checkpoint\r\nid: cs'
        yield b'tate\r\ndata: {"Version":1}\r\n\r\nevent: revoked\ndata: {"Version":1}\n'

    content.iter_chunked = chunks
    result = [frame async for frame in frames(content)]
    assert [(f.event, f.cursor) for f in result] == [("checkpoint", "cstate")]


@pytest.fixture
def feed(hass: HomeAssistant) -> AmZettelCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=SUB, data=entry_data())
    entry.add_to_hass(hass)
    api = MagicMock(spec=FeedApi, server=DEFAULT_SERVER)
    result = AmZettelCoordinator(hass, entry, api, SCOPES)
    api.async_snapshot.return_value = snapshot()
    return result


async def test_abort_page_keeps_baseline_and_cursor_then_rescope(feed: AmZettelCoordinator) -> None:
    await feed.async_resnapshot()
    old = feed.data
    feed.api.async_snapshot.side_effect = FeedHttpError(410, "snapshot_expired", None)
    with pytest.raises(FeedHttpError):
        await feed.async_resnapshot()
    assert feed.data is old and feed.cursor == "snapshot"
    feed.hide_feed_data()
    assert not feed.data.authoritative and not feed.data.lists and feed.cursor is None
    feed.api.async_snapshot.side_effect = None
    feed.api.async_snapshot.return_value = Snapshot(("MealPlans",), {}, {}, snapshot().plans, "rescoped")
    await feed.async_resnapshot()
    assert feed.data.baseline and feed.data.authoritative
    assert feed.cursor == "rescoped" and feed.areas == ("MealPlans",)


async def test_ordered_replay_does_not_ack_a_failed_read_or_later_checkpoint(feed: AmZettelCoordinator, socket_enabled):
    route_calls = []

    async def changes(request: web.Request) -> web.Response:
        route_calls.append((request.headers["Last-Event-ID"], request.query.getall("area")))
        return web.Response(
            content_type="text/event-stream",
            text=(
                f'event: change\nid: change\ndata: {{"Version":1,"Area":"Lists","Kind":"List","Id":"{LIST}",'
                '"Change":"Invalidated"}\n\n'
                'event: checkpoint\nid: checkpoint\ndata: {"Version":1}\n\n'
            ),
        )

    app = web.Application()
    app.router.add_get(PREFIX + "/changes", changes)
    await feed.async_resnapshot()
    feed.api.async_object.side_effect = CannotConnect("temporary")
    feed.api.headers.return_value = {"Authorization": "Bearer fake"}
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        feed.api.server = str(server.make_url("")).rstrip("/")
        sse = FeedStream(client, feed)
        with pytest.raises(CannotConnect):
            await sse._once(False)
        assert feed.cursor == "snapshot"
        feed.api.async_object.side_effect = None
        feed.api.async_object.return_value = {**feed.data.lists[LIST], "Name": "Updated"}
        assert await sse._once(False) == "retry"
    assert route_calls == [("snapshot", list(feed.areas))] * 2
    assert feed.cursor == "checkpoint" and feed.data.lists[LIST]["Name"] == "Updated"


async def test_token_expiry_keeps_cursor_and_revocation_stops(feed: AmZettelCoordinator, socket_enabled) -> None:
    auth = []

    async def changes(request: web.Request) -> web.Response:
        assert request.headers["Last-Event-ID"] == "snapshot"
        auth.append(request.headers["Authorization"])
        event = "token_expired" if len(auth) == 1 else "revoked"
        return web.Response(content_type="text/event-stream", text=f'event: {event}\ndata: {{"Version":1}}\n\n')

    app = web.Application()
    app.router.add_get(PREFIX + "/changes", changes)
    await feed.async_resnapshot()
    feed.api.headers.side_effect = lambda refresh: {"Authorization": "fresh" if refresh else "old"}
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        feed.api.server = str(server.make_url("")).rstrip("/")
        sse = FeedStream(client, feed)
        with patch.object(feed, "_start_reauth") as reauth, patch(
            "custom_components.amzettel.feed_stream.asyncio.sleep", new=AsyncMock(),
        ):
            await sse.run()
            feed.revoke_feed()
            reauth.assert_called_once()
    assert auth == ["old", "fresh"] and feed.revoked
    assert feed.cursor is None and feed.data == AmZettelData(baseline=True, authoritative=False)


@pytest.mark.parametrize("status", [429, 503])
async def test_rate_limit_reconnect_keeps_cursor(feed: AmZettelCoordinator, status: int) -> None:
    await feed.async_resnapshot()
    sse = FeedStream(MagicMock(), feed)
    with (
        patch.object(sse, "_once", side_effect=[FeedHttpError(status, "temporary", "23"), AuthFailed()]) as connect,
        patch.object(feed, "_start_reauth"),
        patch("custom_components.amzettel.feed_stream.asyncio.sleep", new=AsyncMock()) as sleep,
    ):
        await sse.run()
        assert connect.await_count == 2
        sleep.assert_awaited_once_with(23)
    feed.api.async_snapshot.assert_awaited_once()


async def test_resync_hides_while_snapshot_limit_is_reached(feed: AmZettelCoordinator) -> None:
    await feed.async_resnapshot()
    sse = FeedStream(MagicMock(), feed)
    feed.api.async_snapshot.side_effect = [FeedHttpError(429, "integration_limit", "19"), snapshot()]
    observations = []

    async def connect(refresh):
        if not observations:
            return "resync_required"
        assert feed.cursor == "snapshot" and feed.data.authoritative
        raise AuthFailed()

    async def wait(delay: float):
        observations.append((delay, feed.data.authoritative, feed.cursor))

    with (
        patch.object(sse, "_once", side_effect=connect),
        patch.object(feed, "_start_reauth"),
        patch("custom_components.amzettel.feed_stream.asyncio.sleep", side_effect=wait),
    ):
        await sse.run()
    assert observations == [(2, False, None), (19, False, None)]


async def test_stream_stop_cancels_open_connection(feed: AmZettelCoordinator, socket_enabled) -> None:
    opened = asyncio.Event()
    finish = asyncio.Event()

    async def changes(request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        opened.set()
        await finish.wait()
        return response

    app = web.Application()
    app.router.add_get(PREFIX + "/changes", changes)
    await feed.async_resnapshot()
    feed.api.headers.return_value = {}
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        feed.api.server = str(server.make_url("")).rstrip("/")
        sse = FeedStream(client, feed)
        task = asyncio.create_task(sse.run())
        try:
            await asyncio.wait_for(opened.wait(), 5)
            await sse.stop()
            assert task.cancelled() and not sse.connected
        finally:
            finish.set()


async def test_transport_retry_preserves_the_mutation_and_preconditions(socket_enabled) -> None:
    writes = []

    async def change(request: web.Request) -> web.Response:
        writes.append((request.method, request.headers["If-Match"], request.headers["Idempotency-Key"],
                       await request.json()))
        if len(writes) == 1:
            request.transport.close()
        return web.json_response({"Id": ITEM, "Replayed": len(writes) > 1})

    app = web.Application()
    app.router.add_patch(PREFIX + f"/calendars/{CAL}/events/{ITEM}", change)
    tokens = MagicMock(async_get_access_token=AsyncMock(return_value="fake"))
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        api = FeedApi(client, DEFAULT_SERVER, tokens)
        api.server = str(server.make_url("")).rstrip("/")
        assert await api.async_mutate(snapshot().lists[CAL], "edit", ITEM, {"Name": "Test"}) == {"id": ITEM}
    assert len(writes) == 2 and writes[0] == writes[1] and writes[0][1] == ETAG


async def test_patch_fields_and_conflict_are_not_replayed(feed: AmZettelCoordinator) -> None:
    await feed.async_resnapshot()
    feed.operations["Calendar"] = frozenset({"PatchEvent"})
    expected = deepcopy(feed.data.lists[CAL])
    feed.api.async_object.return_value = {**expected, "ETag": '"new"'}
    feed.api.async_mutate.side_effect = ApiError(412, "precondition_failed")
    previous = {
        "Due": {"Date": "2026-11-24", "Time": "16:00", "Zone": "Europe/Vienna", "EndTime": "17:00",
                "ReminderMinutes": 10},
    }
    with pytest.raises(ApiError, match="412"):
        await async_item_mutation(
            feed, expected, "edit", ITEM,
            {"Due": {"Date": "2026-11-24", "ReminderMinutes": 10}}, previous=previous,
        )
    feed.api.async_mutate.assert_awaited_once_with(
        expected, "edit", ITEM, {"Due": {"Time": None, "Zone": None, "EndTime": None}},
    )
    assert feed.data.lists[CAL]["ETag"] == '"new"'


async def test_buy_sends_no_client_check_id_and_reads_back(feed: AmZettelCoordinator) -> None:
    await feed.async_resnapshot()
    view = feed.data.lists[LIST]
    feed.api.async_mutate.return_value = {"id": ITEM}
    feed.api.async_object.return_value = {**view, "ETag": '"after"'}
    await async_item_mutation(feed, view, "buy", ITEM, {"CheckId": "legacy-only"})
    feed.api.async_mutate.assert_awaited_once_with(view, "buy", ITEM, None)
    assert feed.data.lists[LIST]["ETag"] == '"after"'


async def test_entry_keeps_registry_id_and_hides_data_on_revocation(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=SUB, data=entry_data())
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    prev = registry.async_get_or_create(
        "todo", DOMAIN, f"{SUB}_{LIST}", config_entry=entry, suggested_object_id="custom",
    )
    with (
        patch.object(FeedApi, "async_get_me", new=AsyncMock(return_value={})),
        patch.object(FeedApi, "async_snapshot", new=AsyncMock(return_value=snapshot())),
        patch.object(FeedStream, "run", new=AsyncMock()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get(prev.entity_id).state != "unavailable"
        coordinator = entry.runtime_data.coordinator
        with patch.object(coordinator, "_start_reauth"):
            coordinator.revoke_feed()
        await hass.async_block_till_done()
        assert registry.async_get(prev.entity_id) is not None
        assert hass.states.get(prev.entity_id).state == "unavailable"
        assert not coordinator.data.lists and not coordinator.data.meal_plans


def test_shared_sample_data_is_wire_shaped() -> None:
    """The fixtures every platform test uses are exactly what the v1 mapping makes of wire objects."""
    from .data import area_of, lists, plans, wire

    for view in [*lists(), *plans()]:
        assert map_object(wire(view), (area_of(view),)) == view
