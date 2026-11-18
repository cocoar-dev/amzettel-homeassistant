"""Every write to amZettel goes through `async_write`: one mapping of refusals onto HA errors, and a
repair per entry and area when the plan or the connection refuses writing.

A refusal by the plan (`integration_premium_required`) or by the connection's grant
(`integration_not_permitted` and the gate's other titles) is not a passing failure — every following
write of that area fails the same way. So besides the error of the call, HA shows a repair; the next
successful write in the same area removes it again.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, Final, TypeVar

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .exceptions import AmZettelError, ApiError, AuthFailed, NotFound, NotPermitted, PremiumRequired

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import AmZettelCoordinator

T = TypeVar("T")


async def async_item_mutation(
    coordinator: AmZettelCoordinator,
    view: dict[str, Any],
    operation: str,
    ident: str,
    body: dict[str, Any] | None = None,
    *,
    previous: dict[str, Any] | None = None,
) -> Any:
    """Apply the requested fields against the exact read version."""
    if operation == "buy":
        body = None  # The server derives the purchase ID from the mutation key.
    if body is not None:
        body = dict(body)
        body.pop("Id", None)
        if view["Type"] == "Events":
            body.pop("Quantity", None)
        if operation == "edit" and isinstance(due := body.get("Due"), dict):
            before = (previous or {}).get("Due") or {}
            fields = ("Date", "Time", "Zone", "EndDate", "EndTime")
            due = {key: due.get(key) for key in fields if due.get(key) != before.get(key)}
            if due:
                body["Due"] = due
            else:
                body.pop("Due")
        if operation == "edit" and not body:
            return None
        if isinstance(rule := body.get("Recurrence"), dict):
            body["Recurrence"] = {
                "Rule": rule["Rule"],
                "RDates": rule.get("RDateLines") or [],
                "ExDates": rule.get("ExDateLines") or [],
            }
    return await coordinator.async_mutate_item(view, operation, ident, body)


AREA_LISTS: Final = "lists"
AREA_CALENDAR: Final = "calendar"

ISSUE_PREMIUM: Final = "write_premium_required"
ISSUE_NOT_PERMITTED: Final = "write_not_permitted"


def issue_id(entry: ConfigEntry, area: str) -> str:
    """One repair per entry and area."""
    return f"{entry.entry_id}_{area}_write"


def _raise_repair(hass: HomeAssistant, entry: ConfigEntry, area: str, key: str) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(entry, area),
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=f"{key}_{area}",
        translation_placeholders={"account": entry.title},
    )


async def async_write(
    hass: HomeAssistant,
    entry: ConfigEntry,
    area: str,
    call: Awaitable[T],
    *,
    gone_key: str = "item_gone",
) -> T:
    """Await one write; success clears the area's repair, a plan or grant refusal raises one."""
    try:
        result = await call
    except PremiumRequired as err:
        _raise_repair(hass, entry, area, ISSUE_PREMIUM)
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="premium_required") from err
    except NotPermitted as err:
        if err.is_gate:
            _raise_repair(hass, entry, area, ISSUE_NOT_PERMITTED)
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="not_permitted") from err
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="refused",
            translation_placeholders={"reason": err.server_message},
        ) from err
    except NotFound as err:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key=gone_key) from err
    except AuthFailed as err:
        if not getattr(getattr(entry.runtime_data, "coordinator", None), "revoked", False):
            entry.async_start_reauth(hass)
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="auth_failed") from err
    except AmZettelError as err:
        if isinstance(err, ApiError) and err.status == 412:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="write_conflict") from err
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="api_error",
            translation_placeholders={"error": str(err)},
        ) from err
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry, area))
    return result


async def async_current_list(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: AmZettelCoordinator, list_id: str,
) -> dict[str, Any]:
    """The list as the server has it NOW; a failed pull raises and nothing is written."""
    try:
        view = await coordinator.async_pull_list(list_id)
    except AuthFailed as err:
        if not getattr(coordinator, "revoked", False):
            entry.async_start_reauth(hass)
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="auth_failed") from err
    except AmZettelError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="refresh_failed", translation_placeholders={"error": str(err)}
        ) from err
    if view is None:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="list_gone")
    return view


def clear_repairs(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The entry is removed: its repairs go with it."""
    for area in (AREA_LISTS, AREA_CALENDAR):
        ir.async_delete_issue(hass, DOMAIN, issue_id(entry, area))
