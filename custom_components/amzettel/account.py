"""Confirmed account display, independent of device names, tokens and entity identities."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .auth import normalize_server
from .const import CONF_ACCOUNT_LABEL, CONF_SERVER, CONF_USER_ID, DEFAULT_SERVER


def account_label(me: Mapping[str, Any]) -> str | None:
    """Use only the confirmed profile: e-mail distinguishes accounts with the same name."""
    for key in ("Email", "Name"):
        value = me.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def entry_title(server: str, label: str | None = None) -> str:
    """Account plus non-production host; never use a token or a device's user."""
    name = f"amZettel – {label}" if label else "amZettel"
    if normalize_server(server) == DEFAULT_SERVER:
        return name
    return f"{name} ({urlparse(normalize_server(server)).netloc})"


def updated_title(entry: ConfigEntry, label: str | None) -> str:
    """Refresh only generated titles, including legacy ones; preserve a user's custom title."""
    server = entry.data[CONF_SERVER]
    if entry.title in (entry_title(server), entry_title(server, entry.data.get(CONF_ACCOUNT_LABEL))):
        return entry_title(server, label)
    return entry.title


@callback
def update_account(hass: HomeAssistant, entry: ConfigEntry, me: Mapping[str, Any]) -> None:
    """Store one display value, without replacing credentials, options or identities."""
    label = account_label(me)
    hass.config_entries.async_update_entry(
        entry,
        title=updated_title(entry, label),
        data={**entry.data, CONF_ACCOUNT_LABEL: label, CONF_USER_ID: str(me["Id"])},
    )
