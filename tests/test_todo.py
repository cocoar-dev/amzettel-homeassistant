"""To-do lists: mapping of the three types and the writes against a mocked API."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.todo import TodoItemStatus
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amzettel.const import CONF_SCOPES
from custom_components.amzettel.coordinator import AmZettelData
from custom_components.amzettel.todo import (
    due_from_ha,
    due_to_ha,
    format_number,
    note_from_description,
    recent_purchases,
    render_amount,
    todo_items,
)

from .conftest import SUB, entry_data
from .data import (
    BREAD_CHECK,
    CASE_ID,
    CHECKLIST,
    CHECKLIST_ID,
    MILK_ID,
    PASS_ID,
    SHOPPING,
    SHOPPING_ID,
    TAX_ID,
    TODO,
    TODO_ID,
    lists,
)
from .fake_server import FakeServer

SHOP = "todo.einkauf"
CHECK = "todo.urlaub"
TASKS = "todo.aufgaben"
NOW = datetime(2026, 11, 13, 11, 0, tzinfo=ZoneInfo("UTC"))


# ── pure mapping ───────────────────────────────────────────────────────────────────────────


def test_amount_formatting() -> None:
    assert render_amount(2, "L") == "2 L"
    assert render_amount(1, "kg") == "1 kg"
    assert render_amount(6, None) == "6"
    assert render_amount(1, None) == ""
    assert render_amount(0.25, "kg") == "0.25 kg"
    assert render_amount(0.25, "kg", "de") == "0,25 kg"
    assert format_number(1e21) == "1000000000000000000000"  # no exponent
    assert format_number(1 / 3) == "0.333"  # at most three decimals
    assert format_number(0.0000001) == "0"
    assert format_number(1.5, "de-AT") == "1,5"


def test_summary_is_the_name_and_amount_leads_the_description() -> None:
    items = todo_items(SHOPPING, "en", NOW)
    milk = next(i for i in items if i.uid == MILK_ID)
    eggs = next(i for i in items if i.summary == "Eier")
    assert milk.summary == "Milch"  # Assist finds "Milch"
    assert milk.description == "2 L\nlaktosefrei"
    assert eggs.description is None  # one, no unit, no note


def test_note_from_description() -> None:
    assert note_from_description("2 L\nHafer", "2 L") == "Hafer"
    assert note_from_description("2 L", "2 L") is None
    assert note_from_description("Hafer", "2 L") == "Hafer"  # amount line removed by the user
    assert note_from_description("3 L\nHafer", "2 L") == "3 L\nHafer"  # not the amount → all note
    assert note_from_description(None, "2 L") is None


def test_shopping_mapping() -> None:
    items = todo_items(SHOPPING, "en", NOW)
    open_items = [i for i in items if i.status == TodoItemStatus.NEEDS_ACTION]
    done = [i for i in items if i.status == TodoItemStatus.COMPLETED]
    assert [i.summary for i in open_items] == ["Milch", "Eier"]
    assert open_items[0].due is None
    # only purchases after CartClosedAt are "recent"
    assert [(i.uid, i.summary) for i in done] == [(f"purchase:{BREAD_CHECK}", "Brot")]
    assert done[0].completed == datetime(2026, 11, 13, 10, tzinfo=ZoneInfo("UTC"))


def test_recent_purchases_follow_the_web_rule(hass: HomeAssistant) -> None:
    """After the cart close AND (today OR younger than 2 h) — ListWorkspace.vue `recentOf`."""
    view = {
        "CartClosedAt": None,
        "RecentPurchases": [
            {"CheckId": "today", "Name": "a", "Quantity": 1, "CheckedAt": "2026-11-13T00:30:00+00:00"},
            {"CheckId": "yesterday", "Name": "b", "Quantity": 1, "CheckedAt": "2026-11-12T08:00:00+00:00"},
            {"CheckId": "recent", "Name": "c", "Quantity": 1, "CheckedAt": "2026-11-13T07:30:00+00:00"},
        ],
    }
    now = datetime(2026, 11, 13, 9, 0, tzinfo=ZoneInfo("UTC"))
    # HA's test zone is US/Pacific: "today" there is 13.11. from 08:00 UTC on.
    ids = [p["CheckId"] for p in recent_purchases(view, now)]
    assert ids == ["recent"]  # 00:30 UTC is 12.11. in Pacific and > 2 h ago; 08:00 UTC the day before
    earlier = datetime(2026, 11, 13, 1, 0, tzinfo=ZoneInfo("UTC"))  # 12.11. 17:00 Pacific
    view["RecentPurchases"] = view["RecentPurchases"][:2]
    # both lie on 12.11. Pacific = "today" then; newest first
    assert [p["CheckId"] for p in recent_purchases(view, earlier)] == ["today", "yesterday"]


def test_checklist_mapping() -> None:
    items = {i.uid: i for i in todo_items(CHECKLIST)}
    assert items[PASS_ID].status == TodoItemStatus.NEEDS_ACTION
    assert items[CASE_ID].status == TodoItemStatus.COMPLETED
    assert items[CASE_ID].completed is not None


def test_todo_mapping_with_due() -> None:
    (item,) = todo_items(TODO)
    assert item.due == datetime(2026, 11, 20, 10, 0, tzinfo=ZoneInfo("Europe/Vienna"))
    assert item.description == "bis Freitag"


def test_due_round_trip() -> None:
    assert due_to_ha({"Date": "2026-11-20"}) == date(2026, 11, 20)
    assert due_from_ha(datetime(2026, 11, 21, 9, 30, tzinfo=ZoneInfo("Europe/London")), None) == {
        "Date": "2026-11-21",
        "Time": "09:30",
        "Zone": "Europe/London",
    }


def test_due_from_ha_keeps_every_other_field_and_moves_the_end() -> None:
    all_day = {"Date": "2026-11-20", "EndDate": "2026-11-22", "ReminderMinutes": 0, "IncludeInSummary": True}
    assert due_from_ha(date(2026, 11, 25), all_day) == {
        "Date": "2026-11-25",
        "EndDate": "2026-11-27",  # same three days, moved along
        "ReminderMinutes": 0,
        "IncludeInSummary": True,
    }
    timed = {
        "Date": "2026-11-20",
        "Time": "22:00",
        "Zone": "Europe/Vienna",
        "EndDate": "2026-11-21",
        "EndTime": "01:00",
        "ReminderMinutes": 30,
        "IncludeInSummary": True,
    }
    assert due_from_ha(datetime(2026, 11, 20, 10, 0, tzinfo=ZoneInfo("Europe/Vienna")), timed) == {
        "Date": "2026-11-20",
        "Time": "10:00",
        "Zone": "Europe/Vienna",
        "EndTime": "13:00",  # same three hours, now on the same day → no EndDate
        "ReminderMinutes": 30,
        "IncludeInSummary": True,
    }
    # all-day ↔ timed: the end has another meaning → dropped
    assert due_from_ha(date(2026, 11, 20), timed) == {
        "Date": "2026-11-20",
        "ReminderMinutes": 30,
        "IncludeInSummary": True,
    }


# ── entities ───────────────────────────────────────────────────────────────────────────────


async def _setup(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry, **server: Any
) -> FakeServer:
    fake = FakeServer(aioclient_mock, scopes=entry.data[CONF_SCOPES], **server)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return fake


@pytest.fixture
async def server(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, no_realtime, freezer):
    freezer.move_to(NOW)
    fake = await _setup(hass, aioclient_mock, config_entry)
    yield fake
    await hass.config_entries.async_unload(config_entry.entry_id)


async def _service(hass: HomeAssistant, service: str, entity_id: str, **data) -> None:
    await hass.services.async_call("todo", service, {"entity_id": entity_id, **data}, blocking=True)


async def test_entities_and_state(hass: HomeAssistant, server) -> None:
    registry = er.async_get(hass)
    assert registry.async_get(SHOP).unique_id == f"{SUB}_{SHOPPING_ID}"
    assert hass.states.get(SHOP).state == "2"  # two open items
    assert hass.states.get(CHECK).state == "1"
    assert hass.states.get(TASKS).state == "1"
    assert not [e for e in registry.entities.values() if e.domain == "todo" and e.unique_id.endswith("0004")]


async def test_no_write_features_without_lists_write(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
) -> None:
    entry = MockConfigEntry(
        domain="amzettel",
        unique_id=SUB,
        title="amZettel",
        data=entry_data(**{CONF_SCOPES: ["openid", "offline_access", "lists.read"]}),
    )
    await _setup(hass, aioclient_mock, entry)
    assert hass.states.get(SHOP).attributes["supported_features"] == 0
    assert hass.states.get(TASKS).attributes["supported_features"] == 0
    await hass.config_entries.async_unload(entry.entry_id)


async def test_no_write_features_without_the_write_capability(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
) -> None:
    """The scope alone is not enough: the grant's capabilities must offer the operations too."""
    entry = MockConfigEntry(domain="amzettel", unique_id=SUB, title="amZettel", data=entry_data())
    server = FakeServer(aioclient_mock, scopes=["lists.read", "calendar.read", "mealplans.read"])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(SHOP).attributes["supported_features"] == 0
    assert server.writes == []
    await hass.config_entries.async_unload(entry.entry_id)


async def test_write_features_with_lists_write(hass: HomeAssistant, server) -> None:
    assert hass.states.get(SHOP).attributes["supported_features"] == 1 | 2 | 4 | 64
    assert hass.states.get(TASKS).attributes["supported_features"] == 1 | 2 | 4 | 16 | 32 | 64


async def test_add_item(hass: HomeAssistant, server: FakeServer) -> None:
    await _service(hass, "add_item", SHOP, item="Käse", description="Bergkäse")
    ((operation, item_id, body),) = server.writes
    assert operation == "add" and len(item_id) == 36
    assert body == {"Name": "Käse", "Quantity": 1, "Note": "Bergkäse"}  # the id is in the path
    ((method, path, headers),) = server.requests
    assert (method, path) == ("PUT", f"/api/integrations/v1/lists/{SHOPPING_ID}/items/{item_id}")
    assert headers["If-Match"] == SHOPPING["ETag"] and headers["Idempotency-Key"]
    assert server.item(SHOPPING_ID, item_id)["Name"] == "Käse"


async def test_add_todo_with_due(hass: HomeAssistant, server: FakeServer) -> None:
    await _service(hass, "add_item", TASKS, item="Müll", due_date="2026-11-25")
    ((_, _, body),) = server.writes
    assert body["Due"] == {"Date": "2026-11-25"}


async def test_buy_by_plain_name(hass: HomeAssistant, server: FakeServer) -> None:
    """Assist says "Milch", not "Milch (2 L)"."""
    await _service(hass, "update_item", SHOP, item="Milch", status="completed")
    # the server derives the purchase from the mutation; nothing else changed: the amount line is no note
    assert server.writes == [("buy", MILK_ID, None)]
    assert server.requests[0][1].endswith(f"/lists/{SHOPPING_ID}/items/{MILK_ID}/buy")
    assert hass.states.get(SHOP).state == "1"  # read back after the write


async def test_unbuy_purchase(hass: HomeAssistant, server: FakeServer) -> None:
    await _service(hass, "update_item", SHOP, item=f"purchase:{BREAD_CHECK}", status="needs_action")
    assert server.writes == [("unbuy", BREAD_CHECK, None)]
    assert server.requests[0][:2] == ("POST", f"/api/integrations/v1/lists/{SHOPPING_ID}/purchases/{BREAD_CHECK}/unbuy")


async def test_rename_is_a_partial_update(hass: HomeAssistant, server: FakeServer) -> None:
    await _service(hass, "update_item", SHOP, item=MILK_ID, rename="Hafermilch")
    assert server.writes == [("edit", MILK_ID, {"Name": "Hafermilch"})]
    assert server.requests[0][0] == "PATCH"
    milk = server.item(SHOPPING_ID, MILK_ID)
    assert milk == {**SHOPPING["Items"][0], "Name": "Hafermilch"}  # every other field kept


async def test_description_edit_strips_the_amount_line(hass: HomeAssistant, server: FakeServer) -> None:
    await _service(hass, "update_item", SHOP, item="Milch", description="2 L\nHafer")
    assert server.writes == [("edit", MILK_ID, {"Note": "Hafer"})]
    milk = server.item(SHOPPING_ID, MILK_ID)
    assert milk["Name"] == "Milch" and milk["Note"] == "Hafer" and milk["Quantity"] == 2


async def test_todo_due_change_keeps_reminder(hass: HomeAssistant, server: FakeServer) -> None:
    await _service(hass, "update_item", TASKS, item="Steuer", due_date="2026-11-27")
    # only the changed time fields; the reminder is not named, so the server keeps it
    assert server.writes == [("edit", TAX_ID, {"Due": {"Date": "2026-11-27", "Time": None, "Zone": None}})]
    tax = server.item(TODO_ID, TAX_ID)
    assert tax["Due"] == {"Date": "2026-11-27", "ReminderMinutes": 30}
    assert tax["Name"] == "Steuer" and tax["Note"] == "bis Freitag"


async def test_checklist_done_and_undone(hass: HomeAssistant, server: FakeServer) -> None:
    await _service(hass, "update_item", CHECK, item="Pass", status="completed")
    await _service(hass, "update_item", CHECK, item="Koffer", status="needs_action")
    assert server.writes == [("done", PASS_ID, {"Done": True}), ("done", CASE_ID, {"Done": False})]
    assert [(m, p) for m, p, _ in server.requests] == [
        ("PUT", f"/api/integrations/v1/lists/{CHECKLIST_ID}/items/{PASS_ID}/done"),
        ("PUT", f"/api/integrations/v1/lists/{CHECKLIST_ID}/items/{CASE_ID}/done"),
    ]
    # the second write carries the ETag the first one produced (read back in between)
    assert server.requests[1][2]["If-Match"] != server.requests[0][2]["If-Match"]


async def test_delete(hass: HomeAssistant, server: FakeServer) -> None:
    await _service(hass, "remove_item", SHOP, item=["Milch"])
    assert server.writes == [("remove", MILK_ID, None)]
    assert server.requests[0][:2] == ("DELETE", f"/api/integrations/v1/lists/{SHOPPING_ID}/items/{MILK_ID}")


async def test_purchase_cannot_be_deleted(hass: HomeAssistant, server: FakeServer) -> None:
    with pytest.raises(HomeAssistantError) as err:
        await _service(hass, "remove_item", SHOP, item=[f"purchase:{BREAD_CHECK}"])
    assert err.value.translation_key == "purchase_not_deletable"
    assert server.requests == []


async def test_premium_required(hass: HomeAssistant, server: FakeServer) -> None:
    server.refuse = {"title": "integration_premium_required", "status": 403}
    with pytest.raises(HomeAssistantError) as err:
        await _service(hass, "add_item", SHOP, item="Käse")
    assert err.value.translation_key == "premium_required"


async def test_gate_refusal_points_to_connected_systems(hass: HomeAssistant, server: FakeServer) -> None:
    server.refuse = "integration_not_permitted"
    with pytest.raises(HomeAssistantError) as err:
        await _service(hass, "add_item", SHOP, item="Käse")
    assert err.value.translation_key == "not_permitted"


async def test_other_403_carries_the_server_reason(hass: HomeAssistant, server: FakeServer) -> None:
    server.refuse = {"title": "Share.OwnerOnly", "detail": "Only the owner can do this", "status": 403}
    with pytest.raises(HomeAssistantError) as err:
        await _service(hass, "remove_item", SHOP, item=["Milch"])
    assert err.value.translation_key == "refused"
    assert err.value.translation_placeholders == {"reason": "Only the owner can do this"}


async def test_parallel_updates_keep_both_changes(hass: HomeAssistant, server: FakeServer) -> None:
    """Rename and note at the same time: the second write must build on the first one's result."""
    server.slow_writes = 20  # let the other call run meanwhile
    await asyncio.gather(
        _service(hass, "update_item", SHOP, item=MILK_ID, rename="Hafermilch"),
        _service(hass, "update_item", SHOP, item=MILK_ID, description="2 L\nungesüßt"),
    )
    milk = server.item(SHOPPING_ID, MILK_ID)
    assert (milk["Name"], milk["Note"]) == ("Hafermilch", "ungesüßt"), json.dumps(milk)
    # the second write carries the ETag the first one produced, so it was not a blind overwrite
    first, second = (headers["If-Match"] for _, _, headers in server.requests)
    assert first == SHOPPING["ETag"] and second != first


async def test_a_stale_version_is_refused_as_a_conflict_and_read_again(
    hass: HomeAssistant, server: FakeServer
) -> None:
    """Changed elsewhere since the last read: 412, nothing written, the current version is read back."""
    server.touch(SHOPPING_ID)
    with pytest.raises(HomeAssistantError) as err:
        await _service(hass, "update_item", SHOP, item=MILK_ID, rename="Hafermilch")
    assert err.value.translation_key == "write_conflict"
    assert server.writes == [] and server.item(SHOPPING_ID, MILK_ID)["Name"] == "Milch"
    await _service(hass, "update_item", SHOP, item=MILK_ID, rename="Hafermilch")  # the retry builds on it
    assert server.writes == [("edit", MILK_ID, {"Name": "Hafermilch"})]


async def test_only_v1_and_identity_endpoints_are_used(
    hass: HomeAssistant, server: FakeServer, aioclient_mock: AiohttpClientMocker
) -> None:
    await _service(hass, "add_item", SHOP, item="Käse")
    await _service(hass, "update_item", CHECK, item="Pass", status="completed")
    await _service(hass, "remove_item", SHOP, item=["Milch"])
    paths = {c[1].path for c in aioclient_mock.mock_calls}
    assert paths and all(p == "/api/auth/me" or p.startswith("/api/integrations/v1/") for p in paths), paths


async def test_lists_come_and_go(hass: HomeAssistant, server, config_entry) -> None:
    coordinator = config_entry.runtime_data.coordinator
    registry = er.async_get(hass)
    new_list = {**CHECKLIST, "Id": "aaaaaaaa-0000-0000-0000-000000000099", "Name": "Neu", "Items": []}
    data = AmZettelData(
        lists={v["Id"]: v for v in lists() if v["Id"] != CHECKLIST_ID} | {new_list["Id"]: new_list},
        meal_plans=coordinator.data.meal_plans,
    )
    coordinator.async_set_updated_data(data)
    await hass.async_block_till_done()
    assert registry.async_get(CHECK) is None
    assert hass.states.get(CHECK) is None
    assert registry.async_get_entity_id("todo", "amzettel", f"{SUB}_{new_list['Id']}") is not None


async def test_clearing_the_note_through_the_service_still_works(hass: HomeAssistant, server: FakeServer) -> None:
    """An explicitly empty description clears the note."""
    await _service(hass, "update_item", TASKS, item="Steuer", description="")
    assert server.writes == [("edit", TAX_ID, {"Note": None})]  # no status change
    assert server.item(TODO_ID, TAX_ID)["Due"] == TODO["Items"][0]["Due"]  # untouched


async def test_status_change_and_rename_together(hass: HomeAssistant, server: FakeServer) -> None:
    await _service(hass, "update_item", TASKS, item="Steuer", rename="Steuererklärung", status="completed")
    assert server.writes == [("edit", TAX_ID, {"Name": "Steuererklärung"}), ("done", TAX_ID, {"Done": True})]
    tax = server.item(TODO_ID, TAX_ID)
    assert tax["Note"] == "bis Freitag" and tax["Due"] == TODO["Items"][0]["Due"]  # every other field kept


def test_due_from_ha_is_unambiguous_around_the_clock_change() -> None:
    """The backend resolves a repeated wall time to the earlier instant and pushes a skipped one forward,
    so a repeated time goes to UTC and a skipped one is written as the real wall time."""
    vienna = ZoneInfo("Europe/Vienna")
    second = datetime(2027, 10, 31, 2, 30, tzinfo=vienna, fold=1)  # 02:30+01:00, the later one
    assert due_from_ha(second, None) == {"Date": "2027-10-31", "Time": "01:30", "Zone": "UTC"}
    first = datetime(2027, 10, 31, 2, 30, tzinfo=vienna)  # 02:30+02:00
    assert due_from_ha(first, None) == {"Date": "2027-10-31", "Time": "00:30", "Zone": "UTC"}
    skipped = datetime(2027, 3, 28, 2, 30, tzinfo=vienna)
    assert due_from_ha(skipped, None) == {"Date": "2027-03-28", "Time": "03:30", "Zone": "Europe/Vienna"}
