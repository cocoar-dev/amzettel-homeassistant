"""Errors of the amZettel client (shared by auth, api and realtime)."""

from __future__ import annotations


class AmZettelError(Exception):
    """Base error."""


class CannotConnect(AmZettelError):
    """The server or the identity provider could not be reached."""


class AuthFailed(AmZettelError):
    """The credentials are no longer valid — a reauthentication is needed.

    401, `integration_revoked`, `invalid_grant` on refresh, or a realtime stream the
    server ended as unauthorized.
    """


class ApiError(AmZettelError):
    """Any other refusal of the API."""

    def __init__(
        self, status: int, title: str | None = None, detail: str | None = None, code: str | None = None
    ) -> None:
        super().__init__(f"{status} {title or code or ''}".strip())
        self.status = status
        self.title = title
        self.detail = detail
        self.code = code

    @property
    def server_message(self) -> str:
        """What the server said, for a user-facing message."""
        return self.detail or self.code or self.title or str(self.status)


class NotFound(ApiError):
    """404 — the object is gone or no longer visible."""


class NotPermitted(ApiError):
    """403 — refused. `is_gate` = the integration gate refused (grant / token scopes)."""

    @property
    def is_gate(self) -> bool:
        """One of the gate's titles (IntegrationGate / IntegrationContext.Refusal)."""
        return (self.title or "").startswith("integration_")


class PremiumRequired(ApiError):
    """403 `integration_premium_required` — writing this area needs Premium."""


# Device flow outcomes
class DeviceFlowError(AmZettelError):
    """The device authorization did not succeed."""


class DeviceAccessDenied(DeviceFlowError):
    """The user declined the request."""


class DeviceExpired(DeviceFlowError):
    """The user code expired before the user confirmed."""
