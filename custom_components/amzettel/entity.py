"""Entity lifecycle: entities follow the lists, calendars and meal plans that are shown."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


@callback
def track_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    platform: str,
    current_ids: Callable[[], Iterable[str]],
    unique_id: Callable[[str], str],
    factory: Callable[[str], Entity],
    async_add_entities: AddConfigEntryEntitiesCallback,
    subscribe: Callable[[Callable[[], None]], Callable[[], None]],
    authoritative: Callable[[], bool] = lambda: True,
) -> None:
    """Add entities for new objects and remove vanished ones from the entity registry.

    Runs once at setup (which also removes entities of objects deleted while HA was off) and on
    every coordinator update.
    """
    registry = er.async_get(hass)
    known: set[str] = set()

    @callback
    def _sync() -> None:
        if not authoritative():
            return
        ids = set(current_ids())
        wanted = {unique_id(i): i for i in ids}
        new = [i for i in ids if i not in known]
        if new:
            known.update(new)
            async_add_entities([factory(i) for i in new])
        for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
            if reg_entry.domain == platform and reg_entry.unique_id not in wanted:
                registry.async_remove(reg_entry.entity_id)
        known.intersection_update(ids)

    _sync()
    entry.async_on_unload(subscribe(_sync))
