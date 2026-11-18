"""A small in-memory amZettel behind the public integrations API v1.

It answers capabilities, one complete snapshot page, single objects and the item/event mutations
the way the backend does: client-chosen ids in the path (`PUT` of an existing id is a no-op), `PATCH`
changes only the fields it names, every mutation needs the current `If-Match` ETag (`412` otherwise)
and an `Idempotency-Key`, and every committed change gives the object a new ETag.

`writes` records `(operation, id, body)` with operation one of add, edit, remove, done, buy, unbuy;
`requests` keeps `(method, path, headers)` of each mutation for checks on the wire.
"""

from __future__ import annotations

import asyncio
import copy
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from typing import Any
from unittest.mock import patch

from aiohttp import web
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from .conftest import SCOPES, SERVER, USER_ID
from .data import KIND, area_of, lists, plans, wire

FEED = f"{SERVER}/api/integrations/v1"
OPERATIONS = {
    "Lists": ["AddItem", "PatchItem", "RemoveItem", "SetDone", "Buy", "Unbuy"],
    "Calendar": ["AddEvent", "PatchEvent", "RemoveEvent"],
    "MealPlans": [],
}
READ_SCOPES = {"Lists": "lists.read", "Calendar": "calendar.read", "MealPlans": "mealplans.read"}
WRITE_SCOPES = {"Lists": "lists.write", "Calendar": "calendar.write"}


def _clean(value: Any) -> Any:
    return value.strip() if isinstance(value, str) and value.strip() else (None if isinstance(value, str) else value)


def _recurrence(rule: Any) -> Any:
    """Wire `RDates`/`ExDates` → the view's `RDateLines`/`ExDateLines`."""
    if not isinstance(rule, dict):
        return rule
    return {"Rule": rule.get("Rule"), "RDateLines": rule.get("RDates"), "ExDateLines": rule.get("ExDates")}


def _json(method: str, url: Any, status: int = HTTPStatus.OK, body: Any = None) -> AiohttpClientMockResponse:
    return AiohttpClientMockResponse(method, url, status=status, json=body if body is not None else {})


class FakeServer:
    """`scopes` decide the capabilities (as the grant does); `refuse` = the 403 (a title or a whole
    problem body) every write answers."""

    def __init__(
        self,
        aioclient_mock: AiohttpClientMocker,
        views: list[dict[str, Any]] | None = None,
        meal_plans: list[dict[str, Any]] | None = None,
        *,
        scopes: list[str] | None = None,
    ) -> None:
        self._mock = aioclient_mock
        self.lists = {str(v["Id"]): v for v in copy.deepcopy(views if views is not None else lists())}
        self.plans = {str(p["Id"]): p for p in copy.deepcopy(meal_plans if meal_plans is not None else plans())}
        self.scopes = list(scopes if scopes is not None else SCOPES)
        self.writes: list[tuple[str, str, Any]] = []
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.refuse: str | dict[str, Any] | None = None
        self.fail_reads = False  # GET of one object answers 503
        self.slow_writes = 0  # event-loop turns a write takes before it is applied
        self.capabilities_error: tuple[int, str] | None = None  # e.g. (404, …): v1 not deployed there
        self.snapshots = 0
        self.cursor = "cursor-1"
        self._etag = 0
        base = re.escape(FEED)
        aioclient_mock.get(re.compile(f"^{base}/capabilities$"), side_effect=self._capabilities)
        aioclient_mock.post(re.compile(f"^{base}/snapshots$"), side_effect=self._snapshot)
        aioclient_mock.get(re.compile(f"^{base}/objects/[^/]+/[^/]+$"), side_effect=self._object)
        container = f"^{base}/(lists|calendars)/[^/]+/"
        aioclient_mock.put(re.compile(f"{container}(items|events)/[^/]+$"), side_effect=self._add)
        aioclient_mock.patch(re.compile(f"{container}(items|events)/[^/]+$"), side_effect=self._edit)
        aioclient_mock.delete(re.compile(f"{container}(items|events)/[^/]+$"), side_effect=self._remove)
        aioclient_mock.put(re.compile(f"{container}items/[^/]+/done$"), side_effect=self._done)
        aioclient_mock.post(re.compile(f"{container}items/[^/]+/buy$"), side_effect=self._buy)
        aioclient_mock.post(re.compile(f"{container}purchases/[^/]+/unbuy$"), side_effect=self._unbuy)

    # ── state ──────────────────────────────────────────────────────────────────────────────

    def items(self, list_id: str) -> list[dict[str, Any]]:
        return self.lists[list_id]["Items"]

    def item(self, list_id: str, item_id: str) -> dict[str, Any] | None:
        return next((i for i in self.items(list_id) if i["Id"] == item_id), None)

    def touch(self, list_id: str) -> None:
        """A change made on the server (another client): the object gets a new ETag."""
        self._etag += 1
        self.lists[list_id]["ETag"] = f'"server-{self._etag}"'

    def readable(self) -> list[str]:
        return sorted(area for area, scope in READ_SCOPES.items() if scope in self.scopes)

    def operations(self, area: str) -> list[str]:
        scope = WRITE_SCOPES.get(area)
        return list(OPERATIONS[area]) if scope in self.scopes else []

    # ── reads ──────────────────────────────────────────────────────────────────────────────

    async def _capabilities(self, method, url, data):
        if self.capabilities_error is not None:
            status, title = self.capabilities_error
            return _json(method, url, status, {"title": title})
        areas = [
            {
                "Area": area,
                "Read": area in self.readable(),
                "Write": bool(self.operations(area)),
                "Operations": self.operations(area),
            }
            for area in ("Lists", "Calendar", "MealPlans")
        ]
        areas.append({"Area": "Recipes", "Read": False, "Write": False, "Operations": []})  # not shown in HA
        return _json(method, url, body={"Version": 1, "Areas": areas})

    def objects(self, areas: list[str]) -> list[dict[str, Any]]:
        views = [*self.lists.values(), *self.plans.values()]
        return [wire(v) for v in views if area_of(v) in areas and not v.get("ArchivedAt")]

    async def _snapshot(self, method, url, data):
        self.snapshots += 1
        areas = list((data or {}).get("Areas") or [])
        assert set(areas) <= set(self.readable()), areas
        return _json(method, url, body={
            "Version": 1, "Cursor": self.cursor, "Complete": True, "Objects": self.objects(areas),
        })

    async def _object(self, method, url, data):
        if self.fail_reads:
            return _json(method, url, HTTPStatus.SERVICE_UNAVAILABLE, {"title": "unavailable"})
        kind, ident = str(url.path).strip("/").split("/")[4:6]
        view = self.lists.get(ident) or self.plans.get(ident)
        if view is None or view.get("ArchivedAt") or KIND[area_of(view)] != kind:
            return _json(method, url, HTTPStatus.NOT_FOUND, {"title": "integration_object_unavailable"})
        if area_of(view) not in self.readable():
            return _json(method, url, HTTPStatus.FORBIDDEN, {"title": "integration_not_permitted"})
        return _json(method, url, body=wire(view))

    # ── writes ─────────────────────────────────────────────────────────────────────────────

    async def _begin(self, method, url, operation: str, ident: str, body: Any) -> tuple[Any, Any]:
        """Check the preconditions when the write is applied; returns (view, refusal)."""
        headers = self._mock.mock_calls[-1][3] or {}  # this request: recorded just before its side effect
        self.requests.append((method.upper(), str(url.path), dict(headers)))
        for _ in range(self.slow_writes):  # let other calls run meanwhile (no real sleep)
            await asyncio.sleep(0)
        list_id, _ = self._ids(url)
        view = self.lists.get(list_id)
        if self.refuse is not None:
            body = self.refuse if isinstance(self.refuse, dict) else {"title": self.refuse}
            return view, _json(method, url, HTTPStatus.FORBIDDEN, body)
        if view is None:
            return view, _json(method, url, HTTPStatus.NOT_FOUND, {"title": "integration_object_unavailable"})
        if not headers.get("Idempotency-Key"):
            return view, _json(method, url, HTTPStatus.BAD_REQUEST, {"title": "idempotency_key_required"})
        if headers.get("If-Match") != view["ETag"]:
            return view, _json(method, url, HTTPStatus.PRECONDITION_FAILED, {"title": "precondition_failed"})
        self.writes.append((operation, ident, copy.deepcopy(body)))
        return view, None

    def _commit(self, method, url, view: dict[str, Any], ident: str) -> AiohttpClientMockResponse:
        self.touch(view["Id"])
        return _json(method, url, body={"Id": ident})

    @staticmethod
    def _ids(url: Any) -> tuple[str, str]:
        parts = str(url.path).strip("/").split("/")  # api integrations v1 lists {id} items {itemId} …
        return parts[4], parts[6]

    async def _add(self, method, url, data):
        _, ident = self._ids(url)
        view, refused = await self._begin(method, url, "add", ident, data)
        if refused:
            return refused
        assert "Id" not in data, "the id is in the path"
        if self.item(view["Id"], ident) is None:
            item: dict[str, Any] = {"Id": ident}
            if view["Type"] != "Events":
                item["Quantity"] = 1
            item.update({k: _clean(v) for k, v in data.items()})
            if "Recurrence" in item:
                item["Recurrence"] = _recurrence(item["Recurrence"])
            self.items(view["Id"]).append(item)
        return self._commit(method, url, view, ident)

    async def _edit(self, method, url, data):
        _, ident = self._ids(url)
        view, refused = await self._begin(method, url, "edit", ident, data)
        if refused:
            return refused
        item = self.item(view["Id"], ident)
        if item is None:
            return _json(method, url, HTTPStatus.NOT_FOUND, {"title": "item_not_found"})
        for key, value in data.items():
            if key == "Due" and isinstance(value, dict) and isinstance(item.get("Due"), dict):
                due = {**item["Due"], **value}
                item["Due"] = {k: v for k, v in due.items() if v is not None}
            elif key == "Recurrence":
                item[key] = _recurrence(value)
            else:
                item[key] = _clean(value)
        return self._commit(method, url, view, ident)

    async def _remove(self, method, url, data):
        _, ident = self._ids(url)
        view, refused = await self._begin(method, url, "remove", ident, None)
        if refused:
            return refused
        view["Items"] = [i for i in view["Items"] if i["Id"] != ident]
        return self._commit(method, url, view, ident)

    async def _done(self, method, url, data):
        _, ident = self._ids(url)
        view, refused = await self._begin(method, url, "done", ident, data)
        if refused:
            return refused
        item = self.item(view["Id"], ident)
        if item is None:
            return _json(method, url, HTTPStatus.NOT_FOUND, {"title": "item_not_found"})
        item["Done"] = bool(data["Done"])
        item["DoneAt"] = "2026-11-13T12:00:00+00:00" if item["Done"] else None
        return self._commit(method, url, view, ident)

    async def _buy(self, method, url, data):
        _, ident = self._ids(url)
        view, refused = await self._begin(method, url, "buy", ident, data)
        if refused:
            return refused
        item = self.item(view["Id"], ident)
        if item is None:
            return _json(method, url, HTTPStatus.NOT_FOUND, {"title": "item_not_found"})
        view["Items"].remove(item)
        check_id = str(uuid.uuid4())
        view["RecentPurchases"].insert(0, {
            "CheckId": check_id, "ItemId": ident, "Name": item["Name"], "Quantity": item.get("Quantity", 1),
            "CheckedAt": "2026-11-13T12:00:00+00:00",
        })
        return self._commit(method, url, view, ident)

    async def _unbuy(self, method, url, data):
        _, check_id = self._ids(url)
        view, refused = await self._begin(method, url, "unbuy", check_id, data)
        if refused:
            return refused
        purchase = next((p for p in view["RecentPurchases"] if p["CheckId"] == check_id), None)
        if purchase is None:
            return _json(method, url, HTTPStatus.NOT_FOUND, {"title": "purchase_not_found"})
        view["RecentPurchases"].remove(purchase)
        view["Items"].append({"Id": purchase["ItemId"], "Name": purchase["Name"], "Quantity": purchase["Quantity"]})
        return self._commit(method, url, view, check_id)


# ── a real local server (tests with real sockets) ──────────────────────────────────────────


def add_v1_routes(app: web.Application, changes: Any = None) -> None:
    """Identity and v1 read routes with the sample data; `changes` is the SSE handler, if any."""

    async def me(request: web.Request) -> web.Response:
        return web.json_response({"Id": USER_ID, "Email": "a@example.com", "Name": "Anna"})

    async def capabilities(request: web.Request) -> web.Response:
        areas = [
            {"Area": area, "Read": True, "Write": bool(ops), "Operations": ops} for area, ops in OPERATIONS.items()
        ]
        return web.json_response({"Version": 1, "Areas": areas})

    async def snapshots(request: web.Request) -> web.Response:
        objects = [wire(v) for v in [*lists(), *plans()] if area_of(v) in (await request.json())["Areas"]]
        return web.json_response({"Version": 1, "Cursor": "cursor-1", "Complete": True, "Objects": objects})

    app.router.add_get("/api/auth/me", me)
    app.router.add_get("/api/integrations/v1/capabilities", capabilities)
    app.router.add_post("/api/integrations/v1/snapshots", snapshots)
    if changes is not None:
        app.router.add_get("/api/integrations/v1/changes", changes)


@contextmanager
def serving_amzettel(base: str) -> Iterator[None]:
    """Let a local test server stand in for the official amZettel server."""
    with (
        patch("custom_components.amzettel.DEFAULT_SERVER", base),
        patch("custom_components.amzettel.feed_api.DEFAULT_SERVER", base),
    ):
        yield
