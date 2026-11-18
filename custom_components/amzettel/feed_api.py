"""The public integrations REST contract."""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import aiohttp

from .api import REQUEST_TIMEOUT, AmZettelApi
from .auth import TokenManager
from .const import DEFAULT_SERVER
from .exceptions import AmZettelError, ApiError, AuthFailed, CannotConnect, NotFound

_LOGGER = logging.getLogger(__name__)
PREFIX = "/api/integrations/v1"
KINDS = {"Lists": "List", "Calendar": "Calendar", "MealPlans": "MealPlan"}
MAX_BODY = 16 * 1024 * 1024
MAX_OBJECTS = 5000


class FeedProtocolError(CannotConnect):
    """An invalid wire response; never use it as an empty result."""


class FeedUnavailable(CannotConnect):
    """The server does not expose the public integrations API."""


class FeedHttpError(ApiError):
    """A feed response with optional reconnect delay."""

    def __init__(self, status: int, title: str | None, retry_after: str | None) -> None:
        super().__init__(status, title)
        self.delay = min(int(retry_after), 300) if retry_after and re.fullmatch(r"[0-9]{1,10}", retry_after) else 0


def wire_dict(data: Any, *, versioned: bool = False) -> dict[str, Any]:
    if not isinstance(data, dict) or (versioned and (type(data.get("Version")) is not int or data["Version"] != 1)):
        raise FeedProtocolError("invalid integration API object")
    return data


def opaque(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[\x21-\x7e]{1,4096}", value):
        raise FeedProtocolError("invalid integration API token")
    return value


def object_id(value: Any) -> str:
    try:
        if isinstance(value, str) and str(uuid.UUID(value)) == value and uuid.UUID(value).int:
            return value
    except ValueError:
        pass
    raise FeedProtocolError("invalid integration API object ID")


def map_object(raw: Any, areas: tuple[str, ...]) -> dict[str, Any]:
    obj = wire_dict(raw)
    area = obj.get("Area")
    if not isinstance(area, str) or area not in areas or obj.get("Kind") != KINDS[area]:
        raise FeedProtocolError("unexpected integration API area or kind")
    view = dict(wire_dict(obj.get("Data")))
    etag = opaque(obj.get("ETag"))
    if not re.fullmatch(r'"[^"]+"', etag):
        raise FeedProtocolError("invalid integration API ETag")
    view.update(Id=object_id(obj.get("Id")), ETag=etag)
    if not isinstance(view.get("IsOwner"), bool) or not isinstance(view.get("Name"), str):
        raise FeedProtocolError("invalid integration API container")
    if area == "Calendar":
        view["Type"] = "Events"
        view["Items"] = view.pop("Events", None)
    elif area == "Lists" and view.get("Type") not in ("Shopping", "Checklist", "Todo"):
        raise FeedProtocolError("invalid integration API list type")
    required_arrays = {
        "Calendar": ("Items", "EventCategories", "Persons"),
        "Lists": ("Items", "RecentPurchases"),
        "MealPlans": ("Entries", "Slots"),
    }[area]
    for field in required_arrays:
        if not isinstance(view.get(field), list) or any(not isinstance(i, dict) for i in view[field]):
            raise FeedProtocolError("invalid integration API collection")
    if area == "Calendar":
        view["Items"] = [
            {**item, "Recurrence": {
                "Rule": rec.get("Rule"), "RDateLines": rec.get("RDates"), "ExDateLines": rec.get("ExDates"),
            }} if isinstance(rec := item.get("Recurrence"), dict) else item
            for item in view["Items"]
        ]
    return view


@dataclass(frozen=True, slots=True)
class Snapshot:
    areas: tuple[str, ...]
    operations: dict[str, frozenset[str]]
    lists: dict[str, dict[str, Any]]
    plans: dict[str, dict[str, Any]]
    cursor: str | None


class FeedApi(AmZettelApi):
    """Public v1 data, with the general identity and registration endpoints."""

    def __init__(self, session: aiohttp.ClientSession, server: str, tokens: TokenManager) -> None:
        if server.rstrip("/") != DEFAULT_SERVER:
            raise FeedProtocolError("integration API v1 requires the official amZettel server")
        super().__init__(session, server, tokens)

    async def headers(self, *, refresh: bool = False) -> dict[str, str]:
        token = await self._tokens.async_get_access_token(force_refresh=refresh)
        return {"Authorization": f"Bearer {token}", "X-AmZettel-Api-Version": "1"}

    @staticmethod
    async def response(response: aiohttp.ClientResponse) -> Any:
        raw = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            raw.extend(chunk)
            if len(raw) > MAX_BODY:
                raise FeedProtocolError("integration API response too large")
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            body = None
        if response.status in (400, 410, 429, 503):
            title = body.get("title") if isinstance(body, dict) else None
            raise FeedHttpError(response.status, title, response.headers.get("Retry-After"))
        return AmZettelApi._result(response.status, body)

    async def request(
        self, method: str, path: str, body: Any = None, *, mutation: dict[str, str] | None = None,
    ) -> Any:
        refreshed = retried = False
        headers = await self.headers()
        while True:
            try:
                async with self._session.request(
                    method, self.server + PREFIX + path, json=body,
                    headers={**headers, "Accept": "application/json", **(mutation or {})},
                    timeout=REQUEST_TIMEOUT, allow_redirects=False,
                ) as resp:
                    if resp.status != 401 or refreshed:
                        return await self.response(resp)
            except (aiohttp.ClientError, TimeoutError) as err:
                if mutation and not retried:
                    retried = True  # Same request and preconditions, including the operation UUID.
                    continue
                raise CannotConnect("integration API request failed") from err
            refreshed = True
            headers = await self.headers(refresh=True)

    async def async_snapshot(self) -> Snapshot:
        try:
            caps = wire_dict(await self.request("GET", "/capabilities"), versioned=True)
        except NotFound as err:
            raise FeedUnavailable("REST/SSE v1 is not available on this server") from err
        rows = caps.get("Areas")
        if not isinstance(rows, list):
            raise FeedProtocolError("invalid integration capabilities")
        readable: set[str] = set()
        operations: dict[str, frozenset[str]] = {}
        for row in rows:
            cap = wire_dict(row)
            area = cap.get("Area")
            if not isinstance(area, str) or area not in KINDS:
                continue
            ops = cap.get("Operations")
            if not isinstance(ops, list) or any(not isinstance(op, str) for op in ops):
                raise FeedProtocolError("invalid integration operations")
            if cap.get("Read") is True:
                readable.add(area)
            if cap.get("Write") is True:
                operations[area] = frozenset(ops)
        areas = tuple(sorted(readable))
        lists: dict[str, dict[str, Any]] = {}
        plans: dict[str, dict[str, Any]] = {}
        if not areas:
            return Snapshot(areas, operations, lists, plans, None)
        page = wire_dict(await self.request("POST", "/snapshots", {"Areas": list(areas)}), versioned=True)
        # A complete first page has no server-side reservation. For a paged snapshot,
        # keep its first page token even if a later page fails or the entry is unloaded.
        release_path = (
            "/snapshots/" + quote(opaque(page.get("NextPageToken")), safe="") if page.get("Complete") is False else None
        )
        complete = False
        try:
            cursor = await self._read_snapshot_pages(page, areas, lists, plans)
            complete = True
        finally:
            if release_path is not None:
                try:
                    await self.request("DELETE", release_path)
                except AuthFailed:
                    if complete:
                        raise  # Do not publish a snapshot after discovering a revocation.
                except AmZettelError as err:
                    _LOGGER.debug("amZettel: snapshot release failed (%s); falls back to expiry", type(err).__name__)
        return Snapshot(areas, operations, lists, plans, cursor)

    async def _read_snapshot_pages(
        self,
        page: dict[str, Any],
        areas: tuple[str, ...],
        lists: dict[str, dict[str, Any]],
        plans: dict[str, dict[str, Any]],
    ) -> str:
        cursor = opaque(page.get("Cursor"))
        seen = set()
        snapshot_bytes = 0
        while True:
            if page.get("Cursor") != cursor or not isinstance(page.get("Complete"), bool):
                raise FeedProtocolError("inconsistent integration snapshot")
            objects = page.get("Objects")
            if not isinstance(objects, list) or len(objects) + len(lists) + len(plans) > MAX_OBJECTS:
                raise FeedProtocolError("invalid integration snapshot size")
            snapshot_bytes += len(json.dumps(objects, separators=(",", ":")))
            if snapshot_bytes > MAX_BODY:
                raise FeedProtocolError("integration snapshot too large")
            for obj in objects:
                view = map_object(obj, areas)
                ident = view["Id"]
                if ident in lists or ident in plans:
                    raise FeedProtocolError("duplicate integration snapshot object")
                target = plans if obj["Area"] == "MealPlans" else lists
                target[ident] = view
            if page["Complete"]:
                if page.get("NextPageToken") is not None:
                    raise FeedProtocolError("unexpected integration snapshot token")
                return cursor
            token = opaque(page.get("NextPageToken"))
            if token in seen or len(seen) >= MAX_OBJECTS:
                raise FeedProtocolError("integration snapshot pagination loop")
            seen.add(token)
            page = wire_dict(await self.request("GET", "/snapshots/" + quote(token, safe="")), versioned=True)

    async def async_object(self, area: str, ident: str) -> dict[str, Any]:
        view = map_object(await self.request("GET", f"/objects/{KINDS[area]}/{object_id(ident)}"), (area,))
        if view["Id"] != ident:
            raise FeedProtocolError("wrong integration API object ID")
        return view

    async def async_mutate(
        self, view: dict[str, Any], operation: str, ident: str, body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        calendar = view["Type"] == "Events"
        base = f"/{'calendars' if calendar else 'lists'}/{object_id(view['Id'])}"
        collection = "events" if calendar else "items"
        path = f"{base}/{collection}/{object_id(ident)}"
        method = {"add": "PUT", "edit": "PATCH", "remove": "DELETE", "done": "PUT", "buy": "POST", "unbuy": "POST"}[
            operation
        ]
        if operation in ("done", "buy"):
            path += f"/{operation}"
        elif operation == "unbuy":
            path = f"{base}/purchases/{ident}/unbuy"
        answer = wire_dict(await self.request(
            method, path, body, mutation={"If-Match": view["ETag"], "Idempotency-Key": str(uuid.uuid4())},
        ))
        return {"id": object_id(answer.get("Id"))}
