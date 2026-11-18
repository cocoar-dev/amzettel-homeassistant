"""Sequential SSE consumer for the public integrations feed."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp

from .const import REALTIME_BACKOFF_MAX, REALTIME_BACKOFF_MIN
from .exceptions import AmZettelError, AuthFailed, NotFound, NotPermitted
from .feed_api import KINDS, PREFIX, FeedHttpError, FeedProtocolError, object_id, opaque, wire_dict

if TYPE_CHECKING:
    from .coordinator import AmZettelCoordinator

STREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=30, sock_read=60)
INVALID_CURSOR_BACKOFF = 300
MAX_LINE = 64 * 1024
MAX_FRAME = 256 * 1024
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Frame:
    event: str
    cursor: str | None
    body: dict[str, Any]


async def frames(content: aiohttp.StreamReader) -> AsyncIterator[Frame]:
    """Read only complete frames, with bounded buffers."""
    pending = bytearray()
    parts: dict[str, str] = {}
    data: list[str] = []
    size = 0
    async for chunk in content.iter_chunked(8192):
        pending.extend(chunk)
        while (end := pending.find(b"\n")) >= 0:
            if end > MAX_LINE:
                raise FeedProtocolError("SSE line too large")
            try:
                line = pending[:end].rstrip(b"\r").decode("utf-8")
            except UnicodeError as err:
                raise FeedProtocolError("invalid SSE text") from err
            del pending[:end + 1]
            if not line:
                if data or parts:
                    try:
                        body = wire_dict(json.loads("\n".join(data)), versioned=True)
                    except ValueError as err:
                        raise FeedProtocolError("invalid SSE JSON") from err
                    yield Frame(parts.get("event", ""), opaque(parts["id"]) if "id" in parts else None, body)
                data, parts, size = [], {}, 0
            elif not line.startswith(":"):
                size += end + 1
                if size > MAX_FRAME:
                    raise FeedProtocolError("SSE frame too large")
                field, _, value = line.partition(":")
                value = value.removeprefix(" ")
                if field == "data":
                    data.append(value)
                elif field in ("event", "id"):
                    parts[field] = value
        if len(pending) > MAX_LINE:
            raise FeedProtocolError("SSE line too large")


class FeedStream:
    """One live connection per loaded config entry."""

    def __init__(self, session: aiohttp.ClientSession, coordinator: AmZettelCoordinator) -> None:
        self.session = session
        self.coordinator = coordinator
        self.connected = False
        self.ready_count = self.connect_attempts = 0
        self.task: asyncio.Task[None] | None = None
        self.last_error: str | None = None

    async def stop(self) -> None:
        task = self.cancel()
        if task:
            await asyncio.gather(task, return_exceptions=True)

    def cancel(self) -> asyncio.Task[None] | None:
        """Stop a stream from another task without cancelling its own revocation handler."""
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel()
            return self.task
        return None

    async def run(self) -> None:
        self.task = asyncio.current_task()
        delay = REALTIME_BACKOFF_MIN
        refresh = False
        invalid_cursor = 0
        coord = self.coordinator
        try:
            while not coord.revoked:
                previous_ready = self.ready_count
                retry_after = 0
                try:
                    if coord.cursor is None:
                        await coord.async_resnapshot()
                    if coord.cursor is not None:
                        self.connect_attempts += 1
                        result = await self._once(refresh)
                        refresh = result == "token_expired"
                        if result == "resync_required":
                            coord.hide_feed_data()
                        if self.ready_count != previous_ready:
                            invalid_cursor = 0
                except AuthFailed:
                    coord.revoke_feed()
                    return
                except (NotFound, NotPermitted) as err:
                    self.last_error = str(err.status)
                    coord.hide_feed_data()
                except FeedHttpError as err:
                    self.last_error = str(err.status)
                    retry_after = err.delay
                    if err.title == "invalid_cursor":
                        coord.hide_feed_data()
                        invalid_cursor += 1
                        if invalid_cursor >= 2:
                            # Stay fail-closed, but recover without a reload when the server
                            # starts accepting fresh snapshot cursors again.
                            retry_after = max(retry_after, INVALID_CURSOR_BACKOFF)
                            if invalid_cursor == 2:
                                _LOGGER.warning(
                                    "amZettel rejected a fresh realtime cursor; retrying a full snapshot in %s seconds",
                                    retry_after,
                                )
                    elif err.status == 410:
                        coord.hide_feed_data()
                except (AmZettelError, aiohttp.ClientError, TimeoutError) as err:
                    self.last_error = type(err).__name__  # No payload, URL or cursor in diagnostics.
                finally:
                    self.connected = False
                if self.ready_count != previous_ready:
                    delay = REALTIME_BACKOFF_MIN
                await asyncio.sleep(max(delay, retry_after))
                delay = min(delay * 2, REALTIME_BACKOFF_MAX)
        finally:
            self.task = None

    async def _once(self, refresh: bool) -> str:
        coord = self.coordinator
        api = coord.api
        assert coord.cursor is not None
        headers = {"Accept": "text/event-stream", "Last-Event-ID": coord.cursor}
        params = [("area", area) for area in coord.areas]
        for force in (refresh, True) if not refresh else (True,):
            async with self.session.get(
                api.server + PREFIX + "/changes", params=params,
                headers={**await api.headers(refresh=force), **headers},
                timeout=STREAM_TIMEOUT, allow_redirects=False,
            ) as response:
                if response.status == 401 and not force:
                    continue
                if response.status != 200:
                    await api.response(response)
                    raise FeedProtocolError("unexpected SSE status")
                if response.content_type != "text/event-stream":
                    raise FeedProtocolError("unexpected SSE content type")
                self.connected = True
                first = True
                async for frame in frames(response.content):
                    if frame.event in ("resync_required", "token_expired", "revoked", "retry"):
                        if frame.cursor is not None:
                            raise FeedProtocolError("control frame has a cursor")
                        if frame.event == "revoked":
                            raise AuthFailed("integration_revoked")
                        return frame.event
                    if frame.cursor is None:
                        raise FeedProtocolError("SSE frame without cursor")
                    if frame.event == "change":
                        area = frame.body.get("Area")
                        if (
                            not isinstance(area, str) or area not in coord.areas
                            or frame.body.get("Kind") != KINDS[area] or frame.body.get("Change") != "Invalidated"
                        ):
                            raise FeedProtocolError("invalid SSE change")
                        await coord.async_apply_change(area, object_id(frame.body.get("Id")), frame.cursor)
                    elif frame.event == "checkpoint":
                        coord.apply_checkpoint(frame.cursor)
                    else:
                        raise FeedProtocolError("unknown SSE event")
                    if first:
                        first = False
                        self.last_error = None
                        self.ready_count += 1
                return "retry"
        raise AuthFailed("SSE token rejected")
