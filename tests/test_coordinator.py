"""Coordinator: lock + snapshot after the answer, areas loaded as the capabilities allow."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amzettel.const import DEFAULT_SERVER, DOMAIN
from custom_components.amzettel.coordinator import AmZettelCoordinator
from custom_components.amzettel.feed_api import FeedApi, Snapshot

from .conftest import SCOPES, SUB, entry_data
from .data import CHECKLIST_ID, EVENTS_ID, PLAN_ID, SHOPPING_ID, lists, plans
from .fake_server import FEED, FakeServer

ALL_LIST_IDS = {view["Id"] for view in lists()}


def _snapshot(views=None, cursor: str = "cursor-1") -> Snapshot:
    return Snapshot(
        ("Calendar", "Lists", "MealPlans"),
        {"Lists": frozenset({"AddItem"})},
        {v["Id"]: v for v in (views if views is not None else lists())},
        {p["Id"]: p for p in plans()},
        cursor,
    )


async def _coordinator(hass: HomeAssistant) -> AmZettelCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=SUB, data=entry_data())
    entry.add_to_hass(hass)
    api = MagicMock(spec=FeedApi, server=DEFAULT_SERVER)
    api.async_snapshot.return_value = _snapshot()
    coordinator = AmZettelCoordinator(hass, entry, api, SCOPES)
    await coordinator.async_resnapshot()
    return coordinator


class Gate:
    """Hold API answers until the test releases them."""

    def __init__(self) -> None:
        self.events: dict[str, asyncio.Event] = {}

    def event(self, key: str) -> asyncio.Event:
        return self.events.setdefault(key, asyncio.Event())


async def test_concurrent_partial_refreshes_lose_nothing(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    gate = Gate()
    answers = {
        SHOPPING_ID: {**lists()[0], "Name": "Wocheneinkauf", "ETag": '"shopping-2"'},
        CHECKLIST_ID: {**lists()[1], "Name": "Sommerurlaub", "ETag": '"checklist-2"'},
    }

    async def get_object(area, ident):
        assert area == "Lists"
        await gate.event(ident).wait()
        return answers[ident]

    coordinator.api.async_object.side_effect = get_object
    first = asyncio.create_task(coordinator.async_apply_change("Lists", SHOPPING_ID, "c-2"))
    second = asyncio.create_task(coordinator.async_apply_change("Lists", CHECKLIST_ID, "c-3"))
    await asyncio.sleep(0)
    gate.event(CHECKLIST_ID).set()
    gate.event(SHOPPING_ID).set()
    await asyncio.gather(first, second)
    assert coordinator.data.lists[SHOPPING_ID]["Name"] == "Wocheneinkauf"
    assert coordinator.data.lists[CHECKLIST_ID]["Name"] == "Sommerurlaub"
    assert coordinator.cursor == "c-3"  # the cursors advance in the order of the stream


async def test_partial_refresh_next_to_full_refresh_keeps_a_new_list(hass: HomeAssistant) -> None:
    coordinator = await _coordinator(hass)
    new_list = {**lists()[1], "Id": "aaaaaaaa-0000-0000-0000-000000000099", "Name": "Neu"}
    gate = asyncio.Event()

    async def get_object(area, ident):
        await gate.wait()
        return lists()[0]

    coordinator.api.async_object.side_effect = get_object
    coordinator.api.async_snapshot.return_value = _snapshot([*lists(), new_list], "cursor-2")
    partial = asyncio.create_task(coordinator.async_pull_list(SHOPPING_ID))
    await asyncio.sleep(0)  # the partial pull is waiting for its answer
    full = asyncio.create_task(coordinator.async_resnapshot())
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(partial, full)
    assert new_list["Id"] in coordinator.data.lists
    assert coordinator.cursor == "cursor-2"


# ── areas: the capabilities of the grant decide what the snapshot asks for ─────────────────


async def _setup(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, scopes, server_scopes=None):
    server = FakeServer(aioclient_mock, scopes=server_scopes if server_scopes is not None else scopes)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=SUB, data=entry_data(scopes=scopes))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    requested = [
        data["Areas"]
        for method, url, data, _ in aioclient_mock.mock_calls
        if method.upper() == "POST" and str(url) == f"{FEED}/snapshots"
    ]
    return entry.runtime_data.coordinator, server, requested


async def test_only_meal_plan_scope_loads_plans(hass: HomeAssistant, aioclient_mock, no_realtime) -> None:
    coordinator, _, requested = await _setup(hass, aioclient_mock, ["openid", "offline_access", "mealplans.read"])
    assert requested == [["MealPlans"]]
    assert coordinator.data.lists == {} and PLAN_ID in coordinator.data.meal_plans


async def test_calendar_scope_alone_loads_calendars(hass: HomeAssistant, aioclient_mock, no_realtime) -> None:
    coordinator, _, requested = await _setup(hass, aioclient_mock, ["openid", "calendar.read"])
    assert requested == [["Calendar"]]
    assert set(coordinator.data.lists) == {EVENTS_ID} and coordinator.data.meal_plans == {}


async def test_refused_meal_plans_leave_lists_intact(hass: HomeAssistant, aioclient_mock, no_realtime) -> None:
    server_scopes = [s for s in SCOPES if s != "mealplans.read"]  # the grant does not allow meal plans
    coordinator, _, requested = await _setup(hass, aioclient_mock, SCOPES, server_scopes)
    assert requested == [["Calendar", "Lists"]]
    assert set(coordinator.data.lists) == ALL_LIST_IDS
    assert coordinator.data.meal_plans == {}


async def test_refused_lists_leave_meal_plans_intact(hass: HomeAssistant, aioclient_mock, no_realtime) -> None:
    server_scopes = ["openid", "offline_access", "mealplans.read"]
    coordinator, _, requested = await _setup(hass, aioclient_mock, SCOPES, server_scopes)
    assert requested == [["MealPlans"]]
    assert coordinator.data.lists == {} and PLAN_ID in coordinator.data.meal_plans
    assert not coordinator.writes_lists  # the scope alone does not allow writing without the capability
