"""Diagnostics without tokens, e-mail addresses or content."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import AmZettelConfigEntry
from .const import CONF_ACCESS_TOKEN, CONF_ACCOUNT_LABEL, CONF_REFRESH_TOKEN

TO_REDACT = {CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, CONF_ACCOUNT_LABEL, "title", "unique_id", "user_id"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: AmZettelConfigEntry) -> dict[str, Any]:
    """Shape and health of the connection — ids, types and counts, no names or tokens."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    data = coordinator.data
    return {
        "entry": async_redact_data(
            {"title": entry.title, "unique_id": entry.unique_id, "data": dict(entry.data)}, TO_REDACT
        ),
        "token_expires_at": runtime.tokens.tokens.expires_at,
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "last_change_area": coordinator.last_change_area,
        },
        "realtime": {
            "connected": runtime.realtime.connected,
            "ready_count": runtime.realtime.ready_count,
            "connect_attempts": runtime.realtime.connect_attempts,
            "protocol": "sse-v1",
            "last_error": runtime.realtime.last_error,
        },
        "lists": [
            {
                "id": list_id,
                "type": view.get("Type") or "Shopping",
                "items": len(view.get("Items") or []),
                "recent_purchases": len(view.get("RecentPurchases") or []),
                "recurring": sum(1 for i in view.get("Items") or [] if i.get("Recurrence")),
            }
            for list_id, view in (data.lists.items() if data else [])
        ],
        "meal_plans": [
            {"id": plan_id, "entries": len(plan.get("Entries") or []), "slots": len(plan.get("Slots") or [])}
            for plan_id, plan in (data.meal_plans.items() if data else [])
        ],
    }
