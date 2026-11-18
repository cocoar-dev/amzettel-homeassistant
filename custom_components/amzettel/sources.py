"""Which events `amzettel.sync_events` created, per config entry, calendar and source (the plan's
"Store" variant: the backend has no origin field, so the integration remembers what is its own).

Stored: `{entry_id: {calendar_id: {source: {external_id: item_id}}}}`. An event is proven to be a
source's own only when it is listed here AND its id is the one derived from (calendar, source,
external id); `sync_events` never removes anything else.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.sync_sources"
DATA_SOURCES: HassKey[SyncSources] = HassKey(f"{DOMAIN}_sync_sources")


class SyncSources:
    """The store, loaded once; `lock` serialises the syncs (they read, write and save it)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: dict[str, Any] | None = None
        self.lock = asyncio.Lock()

    async def async_load(self) -> None:
        if self._data is None:
            self._data = await self._store.async_load() or {}

    def owned(self, entry_id: str, calendar_id: str, source: str) -> dict[str, str]:
        """external id → item id of one source in one calendar (a live dict: changes are kept)."""
        assert self._data is not None
        return self._data.setdefault(entry_id, {}).setdefault(calendar_id, {}).setdefault(source, {})

    def prune(self, entry_id: str, calendar_id: str, source: str) -> None:
        """Drop empty levels, so a source that owns nothing leaves nothing behind."""
        assert self._data is not None
        calendars = self._data.get(entry_id, {})
        sources = calendars.get(calendar_id, {})
        if not sources.get(source):
            sources.pop(source, None)
        if not sources:
            calendars.pop(calendar_id, None)
        if not calendars:
            self._data.pop(entry_id, None)

    async def async_save(self) -> None:
        assert self._data is not None
        await self._store.async_save(self._data)

    async def async_remove_entry(self, entry_id: str) -> None:
        """The entry is removed: forget what its syncs created (the events stay in amZettel)."""
        await self.async_load()
        assert self._data is not None
        if self._data.pop(entry_id, None) is not None:
            await self.async_save()


def sync_sources(hass: HomeAssistant) -> SyncSources:
    """The one store of this HA instance."""
    if DATA_SOURCES not in hass.data:
        hass.data[DATA_SOURCES] = SyncSources(hass)
    return hass.data[DATA_SOURCES]
