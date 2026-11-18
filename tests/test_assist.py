"""Assist finds our lists by their amZettel name and completes items without touching note or due."""

from __future__ import annotations

from homeassistant.components.todo.intent import async_setup_intents
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amzettel.const import DOMAIN

from .conftest import SUB
from .data import EVENTS_ID, SHOPPING_ID
from .fake_server import FakeServer


async def _setup(hass, aioclient_mock, entry) -> FakeServer:
    server = FakeServer(aioclient_mock)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return server


async def test_names_are_the_amzettel_names_without_a_device(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, no_realtime
) -> None:
    await _setup(hass, aioclient_mock, config_entry)
    registry = er.async_get(hass)
    for platform, unique_id, name in (
        ("todo", f"{SUB}_{SHOPPING_ID}", "Einkauf"),
        ("calendar", f"{SUB}_{EVENTS_ID}", "Familie"),
    ):
        entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
        assert registry.async_get(entity_id).device_id is None
        assert hass.states.get(entity_id).attributes["friendly_name"] == name
    assert not dr.async_entries_for_config_entry(dr.async_get(hass), config_entry.entry_id)
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_assist_adds_to_our_list_by_its_name(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, no_realtime
) -> None:
    """HassListAddItem with the list name "Einkauf" — what "Setz Milch auf die Liste Einkauf" becomes."""
    server = await _setup(hass, aioclient_mock, config_entry)
    await async_setup_intents(hass)
    response = await intent.async_handle(
        hass, "test", "HassListAddItem", {"item": {"value": "Milch"}, "name": {"value": "Einkauf"}}
    )
    assert response.response_type == intent.IntentResponseType.ACTION_DONE
    ((operation, item_id, body),) = server.writes
    assert operation == "add" and body["Name"] == "Milch"
    assert server.requests[0][1] == f"/api/integrations/v1/lists/{SHOPPING_ID}/items/{item_id}"
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_assist_completing_a_task_keeps_note_and_due(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, no_realtime
) -> None:
    """HassListCompleteItem sends an item with uid, summary and status only: only "done", no edit."""
    from .data import TAX_ID, TODO, TODO_ID

    server = await _setup(hass, aioclient_mock, config_entry)
    await async_setup_intents(hass)
    response = await intent.async_handle(
        hass, "test", "HassListCompleteItem", {"item": {"value": "Steuer"}, "name": {"value": "Aufgaben"}}
    )
    assert response.response_type == intent.IntentResponseType.ACTION_DONE
    assert server.writes == [("done", TAX_ID, {"Done": True})]  # no edit
    tax = server.item(TODO_ID, TAX_ID)
    assert tax["Note"] == "bis Freitag" and tax["Due"] == TODO["Items"][0]["Due"]  # note and due survive
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_assist_completing_a_shopping_item_only_buys(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, no_realtime
) -> None:
    from .data import MILK_ID

    server = await _setup(hass, aioclient_mock, config_entry)
    await async_setup_intents(hass)
    await intent.async_handle(
        hass, "test", "HassListCompleteItem", {"item": {"value": "Milch"}, "name": {"value": "Einkauf"}}
    )
    assert server.writes == [("buy", MILK_ID, None)]  # no edit: note and amount stay
    assert server.requests[0][:2] == ("POST", f"/api/integrations/v1/lists/{SHOPPING_ID}/items/{MILK_ID}/buy")
    await hass.config_entries.async_unload(config_entry.entry_id)
