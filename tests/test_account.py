"""Confirmed account display: existing entries, user titles, refresh failures and privacy."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.amzettel.account import account_label, entry_title

from .conftest import SERVER
from .fake_server import FakeServer


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ({"Email": " anna@example.test ", "Name": "Anna"}, "anna@example.test"),
        ({"Email": None, "Name": " Anna "}, "Anna"),
        ({"Email": "", "Name": "Anna"}, "Anna"),
        ({"Name": "Anna"}, "Anna"),
        ({"Email": {}, "Name": "Anna"}, "Anna"),
        ({}, None),
        ({"Email": " ", "Name": None}, None),
    ],
)
def test_label_contains_only_confirmed_profile_fields(profile, expected):
    assert account_label(profile) == expected
    assert entry_title("https://app.amzettel.at", expected) == (
        f"amZettel – {expected}" if expected else "amZettel"
    )


@pytest.mark.parametrize(
    ("old_title", "expected"),
    [
        ("amZettel", "amZettel – a@example.com"),
        ("amZettel – old@example.test", "amZettel – a@example.com"),
        ("Unser Familienkonto", "Unser Familienkonto"),
    ],
)
async def test_existing_entry_learns_account_without_changing_identity(
    hass: HomeAssistant, aioclient_mock, config_entry, no_realtime, old_title, expected
):
    FakeServer(aioclient_mock)
    config_entry.add_to_hass(hass)
    data = dict(config_entry.data)
    if "old@example.test" in old_title:
        data["account_label"] = "old@example.test"
    hass.config_entries.async_update_entry(config_entry, title=old_title, data=data)
    original_data = dict(config_entry.data)
    original_unique_id = config_entry.unique_id
    original_options = dict(config_entry.options)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = config_entry.runtime_data.coordinator
    assert config_entry.title == expected
    assert config_entry.data == {**original_data, "account_label": "a@example.com"}
    assert config_entry.unique_id == original_unique_id
    assert dict(config_entry.options) == original_options
    registry = er.async_get(hass)
    identities = {e.unique_id: e.entity_id for e in registry.entities.values()}
    await coordinator.async_refresh()  # a new baseline confirms the profile again
    await hass.async_block_till_done()
    assert config_entry.runtime_data.coordinator is coordinator  # no reload loop for metadata
    assert {e.unique_id: e.entity_id for e in registry.entities.values()} == identities
    options = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert options["description_placeholders"]["account"] == "a@example.com"
    assert options["description_placeholders"]["server"] == SERVER
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_revoked_profile_is_an_auth_failure_not_a_cosmetic_fallback(
    hass, aioclient_mock, config_entry, no_realtime
):
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{SERVER}/api/auth/me", status=403, json={"title": "integration_revoked"})
    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert "account_label" not in config_entry.data
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


@pytest.mark.parametrize("profile_status", [403, 500])
async def test_profile_unavailable_does_not_hide_objects(
    hass, aioclient_mock, config_entry, no_realtime, profile_status
):
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{SERVER}/api/auth/me", status=profile_status, json={})
    FakeServer(aioclient_mock)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.title == "amZettel"
    assert "account_label" not in config_entry.data
    assert config_entry.runtime_data.coordinator.data.lists
    assert config_entry.runtime_data.coordinator.data.meal_plans
    await hass.config_entries.async_unload(config_entry.entry_id)
