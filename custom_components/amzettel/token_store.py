"""The tokens of a config entry live in entry.data — and one TokenManager per entry, across reloads."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .auth import TokenManager, TokenSet
from .const import CONF_ACCESS_TOKEN, CONF_EXPIRES_AT, CONF_REFRESH_TOKEN, CONF_TOKEN_ENDPOINT, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Shutdown waits this long for a running exchange; below HA's 20 s for the shutdown-job stage.
EXCHANGE_SHUTDOWN_WAIT = 10.0


class EntryTokenStore:
    """entry.data as the token store; tells its own writes apart from a reauth's."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._known_refresh = entry.data.get(CONF_REFRESH_TOKEN)
        # Tokens an exchange brought after the entry was removed: kept only for the revocation.
        self._after_removal: TokenSet | None = None

    @property
    def token_endpoint(self) -> str:
        return self._entry.data[CONF_TOKEN_ENDPOINT]

    def get(self) -> TokenSet:
        if self._after_removal is not None:
            return self._after_removal
        data = self._entry.data
        return TokenSet(
            access_token=data[CONF_ACCESS_TOKEN],
            refresh_token=data.get(CONF_REFRESH_TOKEN),
            expires_at=float(data.get(CONF_EXPIRES_AT, 0)),
        )

    def compare_and_set(self, started_with: str | None, tokens: TokenSet) -> bool:
        if self.get().refresh_token != started_with:
            return False
        if self._hass.config_entries.async_get_entry(self._entry.entry_id) is None:
            # HA removes the entry from its registry before async_remove_entry runs.
            _LOGGER.debug("entry removed meanwhile: not storing the refreshed tokens")
            self._after_removal = tokens
            return True
        self._known_refresh = tokens.refresh_token
        self._hass.config_entries.async_update_entry(
            self._entry,
            data={
                **self._entry.data,
                CONF_ACCESS_TOKEN: tokens.access_token,
                CONF_REFRESH_TOKEN: tokens.refresh_token,
                CONF_EXPIRES_AT: tokens.expires_at,
            },
        )
        return True

    def take_replaced_credentials(self) -> bool:
        """True once when entry.data carries a refresh token this store did not write (a reauth)."""
        current = self._entry.data.get(CONF_REFRESH_TOKEN)
        if current == self._known_refresh:
            return False
        self._known_refresh = current
        return True


@dataclass(slots=True)
class EntryTokens:
    manager: TokenManager
    store: EntryTokenStore
    remove_shutdown_job: CALLBACK_TYPE


def entry_tokens(hass: HomeAssistant, entry: ConfigEntry) -> EntryTokens:
    """The manager and store of this entry: created by the first setup, reused by every reload.

    HA stops in stages (core.py `async_stop`): 1. shutdown jobs, awaited; 2. EVENT_HOMEASSISTANT_STOP,
    but tasks created BEFORE it are not awaited; 3. final write (entry.data is saved); 4.
    EVENT_HOMEASSISTANT_CLOSE closes the shared aiohttp session and cancels the old tasks. A running
    exchange therefore needs a shutdown job: it ends with the session still open, and its rotated
    refresh token is in entry.data before the final write.
    """
    registered: dict[str, EntryTokens] = hass.data.setdefault(DOMAIN, {})
    if entry.entry_id not in registered:
        store = EntryTokenStore(hass, entry)
        manager = TokenManager(async_get_clientsession(hass), store)
        remove = hass.async_add_shutdown_job(HassJob(manager.async_wait_idle), EXCHANGE_SHUTDOWN_WAIT)
        registered[entry.entry_id] = EntryTokens(manager, store, remove)
    return registered[entry.entry_id]


def pop_entry_tokens(hass: HomeAssistant, entry: ConfigEntry) -> EntryTokens | None:
    """The entry is removed: take its manager out (the caller still waits and revokes with it)."""
    tokens: EntryTokens | None = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if tokens is not None:
        tokens.remove_shutdown_job()
    return tokens
