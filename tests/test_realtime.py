"""How the SSE stream reaches the coordinator: framing, targeted reads per change, replays, reconnects."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.amzettel.const import DEFAULT_SERVER, REALTIME_BACKOFF_MIN
from custom_components.amzettel.coordinator import AmZettelCoordinator
from custom_components.amzettel.exceptions import AuthFailed, CannotConnect, NotFound
from custom_components.amzettel.feed_api import FeedApi, FeedHttpError, FeedProtocolError, Snapshot
from custom_components.amzettel.feed_stream import MAX_LINE, FeedStream, frames

from .conftest import SCOPES, SUB, entry_data
from .data import EVENTS_ID, PLAN_ID, SHOPPING_ID, lists, plans


def _content(*chunks: bytes) -> MagicMock:
    content = MagicMock()

    async def iterate(size):
        for chunk in chunks:
            yield chunk

    content.iter_chunked = iterate
    return content


# ── framing ────────────────────────────────────────────────────────────────────────────────


async def test_several_frames_in_one_chunk_and_heartbeats() -> None:
    content = _content(
        b': heartbeat\n\nevent: change\nid: c-1\ndata: {"Version":1,\ndata: "Area":"Lists"}\n\n'
        b'event: checkpoint\r\nid: c-2\r\ndata: {"Version":1}\r\n\r\n'
    )
    result = [(f.event, f.cursor, f.body) async for f in frames(content)]
    assert result == [
        ("change", "c-1", {"Version": 1, "Area": "Lists"}),  # data lines join with a newline
        ("checkpoint", "c-2", {"Version": 1}),
    ]


@pytest.mark.parametrize(
    "raw",
    [
        b"data: " + b"x" * (MAX_LINE + 1) + b"\n\n",  # a line beyond the limit
        b'event: change\nid: c\ndata: {"Version":2}\n\n',  # another contract version
        b"event: change\nid: c\ndata: not json\n\n",
    ],
)
async def test_invalid_frames_are_protocol_errors(raw: bytes) -> None:
    with pytest.raises(FeedProtocolError):
        [frame async for frame in frames(_content(raw))]


# ── changes reach the coordinator ──────────────────────────────────────────────────────────


@pytest.fixture
async def coordinator(hass: HomeAssistant) -> AmZettelCoordinator:
    entry = MockConfigEntry(domain="amzettel", unique_id=SUB, data=entry_data())
    entry.add_to_hass(hass)
    api = MagicMock(spec=FeedApi, server=DEFAULT_SERVER)
    api.async_snapshot.return_value = Snapshot(
        ("Calendar", "Lists", "MealPlans"),
        {},
        {item["Id"]: item for item in lists()},
        {p["Id"]: p for p in plans()},
        "cursor-1",
    )
    coordinator = AmZettelCoordinator(hass, entry, api, SCOPES)
    await coordinator.async_resnapshot()
    return coordinator


async def test_a_change_reads_only_that_list(coordinator: AmZettelCoordinator) -> None:
    coordinator.api.async_object.return_value = {**lists()[0], "Name": "Wocheneinkauf", "ETag": '"shopping-2"'}
    await coordinator.async_apply_change("Lists", SHOPPING_ID, "cursor-2")
    coordinator.api.async_object.assert_awaited_once_with("Lists", SHOPPING_ID)
    coordinator.api.async_snapshot.assert_awaited_once()  # only the first baseline
    assert coordinator.data.lists[SHOPPING_ID]["Name"] == "Wocheneinkauf"
    assert coordinator.cursor == "cursor-2" and coordinator.last_change_area == "Lists"


async def test_a_meal_plan_change_reads_that_plan(coordinator: AmZettelCoordinator) -> None:
    coordinator.api.async_object.return_value = {**plans()[0], "Entries": [], "ETag": '"plan-2"'}
    await coordinator.async_apply_change("MealPlans", PLAN_ID, "cursor-2")
    coordinator.api.async_object.assert_awaited_once_with("MealPlans", PLAN_ID)
    assert coordinator.data.meal_plans[PLAN_ID]["Entries"] == []


async def test_a_list_that_is_gone_disappears_with_the_next_baseline(coordinator: AmZettelCoordinator) -> None:
    """A change whose object can no longer be read hides the data and keeps the cursor; the stream
    then takes a new snapshot, without that object."""
    coordinator.api.async_object.side_effect = NotFound(404)
    with pytest.raises(NotFound):
        await coordinator.async_apply_change("Calendar", EVENTS_ID, "cursor-2")
    assert coordinator.cursor is None and not coordinator.data.authoritative

    coordinator.api.async_snapshot.return_value = Snapshot(
        ("Calendar", "Lists", "MealPlans"), {}, {v["Id"]: v for v in lists() if v["Id"] != EVENTS_ID}, {}, "cursor-3"
    )
    await coordinator.async_resnapshot()
    assert EVENTS_ID not in coordinator.data.lists and coordinator.data.authoritative


async def test_a_replayed_change_at_the_current_cursor_reads_nothing(coordinator: AmZettelCoordinator) -> None:
    """After a reconnect the server may repeat the change the cursor already stands on."""
    await coordinator.async_apply_change("Lists", SHOPPING_ID, "cursor-1")
    coordinator.api.async_object.assert_not_awaited()


async def test_a_failed_read_does_not_advance_the_cursor(coordinator: AmZettelCoordinator) -> None:
    coordinator.api.async_object.side_effect = CannotConnect("temporary")
    with pytest.raises(CannotConnect):
        await coordinator.async_apply_change("Lists", SHOPPING_ID, "cursor-2")
    assert coordinator.cursor == "cursor-1" and coordinator.data.authoritative  # replayed after reconnect


# ── reconnects ─────────────────────────────────────────────────────────────────────────────


async def test_run_reconnects_with_backoff_and_resets_after_ready(coordinator: AmZettelCoordinator) -> None:
    stream = FeedStream(MagicMock(), coordinator)

    async def ready_then_drop(refresh: bool) -> str:
        stream.ready_count += 1  # the first frame arrived: the connection was good
        return "retry"

    attempts = [CannotConnect(), CannotConnect(), ready_then_drop, CannotConnect(), AuthFailed()]

    async def connect(refresh: bool) -> str:
        step = attempts.pop(0)
        if isinstance(step, Exception):
            raise step
        return await step(refresh)

    with (
        patch.object(stream, "_once", side_effect=connect),
        patch.object(coordinator, "_start_reauth") as reauth,
        patch("custom_components.amzettel.feed_stream.asyncio.sleep", new=AsyncMock()) as sleep,
    ):
        await stream.run()
    base = REALTIME_BACKOFF_MIN
    assert [c.args[0] for c in sleep.await_args_list] == [base, base * 2, base, base * 2]
    assert stream.connect_attempts == 5
    reauth.assert_called_once()  # the revoked grant ends the stream
    assert coordinator.revoked and stream.task is None


async def test_repeated_invalid_cursors_retry_slowly_and_recover(coordinator, caplog):
    stream = FeedStream(MagicMock(), coordinator)
    attempts = 0
    snapshots = coordinator.api.async_snapshot
    snapshots.reset_mock()

    async def connect(refresh):
        nonlocal attempts
        attempts += 1
        assert coordinator.cursor == "cursor-1"  # never reconnect in live-only mode
        if attempts in (1, 2, 3, 5, 6):
            raise FeedHttpError(400, "invalid_cursor", None)
        if attempts == 4:
            stream.ready_count += 1
            stream.last_error = None
            return "retry"
        raise AuthFailed()

    with (
        patch.object(stream, "_once", side_effect=connect),
        patch.object(coordinator, "_start_reauth") as reauth,
        patch("custom_components.amzettel.feed_stream.asyncio.sleep", new=AsyncMock()) as sleep,
    ):
        await stream.run()
    base = REALTIME_BACKOFF_MIN
    assert [call.args[0] for call in sleep.await_args_list] == [base, 300, 300, base, base * 2, 300]
    assert snapshots.await_count == 5
    assert caplog.text.count("fresh realtime cursor") == 2  # once per failure streak
    assert "cursor-1" not in caplog.text
    reauth.assert_called_once()
    assert coordinator.revoked and stream.task is None


async def test_invalid_cursor_backoff_can_be_cancelled(coordinator):
    import asyncio

    stream = FeedStream(MagicMock(), coordinator)
    waiting = asyncio.Event()

    async def sleep(delay):
        if delay == 300:
            waiting.set()
            await asyncio.Future()

    with (
        patch.object(stream, "_once", side_effect=FeedHttpError(400, "invalid_cursor", None)),
        patch("custom_components.amzettel.feed_stream.asyncio.sleep", side_effect=sleep),
    ):
        task = asyncio.create_task(stream.run())
        try:
            await asyncio.wait_for(waiting.wait(), timeout=2)
            await stream.stop()
            assert task.cancelled() and stream.task is None
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
