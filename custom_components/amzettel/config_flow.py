"""Config flow: server + areas, then the OAuth device flow at modgud; reauth with the same account."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import instance_id
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .account import account_label, entry_title, updated_title
from .auth import (
    AuthEndpoints,
    DeviceCode,
    MemoryTokenStore,
    TokenManager,
    TokenSet,
    async_discover,
    async_poll_device_token,
    async_request_device_code,
    decode_jwt_payload,
    normalize_server,
)
from .const import (
    AREA_DEFAULTS,
    AREA_SCOPES,
    BASE_SCOPES,
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_LABEL,
    CONF_AUTH_BASE,
    CONF_EXPIRES_AT,
    CONF_ISSUER,
    CONF_REFRESH_TOKEN,
    CONF_SCOPES,
    CONF_SERVER,
    CONF_TOKEN_ENDPOINT,
    CONF_USER_ID,
    DEFAULT_SERVER,
    DOMAIN,
    INTEGRATION_KIND,
)
from .exceptions import (
    AmZettelError,
    AuthFailed,
    CannotConnect,
    DeviceAccessDenied,
    DeviceExpired,
    DeviceFlowError,
)
from .feed_api import FeedApi, FeedHttpError, FeedUnavailable
from .selection import (
    OPT_AUTO_ADD,
    Visibility,
    choices,
    options_from_input,
)
from .selection import schema as selection_schema

_LOGGER = logging.getLogger(__name__)


def scopes_for(selection: Mapping[str, Any]) -> list[str]:
    """`openid offline_access` + the checked areas (write implies read)."""
    scopes = list(BASE_SCOPES)
    for key, scope in AREA_SCOPES.items():
        if selection.get(key):
            if scope.endswith(".write"):
                read = scope.replace(".write", ".read")
                if read not in scopes:
                    scopes.append(read)
            if scope not in scopes:
                scopes.append(scope)
    return scopes


class AmZettelConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up an amZettel account."""

    VERSION = 1

    def __init__(self) -> None:
        self._server: str = DEFAULT_SERVER
        self._scopes: list[str] = []
        self._endpoints: AuthEndpoints | None = None
        self._device: DeviceCode | None = None
        self._poll_task: asyncio.Task[TokenSet] | None = None
        self._tokens: TokenSet | None = None
        self._failure: str | None = None
        self._data: dict[str, Any] | None = None
        self._manager: TokenManager | None = None
        self._api: FeedApi | None = None
        self._offered: dict[str, dict[str, str]] | None = None

    # ── user ───────────────────────────────────────────────────────────────────────────────

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._server = normalize_server(user_input[CONF_SERVER])
            self._scopes = scopes_for(user_input)
            if self._server != DEFAULT_SERVER:
                errors["base"] = "unsupported_server"
            elif len(self._scopes) == len(BASE_SCOPES):
                errors["base"] = "no_area"
            else:
                result = await self._async_start_device_flow(errors)
                if result is not None:
                    return result

        defaults = user_input or {CONF_SERVER: self._server, **AREA_DEFAULTS}
        schema = vol.Schema(
            {
                vol.Required(CONF_SERVER, default=defaults.get(CONF_SERVER, DEFAULT_SERVER)): str,
                **{vol.Optional(key, default=bool(defaults.get(key, AREA_DEFAULTS[key]))): bool for key in AREA_SCOPES},
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={"default_server": DEFAULT_SERVER},
        )

    async def _async_start_device_flow(self, errors: dict[str, str]) -> ConfigFlowResult | None:
        session = async_get_clientsession(self.hass)
        try:
            self._endpoints = await async_discover(session, self._server)
            self._device = await async_request_device_code(session, self._endpoints, self._scopes)
        except CannotConnect:
            errors["base"] = "cannot_connect"
            return None
        except DeviceFlowError:
            errors["base"] = "device_flow_failed"
            return None
        self._poll_task = None
        return await self.async_step_device()

    # ── device (progress) ──────────────────────────────────────────────────────────────────

    async def async_step_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._device is not None and self._endpoints is not None
        if self._poll_task is None:
            self._poll_task = self.hass.async_create_task(
                async_poll_device_token(
                    async_get_clientsession(self.hass), self._endpoints.token_endpoint, self._device
                ),
                "amzettel device flow",
            )
        if not self._poll_task.done():
            url = self._device.verification_uri_complete or self._device.verification_uri
            return self.async_show_progress(
                step_id="device",
                progress_action="wait_for_device",
                description_placeholders={
                    "user_code": self._device.user_code,
                    "url": url,
                },
                progress_task=self._poll_task,
            )
        try:
            self._tokens = self._poll_task.result()
        except DeviceAccessDenied:
            self._failure = "access_denied"
        except DeviceExpired:
            self._failure = "expired_token"
        except CannotConnect:
            self._failure = "cannot_connect"
        except DeviceFlowError:
            self._failure = "device_flow_failed"
        finally:
            self._poll_task = None
        if self._failure:
            return self.async_show_progress_done(next_step_id="failed")
        return self.async_show_progress_done(next_step_id="finish")

    async def async_step_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_abort(reason=self._failure or "device_flow_failed")

    # ── finish ─────────────────────────────────────────────────────────────────────────────

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._tokens is not None and self._endpoints is not None
        claims = decode_jwt_payload(self._tokens.access_token)
        sub = claims.get("sub")
        if not sub:
            return self.async_abort(reason="device_flow_failed")
        await self.async_set_unique_id(str(sub))

        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
        else:
            self._abort_if_unique_id_configured()

        session = async_get_clientsession(self.hass)
        manager = TokenManager(session, MemoryTokenStore(self._endpoints.token_endpoint, self._tokens))
        api = FeedApi(session, self._server, manager)
        try:
            # Proves that amZettel accepts the token (audience, client, grant) — the open `aud` question.
            me = await api.async_get_me()
        except AuthFailed:
            return self.async_abort(reason="token_rejected")
        except AmZettelError:
            return self.async_abort(reason="cannot_connect")
        try:
            await api.async_register_self(
                self.hass.config.location_name or "Home Assistant",
                INTEGRATION_KIND,
                await instance_id.async_get(self.hass),
            )
        except AuthFailed:
            return self.async_abort(reason="token_rejected")
        except AmZettelError as err:
            # The name is cosmetic ("Verbundene Systeme"); the connection works without it.
            _LOGGER.warning("amZettel: could not register this system's name (%s)", type(err).__name__)

        tokens = manager.tokens  # may have been refreshed by the two calls above
        data = {
            CONF_SERVER: self._server,
            CONF_ISSUER: self._endpoints.issuer,
            CONF_AUTH_BASE: self._endpoints.auth_base or self._endpoints.issuer,
            CONF_TOKEN_ENDPOINT: self._endpoints.token_endpoint,
            CONF_SCOPES: self._scopes,
            CONF_ACCESS_TOKEN: tokens.access_token,
            CONF_REFRESH_TOKEN: tokens.refresh_token,
            CONF_EXPIRES_AT: tokens.expires_at,
        }
        if isinstance(me, dict) and me.get("Id"):
            data[CONF_USER_ID] = str(me["Id"])
            data[CONF_ACCOUNT_LABEL] = account_label(me)
        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            title = updated_title(entry, data.get(CONF_ACCOUNT_LABEL))
            if entry.update_listeners:
                # Loaded: its update listener reloads it (HA warns when the flow reloads instead).
                return self.async_update_and_abort(entry, data=data, title=title)
            # Not loaded (setup failed on the old credentials): no listener, so the flow reloads.
            return self.async_update_reload_and_abort(entry, data=data, title=title)

        self._data = data
        self._manager = manager
        self._api = api
        return await self.async_step_select()

    async def async_step_select(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Last step of the first setup: which lists, calendars and meal plans appear (default: all).

        The offer is read once, with the fresh tokens, and kept: saving uses exactly what was shown.
        A network error or 5xx shows the form with an error; submitting it tries again.
        """
        assert self._data is not None and self._api is not None and self._manager is not None
        if self._offered is None:
            try:
                snapshot = await self._api.async_snapshot()
                self._offered = choices(
                    snapshot.lists.values(), snapshot.plans.values(),
                    user_id=self._data.get(CONF_USER_ID), language=self.hass.config.language,
                )
            except AuthFailed:
                return self.async_abort(reason="token_rejected")
            except AmZettelError as err:
                error = "cannot_connect"
                if isinstance(err, FeedHttpError) and err.status == 429:
                    error = "feed_snapshot_limit"
                elif isinstance(err, FeedUnavailable):
                    error = "feed_unavailable"
                return self.async_show_form(
                    step_id="select", data_schema=vol.Schema({}), errors={"base": error}
                )
            user_input = None  # first display of the real form (also after a retry)
        if user_input is not None:
            tokens = self._manager.tokens  # current: the reads above may have refreshed them
            data = {
                **self._data,
                CONF_ACCESS_TOKEN: tokens.access_token,
                CONF_REFRESH_TOKEN: tokens.refresh_token,
                CONF_EXPIRES_AT: tokens.expires_at,
            }
            return self.async_create_entry(
                title=entry_title(self._server, data.get(CONF_ACCOUNT_LABEL)),
                data=data,
                options=options_from_input(user_input, self._offered, self._scopes),
            )
        return self.async_show_form(
            step_id="select",
            data_schema=selection_schema(self._offered, self._scopes, Visibility(None), auto_add=True),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """„Konfigurieren“: choose which objects appear."""
        return AmZettelOptionsFlow()

    # ── reauth ─────────────────────────────────────────────────────────────────────────────

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._server = entry_data[CONF_SERVER]
        if self._server != DEFAULT_SERVER:
            return self.async_abort(reason="unsupported_server")
        self._scopes = list(entry_data.get(CONF_SCOPES) or scopes_for(AREA_DEFAULTS))
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._async_start_device_flow(errors)
            if result is not None:
                return result
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"server": self._server},
        )


class AmZettelOptionsFlow(OptionsFlow):
    """Which lists, calendars and meal plans appear — the areas (scopes) need a new sign-in."""

    def __init__(self) -> None:
        self._offered: dict[str, dict[str, str]] | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")
        coordinator = entry.runtime_data.coordinator
        scopes = list(entry.data.get(CONF_SCOPES, []))
        visibility = Visibility(entry.options)
        if user_input is not None and self._offered is not None:
            # Exactly what was shown: an object that appeared meanwhile stays "new" (not known).
            return self.async_create_entry(
                data=options_from_input(user_input, self._offered, scopes, previous=entry.options)
            )
        if not coordinator.last_update_success:
            return self.async_abort(reason="cannot_connect")
        data = coordinator.data
        self._offered = choices(
            data.lists.values(), data.meal_plans.values(), visibility, coordinator.user_id, self.hass.config.language
        )
        return self.async_show_form(
            step_id="init",
            description_placeholders={
                "account": entry.data.get(CONF_ACCOUNT_LABEL) or "—",
                "server": entry.data[CONF_SERVER],
            },
            data_schema=selection_schema(
                self._offered, scopes, visibility, auto_add=bool(entry.options.get(OPT_AUTO_ADD, True))
            ),
        )
