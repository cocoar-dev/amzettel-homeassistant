"""Coordinate the REST snapshot, SSE cursor and versioned writes for one HA entry."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .account import update_account
from .const import CONF_USER_ID, DOMAIN
from .exceptions import AmZettelError, ApiError, AuthFailed, NotFound, NotPermitted, PremiumRequired
from .feed_api import FeedApi, FeedHttpError, FeedUnavailable
from .selection import KIND_CALENDARS, KIND_LISTS, KIND_MEAL_PLANS, Visibility


@dataclass(slots=True)
class AmZettelData:
    """What the account sees right now."""

    lists: dict[str, dict[str, Any]] = field(default_factory=dict)
    meal_plans: dict[str, dict[str, Any]] = field(default_factory=dict)
    baseline: bool = False
    authoritative: bool = True


_LOGGER = logging.getLogger(__name__)
SELECTION_KIND = {"Lists": KIND_LISTS, "Calendar": KIND_CALENDARS, "MealPlans": KIND_MEAL_PLANS}
OPS = {
    "Events": dict(add="AddEvent", edit="PatchEvent", remove="RemoveEvent"),
    "Lists": dict(add="AddItem", edit="PatchItem", remove="RemoveItem", buy="Buy", unbuy="Unbuy", done="SetDone"),
}


class AmZettelCoordinator(DataUpdateCoordinator[AmZettelData]):
    """State and cursor are in memory; a reload starts a new baseline."""

    config_entry: ConfigEntry
    access_generation = 0
    api: FeedApi
    areas: tuple[str, ...] = ()
    cursor: str | None = None
    revoked = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: FeedApi, scopes: list[str]) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN)
        self.api = api
        self.scopes = scopes
        self.last_change_area: str | None = None
        self.visibility = Visibility(entry.options)
        self.user_id: str | None = entry.data.get(CONF_USER_ID)
        self._lock = asyncio.Lock()
        self.operations: dict[str, frozenset[str]] = {}

    @property
    def writes_lists(self) -> bool:
        return "lists.write" in self.scopes and bool(self.operations.get("Lists"))

    @property
    def writes_calendars(self) -> bool:
        return "calendar.write" in self.scopes and bool(self.operations.get("Calendar"))

    async def _baseline(self) -> AmZettelData:
        snap = await self.api.async_snapshot()
        self.areas, self.operations, self.cursor = snap.areas, snap.operations, snap.cursor
        return AmZettelData(lists=snap.lists, meal_plans=snap.plans, baseline=True)

    async def _async_update_data(self) -> AmZettelData:
        async with self._lock:
            try:
                self.user_id = await self._fetch_account()
                return await self._baseline()
            except AuthFailed as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except FeedUnavailable as err:
                raise UpdateFailed(translation_domain=DOMAIN, translation_key="feed_unavailable") from err
            except AmZettelError as err:
                raise UpdateFailed(str(err)) from err

    async def async_resnapshot(self) -> None:
        async with self._lock:
            if not self.revoked:
                self.async_set_updated_data(await self._baseline())

    @callback
    def hide_feed_data(self) -> None:
        """Invalidate data but keep the entity registry until a complete baseline."""
        self.access_generation += 1
        self.cursor = None
        self.operations = {}
        self.async_set_updated_data(AmZettelData(baseline=True, authoritative=False))

    @callback
    def revoke_feed(self) -> None:
        if not self.revoked:
            self.revoked = True
            self.hide_feed_data()
            self._start_reauth()

    @callback
    def apply_checkpoint(self, cursor: str) -> None:
        if self.cursor is None:
            raise AmZettelError("new integration snapshot required")
        self.cursor = cursor

    async def _pull(self, area: str, ident: str, cursor: str | None = None) -> dict[str, Any]:
        """Read and install under the coordinator lock, then commit the cursor."""
        generation = self.access_generation
        try:
            view = await self.api.async_object(area, ident)
        except AuthFailed:
            self.revoke_feed()
            raise
        except (NotFound, NotPermitted):
            self.hide_feed_data()
            raise
        except FeedHttpError as err:
            if err.status == 410:
                self.hide_feed_data()
            raise
        if self.access_generation != generation or self.cursor is None:
            raise AmZettelError("integration state changed during read")
        data = self._snapshot()
        (data.meal_plans if area == "MealPlans" else data.lists)[ident] = view
        self.async_set_updated_data(data)
        if cursor:
            self.cursor = cursor
        return view

    async def async_apply_change(self, area: str, ident: str, cursor: str) -> None:
        async with self._lock:
            if self.cursor is None:
                raise AmZettelError("new integration snapshot required")
            if cursor == self.cursor:
                return
            if not self.visibility.is_visible(SELECTION_KIND[area], ident):
                self.cursor = cursor
                return
            await self._pull(area, ident, cursor)
            self.last_change_area = area

    async def async_pull_list(self, list_id: str) -> dict[str, Any] | None:
        async with self._lock:
            view = self.data.lists.get(list_id)
            if view is None:
                return None
            try:
                return await self._pull("Calendar" if view["Type"] == "Events" else "Lists", list_id)
            except (NotFound, NotPermitted, PremiumRequired):
                return None

    async def async_mutate_item(
        self, view: dict[str, Any], operation: str, ident: str, body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        async with self._lock:
            calendar = view["Type"] == "Events"
            area = "Calendar" if calendar else "Lists"
            name = OPS["Events" if calendar else "Lists"].get(operation)
            if self.cursor is None or view["Id"] not in self.data.lists:
                raise NotFound(404, "integration_object_unavailable")
            if name is None or name not in self.operations.get(area, ()):
                raise NotPermitted(403, "integration_not_permitted")
            try:
                result = await self.api.async_mutate(view, operation, ident, body)
            except AuthFailed:
                self.revoke_feed()
                raise
            except ApiError as err:
                if err.status == 412:
                    try:
                        await self._pull(area, view["Id"])
                    except AuthFailed:
                        raise
                    except AmZettelError:
                        pass  # Preserve the conflict; the feed will recover the current state.
                raise
            # Commit is confirmed; do not invite a second write if a subsequent read fails.
            try:
                await self._pull(area, view["Id"])
            except AmZettelError:
                _LOGGER.debug("amZettel: feed will reconcile after a committed write")
            return result

    async def _fetch_account(self) -> str | None:
        try:
            me = await self.api.async_get_me()
        except AuthFailed:
            raise
        except AmZettelError as err:
            _LOGGER.debug("account profile unavailable: %s", type(err).__name__)
            return self.user_id  # a cosmetic profile failure must not hide usable objects
        if isinstance(me, dict) and me.get("Id"):
            update_account(self.hass, self.config_entry, me)
            return str(me["Id"])
        return self.user_id

    def _snapshot(self) -> AmZettelData:
        data = self.data or AmZettelData()
        return AmZettelData(lists=dict(data.lists), meal_plans=dict(data.meal_plans))

    def _start_reauth(self) -> None:
        self.config_entry.async_start_reauth(self.hass)
