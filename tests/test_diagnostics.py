"""Diagnostics carry no tokens and no e-mail addresses."""

from __future__ import annotations

import json

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amzettel.diagnostics import async_get_config_entry_diagnostics

from .conftest import SERVER, USER_ID
from .fake_server import FakeServer


@pytest.mark.parametrize(
    "profile", [{"Id": USER_ID, "Email": "private@example.test"}, {"Id": USER_ID, "Name": "Private Name"}]
)
async def test_diagnostics_redacted(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, no_realtime, profile
) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{SERVER}/api/auth/me", json=profile)
    server = FakeServer(aioclient_mock)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await async_get_config_entry_diagnostics(hass, config_entry)
    text = json.dumps(result, default=str)
    assert config_entry.data["access_token"] not in text
    assert config_entry.data["refresh_token"] not in text
    assert "@" not in text
    assert config_entry.data["account_label"] not in text
    assert "Milch" not in text  # no content
    assert server.cursor not in text  # no stream position
    assert result["realtime"]["protocol"] == "sse-v1"
    assert len(result["lists"]) == 4 and len(result["meal_plans"]) == 1
    await hass.config_entries.async_unload(config_entry.entry_id)
