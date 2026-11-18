"""The amZettel integration: lists as to-do lists, calendars and meal plans as calendars."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .auth import TokenManager, async_revoke
from .bus import BusEvents
from .const import (
    CONF_AUTH_BASE,
    CONF_ISSUER,
    CONF_REFRESH_TOKEN,
    CONF_SCOPES,
    CONF_SERVER,
    DEFAULT_SERVER,
    DOMAIN,
)
from .coordinator import AmZettelCoordinator
from .feed_api import FeedApi
from .feed_stream import FeedStream
from .services import async_setup_services
from .sources import sync_sources
from .token_store import EXCHANGE_SHUTDOWN_WAIT, entry_tokens, pop_entry_tokens
from .writes import clear_repairs

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CALENDAR, Platform.TODO]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass(slots=True)
class AmZettelRuntime:
    """What a loaded entry holds."""

    api: FeedApi
    tokens: TokenManager
    coordinator: AmZettelCoordinator
    realtime: FeedStream


type AmZettelConfigEntry = ConfigEntry[AmZettelRuntime]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """The actions exist once per HA instance, whatever entries are loaded."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AmZettelConfigEntry) -> bool:
    """Set up one amZettel account."""
    server = entry.data[CONF_SERVER]
    if server != DEFAULT_SERVER:
        raise ConfigEntryError(translation_domain=DOMAIN, translation_key="unsupported_server")
    session = async_get_clientsession(hass)
    # ONE token manager per entry that survives reloads; the tokens themselves live only in entry.data.
    registered = entry_tokens(hass, entry)
    tokens, token_store = registered.manager, registered.store
    token_store.take_replaced_credentials()  # this setup reads the current credentials anyway

    scopes = list(entry.data.get(CONF_SCOPES, []))
    api = FeedApi(session, server, tokens)
    coordinator = AmZettelCoordinator(hass, entry, api, scopes)
    realtime = FeedStream(session, coordinator)

    await coordinator.async_config_entry_first_refresh()

    @callback
    def _cancel_revoked_stream() -> None:
        if coordinator.revoked:
            realtime.cancel()

    entry.async_on_unload(coordinator.async_add_listener(_cancel_revoked_stream))
    # Bus events from comparing states; the first state is only the baseline (no flood).
    bus_events = BusEvents(hass, coordinator)
    entry.async_on_unload(coordinator.async_add_listener(bus_events.handle_update))

    entry.runtime_data = AmZettelRuntime(api, tokens, coordinator, realtime)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_create_background_task(hass, realtime.run(), "amzettel realtime")
    entry.async_on_unload(realtime.stop)

    # The update listener reloads the entry — HA expects an integration with a listener to reload
    # through it, not through the flow. It reloads when the options changed or when a reauth replaced
    # the credentials; the token store's own refreshes do not reload.
    applied: dict[str, Any] = {"options": dict(entry.options), "reloading": False}

    async def _entry_updated(hass: HomeAssistant, changed: ConfigEntry) -> None:
        # Once this setup scheduled its reload, every later change is read by the setup of that
        # reload: HA calls this listener only while it is registered, it is removed while the entry
        # unloads, and the next setup reads entry.data only after that. So one reload is enough.
        if applied["reloading"]:
            return
        replaced = token_store.take_replaced_credentials()
        if replaced or dict(changed.options) != applied["options"]:
            applied["reloading"] = True
            hass.config_entries.async_schedule_reload(changed.entry_id)

    entry.async_on_unload(entry.add_update_listener(_entry_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AmZettelConfigEntry) -> bool:
    """Unload; the realtime task is cancelled with the entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Revoke the refresh token at modgud (RFC 7009), best effort — failures are only logged on debug.

    A running exchange is awaited first: it redeemed the stored token, so only its answer is current.
    """
    refresh_token = entry.data.get(CONF_REFRESH_TOKEN)
    if (registered := pop_entry_tokens(hass, entry)) is not None:
        await registered.manager.async_wait_idle(EXCHANGE_SHUTDOWN_WAIT)
        refresh_token = registered.manager.tokens.refresh_token
    auth_base = entry.data.get(CONF_AUTH_BASE) or entry.data.get(CONF_ISSUER)
    if refresh_token and auth_base:
        await async_revoke(async_get_clientsession(hass), auth_base, refresh_token)
    clear_repairs(hass, entry)
    await sync_sources(hass).async_remove_entry(entry.entry_id)
