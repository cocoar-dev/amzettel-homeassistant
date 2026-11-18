"""Constants of the amZettel integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "amzettel"

CLIENT_ID: Final = "amzettel-homeassistant"
DEFAULT_SERVER: Final = "https://app.amzettel.at"

INTEGRATION_KIND: Final = "homeassistant"

# entry.data keys
CONF_SERVER: Final = "server"
CONF_ISSUER: Final = "issuer"
CONF_AUTH_BASE: Final = "auth_base"
CONF_TOKEN_ENDPOINT: Final = "token_endpoint"
CONF_SCOPES: Final = "scopes"
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_EXPIRES_AT: Final = "expires_at"
CONF_USER_ID: Final = "user_id"  # my amZettel user id (Members[].UserId), from /api/auth/me
CONF_ACCOUNT_LABEL: Final = "account_label"  # confirmed e-mail, else name; redact in diagnostics

# config-flow area checkboxes
CONF_LISTS_READ: Final = "lists_read"
CONF_LISTS_WRITE: Final = "lists_write"
CONF_CALENDAR_READ: Final = "calendar_read"
CONF_CALENDAR_WRITE: Final = "calendar_write"
CONF_MEALPLANS_READ: Final = "mealplans_read"

BASE_SCOPES: Final = ("openid", "offline_access")

# Scope per checkbox — the wire names of IntegrationScopes in the backend (`<area>.read|write`).
AREA_SCOPES: Final[dict[str, str]] = {
    CONF_LISTS_READ: "lists.read",
    CONF_LISTS_WRITE: "lists.write",
    CONF_CALENDAR_READ: "calendar.read",
    CONF_CALENDAR_WRITE: "calendar.write",
    CONF_MEALPLANS_READ: "mealplans.read",
}

# Default selection: everything on. Writing calendars needs Premium; without it HA shows a repair.
AREA_DEFAULTS: Final[dict[str, bool]] = {
    CONF_LISTS_READ: True,
    CONF_LISTS_WRITE: True,
    CONF_CALENDAR_READ: True,
    CONF_CALENDAR_WRITE: True,
    CONF_MEALPLANS_READ: True,
}

SCOPE_LISTS_READ: Final = "lists.read"
SCOPE_LISTS_WRITE: Final = "lists.write"
SCOPE_CALENDAR_READ: Final = "calendar.read"
SCOPE_MEALPLANS_READ: Final = "mealplans.read"
SCOPE_MEALPLANS_WRITE: Final = "mealplans.write"

# Refresh the access token this long before it expires.
TOKEN_REFRESH_MARGIN: Final = 60

# Realtime
REALTIME_BACKOFF_MIN: Final = 2
REALTIME_BACKOFF_MAX: Final = 60

# List types (ListType in the backend)
LIST_TYPE_SHOPPING: Final = "Shopping"
LIST_TYPE_CHECKLIST: Final = "Checklist"
LIST_TYPE_TODO: Final = "Todo"
LIST_TYPE_EVENTS: Final = "Events"
TODO_LIST_TYPES: Final = (LIST_TYPE_SHOPPING, LIST_TYPE_CHECKLIST, LIST_TYPE_TODO)

PURCHASE_UID_PREFIX: Final = "purchase:"

# The category line of an event description ("Kategorie: Müll"), per language.
CATEGORY_LABEL: Final[dict[str, str]] = {"de": "Kategorie", "en": "Category"}
