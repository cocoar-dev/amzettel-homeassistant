"""Category calendars: a chosen event category gets a calendar of its own, its events leave the main one."""

from __future__ import annotations

import copy
from datetime import datetime
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amzettel.const import DOMAIN
from custom_components.amzettel.coordinator import AmZettelData
from custom_components.amzettel.selection import Visibility, choices

from .conftest import SUB, entry_data
from .data import (
    CATEGORY_ID,
    CHECKLIST_ID,
    EVENTS,
    EVENTS_ID,
    PLAN_ID,
    SHOPPING_ID,
    TODO_ID,
)
from .fake_server import FakeServer

VIENNA = ZoneInfo("Europe/Vienna")
ALL_IDS = sorted([SHOPPING_ID, CHECKLIST_ID, TODO_ID, EVENTS_ID, PLAN_ID])
SPLIT = f"{EVENTS_ID}:{CATEGORY_ID}"
CATEGORY_UID = f"{SUB}_{EVENTS_ID}_cat_{CATEGORY_ID}"
OTHER_CATEGORY = "eeeeeeee-0000-0000-0000-000000000002"


def _events_view() -> dict:
    view = copy.deepcopy(EVENTS)
    view["EventCategories"].append({"Id": OTHER_CATEGORY, "Name": None, "Color": "t5"})  # no name: not offered
    view["Items"].append(
        {
            "Id": "dentist",
            "Name": "Zahnarzt",
            "Due": {"Date": "2026-11-16", "Time": "09:00", "Zone": "Europe/Vienna"},
        }
    )
    return view


def _options(split: list[str]) -> dict:
    return {
        "lists": [SHOPPING_ID, CHECKLIST_ID, TODO_ID],
        "calendars": [EVENTS_ID],
        "meal_plans": [PLAN_ID],
        "category_calendars": split,
        "auto_add_new": True,
        "known": ALL_IDS,
    }


async def _setup(hass, aioclient_mock, options) -> MockConfigEntry:
    await hass.config.async_set_time_zone("Europe/Vienna")
    FakeServer(aioclient_mock, [_events_view()])
    entry = MockConfigEntry(domain=DOMAIN, unique_id=SUB, data=entry_data(), options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity(hass: HomeAssistant, unique_id: str):
    entity_id = er.async_get(hass).async_get_entity_id("calendar", DOMAIN, unique_id)
    return hass.data["entity_components"]["calendar"].get_entity(entity_id) if entity_id else None


async def _summaries(
    hass: HomeAssistant, entity, start=datetime(2026, 11, 1, tzinfo=VIENNA), end=datetime(2026, 12, 31, tzinfo=VIENNA)
) -> list[str]:
    return [e.summary for e in await entity.async_get_events(hass, start, end)]


def test_offer_has_only_named_categories_of_visible_calendars() -> None:
    view = _events_view()
    offered = choices([view], [])
    assert offered["category_calendars"] == {SPLIT: "Familie · Müll"}
    hidden = Visibility({"calendars": [], "auto_add_new": False, "known": [EVENTS_ID]})
    assert choices([view], [], hidden)["category_calendars"] == {}


async def test_events_of_a_split_category_appear_only_in_its_calendar(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
) -> None:
    entry = await _setup(hass, aioclient_mock, _options([SPLIT]))
    main = _entity(hass, f"{SUB}_{EVENTS_ID}")
    category = _entity(hass, CATEGORY_UID)
    assert category is not None
    assert category.name == "Familie · Müll"
    assert category.device_info == main.device_info
    # the series (every second Monday, 30.11. excluded) lives only in the category calendar
    assert await _summaries(hass, category) == ["Gelbe Tonne", "Gelbe Tonne", "Gelbe Tonne"]
    assert await _summaries(hass, main) == ["Zahnarzt"]
    # the caches (behind `event`) are split the same way
    assert {e.summary for e in category._cache} == {"Gelbe Tonne"}
    assert {e.summary for e in main._cache} == {"Zahnarzt"}
    await hass.config_entries.async_unload(entry.entry_id)


async def test_one_expansion_per_list_for_main_and_category(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
) -> None:
    from unittest.mock import patch

    from custom_components.amzettel import calendar as cal

    calls = []
    original = cal.list_events_by_category

    def counting(*args, **kwargs):
        calls.append(args[0]["Id"])
        return original(*args, **kwargs)

    with patch.object(cal, "list_events_by_category", counting):
        entry = await _setup(hass, aioclient_mock, _options([SPLIT]))
    assert calls == [EVENTS_ID]  # main + category calendar share one expansion
    await hass.config_entries.async_unload(entry.entry_id)


async def test_deleted_category_removes_its_calendar_and_returns_the_events(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
) -> None:
    entry = await _setup(hass, aioclient_mock, _options([SPLIT]))
    coordinator = entry.runtime_data.coordinator
    view = _events_view()
    view["EventCategories"] = [c for c in view["EventCategories"] if c["Id"] != CATEGORY_ID]
    coordinator.async_set_updated_data(AmZettelData(lists={EVENTS_ID: view}, meal_plans=coordinator.data.meal_plans))
    await hass.async_block_till_done()
    assert er.async_get(hass).async_get_entity_id("calendar", DOMAIN, CATEGORY_UID) is None
    main = _entity(hass, f"{SUB}_{EVENTS_ID}")
    assert sorted(await _summaries(hass, main)) == ["Gelbe Tonne"] * 3 + ["Zahnarzt"]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_deselecting_the_category_calendar(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
) -> None:
    entry = await _setup(hass, aioclient_mock, _options([SPLIT]))
    result = await hass.config_entries.options.async_init(entry.entry_id)
    defaults = {str(k): k.default() for k in result["data_schema"].schema}
    assert defaults["category_calendars"] == [SPLIT]
    await hass.config_entries.options.async_configure(result["flow_id"], {"category_calendars": []})
    await hass.async_block_till_done()
    assert entry.options["category_calendars"] == []
    assert er.async_get(hass).async_get_entity_id("calendar", DOMAIN, CATEGORY_UID) is None
    main = _entity(hass, f"{SUB}_{EVENTS_ID}")
    assert "Gelbe Tonne" in await _summaries(hass, main)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_choosing_a_category_calendar_in_the_options(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
) -> None:
    entry = await _setup(hass, aioclient_mock, _options([]))
    assert _entity(hass, CATEGORY_UID) is None  # default: none
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(result["flow_id"], {"category_calendars": [SPLIT]})
    await hass.async_block_till_done()
    assert _entity(hass, CATEGORY_UID) is not None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_no_category_field_without_calendar_scope(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
) -> None:
    from custom_components.amzettel.const import CONF_SCOPES

    scopes = ["lists.read", "mealplans.read"]
    FakeServer(aioclient_mock, [_events_view()], scopes=scopes)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=SUB, data=entry_data(**{CONF_SCOPES: scopes}))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "category_calendars" not in {str(k) for k in result["data_schema"].schema}
    await hass.config_entries.async_unload(entry.entry_id)
