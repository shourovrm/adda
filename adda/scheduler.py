"""Turn-taking for one thread of a room (SPEC v0.4 E1 and E2, v0.5 F2): event-driven, not a loop.

There is one Scheduler per thread. Seats, folder, budget, spend, liveliness, mode and
max_concurrent belong to the room and are read from the store on every decision, so two threads
of one room share the budget. Messages, sessions, speaking seats and afterthought ticks belong
to the thread. `room` frames go to every tab of the room; everything else to the thread's tabs.

Every posted message (owner or seat) triggers one "reaction": mentions start seats, a `[more]`
continuation restarts the author, and a Jev round lets seats bid to speak. Several seats may
compose at once, up to `Room.max_concurrent`; each is its own asyncio task in `speaking`. When
nobody is speaking the room is quiet, and an afterthought timer may wake seats later. REST and
WebSocket handlers both change rooms and seats through `apply_room_patch` and
`apply_seat_patch`, so there is one code path for validation and for the `room` frame.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from adda.config import Settings
from adda.models import (
    ACCESS_LEVELS,
    MODES,
    THINKING_LEVELS,
    Bid,
    MemorySource,
    Message,
    Room,
    Seat,
    Thread,
)
from adda.personas import Persona, PersonaError, load_persona, load_personas, write_memory_note
from adda.prompts import (
    END_OF_DISCUSSION_PROMPT,
    parse_memory_bullets,
    render_transcript,
    resolve_mentions,
    split_more_marker,
    system_prompt,
    turn_prompt,
)
from adda.routers.base import (
    AFTERTHOUGHT_QUESTION,
    MESSAGE_QUESTION,
    Router,
    RouterError,
)
from adda.seats.base import SeatAdapter, SeatError, is_pass
from adda.seats.registry import build_adapter
from adda.store import Store

logger = logging.getLogger(__name__)

Broadcast = Callable[[dict[str, Any]], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]
AdapterBuilder = Callable[..., SeatAdapter]

# A seat with no session yet gets at most this many earlier messages in its first prompt.
FIRST_TURN_HISTORY_LIMIT = 50
MEMORY_NOTE_TITLE_LENGTH = 80
EAGERNESS_STEP = 0.4
COOLDOWN_PENALTY = 1.5

# Reasons stored in Message.meta["woken_by"] and sent in quiet frames.
WOKEN_BY_BID = "bid"
WOKEN_BY_ROUNDROBIN = "roundrobin"
WOKEN_BY_WAKE = "wake"
WOKEN_BY_ALL = "all"
WOKEN_BY_MENTION = "mention"
WOKEN_BY_MORE = "more"
WOKEN_BY_AFTERTHOUGHT = "afterthought"

# A seat that is already composing when one of these arrives answers it right after (follow-up).
FOLLOW_UP_REASONS = (WOKEN_BY_MENTION, WOKEN_BY_ALL, WOKEN_BY_WAKE)

# Afterthought timing (SPEC v0.4 E2): (11 - liveliness) * 20 s, jittered, scaled per setting.
AFTERTHOUGHT_SECONDS_PER_LIVELINESS_STEP = 20.0
AFTERTHOUGHT_JITTER_LOW = 0.7
AFTERTHOUGHT_JITTER_HIGH = 1.3
# The first tick of a quiet spell is shortened by 5 % per eagerness point above 5.
AFTERTHOUGHT_EAGERNESS_STEP = 0.05

ROOM_PATCH_FIELDS = ("title", "mode", "liveliness", "budget_usd", "max_concurrent", "access")
SEAT_PATCH_FIELDS = (
    "name",
    "program",
    "model",
    "thinking",
    "persona",
    "instruction",
    "color",
    "enabled",
    "position",
    "eagerness",
    "access",
)
# Text fields where the UI sends "" to mean "unset".
EMPTY_MEANS_NONE_FIELDS = ("model", "persona", "instruction")


class RoomEndedError(RuntimeError):
    """The room is archived; it takes no new messages."""


class ThreadEndedError(RoomEndedError):
    """The thread has ended; it takes no new messages (other threads of the room still do)."""


class UnknownSeatError(LookupError):
    """A seat id that is not in this room."""


class BudgetReachedError(RuntimeError):
    """The room's budget is spent; mentions and wakes are refused."""


def liveliness_threshold(liveliness: int, automatic_turns: int, floor: float) -> float:
    """Lowest bid that may speak (SPEC v0.3 D1): base(L) + ramp(L) * k, never below `floor`.

    base(L) = 7.5 - 0.5 L and ramp(L) = 0.3 - 0.025 L, where k is the number of bid-driven turns
    since the last owner message or wake. Jev's scores in real rooms sit between 2 and 7, so the
    curve is tuned to that range: a quiet room needs a strong reason to speak, a lively one
    speaks on a weak reason, and every extra turn raises the bar a little.
    """
    base = 7.5 - 0.5 * liveliness
    ramp = 0.3 - 0.025 * liveliness
    return max(floor, base + ramp * automatic_turns)


def adjusted_bid_score(
    raw_score: float, eagerness: int, cooling_down: bool, extra_penalty: float = 0.0
) -> float:
    """Jev's score shifted by the seat's eagerness and penalties, kept within 0..10.

    Eagerness 5 changes nothing; each step moves the score by 0.4. A seat that spoke less than
    `cooldown_s` ago loses COOLDOWN_PENALTY, so it can still win when it is far ahead.
    `extra_penalty` is the self-reply penalty for the author of the latest message.
    """
    adjusted = raw_score + EAGERNESS_STEP * (eagerness - 5) - extra_penalty
    if cooling_down:
        adjusted -= COOLDOWN_PENALTY
    return min(10.0, max(0.0, adjusted))


def note_title_from_bullet(bullet: str) -> str:
    """Title for a memory note: the bullet's first sentence, cut at a word boundary.

    Seats answer the end-of-thread prompt with full sentences, so a hard cut at a fixed
    length left titles like 'needs an explicit "a yes fixes'. Prefer the first sentence; when
    that is still too long, cut before the last space inside the limit.
    """
    text = bullet.strip()
    for terminator in (". ", "; ", ": "):
        head, separator, _tail = text.partition(terminator)
        if separator and len(head) <= MEMORY_NOTE_TITLE_LENGTH:
            return head.rstrip()
    if len(text) <= MEMORY_NOTE_TITLE_LENGTH:
        return text.rstrip(".")
    cut = text.rfind(" ", 0, MEMORY_NOTE_TITLE_LENGTH)
    if cut < MEMORY_NOTE_TITLE_LENGTH // 2:
        cut = MEMORY_NOTE_TITLE_LENGTH
    title = text[:cut]
    if title.count('"') % 2 == 1:
        # The cut fell inside a quotation; drop the opened fragment rather than leave it dangling.
        title = title[: title.rfind('"')]
    return title.rstrip(" ,;:")


def budget_exhausted(room: Room) -> bool:
    return room.budget_usd > 0 and room.spent_usd >= room.budget_usd


def budget_message(room: Room) -> str:
    return f"Budget of ${room.budget_usd:.2f} reached; raise it to continue."


class Scheduler:
    def __init__(
        self,
        thread_id: str,
        store: Store,
        settings: Settings,
        router: Router,
        fallback_router: Router,
        broadcast: Broadcast,
        adapter_builder: AdapterBuilder = build_adapter,
        harness_versions: Callable[[], dict[str, str | None]] = dict,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
        broadcast_room: Broadcast | None = None,
    ) -> None:
        """`broadcast` reaches the tabs of this thread; `broadcast_room` reaches every tab of the
        room (for `room` and `thread` frames) and defaults to `broadcast`."""
        self.thread_id = thread_id
        self.room_id = store.get_thread(thread_id).room_id
        self.store = store
        self.settings = settings
        self.router = router
        self.fallback_router = fallback_router
        self.broadcast = broadcast
        self.broadcast_room = broadcast_room if broadcast_room is not None else broadcast
        self.adapter_builder = adapter_builder
        self.harness_versions = harness_versions
        # Time and randomness are injected so tests never wait for an afterthought delay.
        self.clock = clock
        self._sleep = sleep
        self._uniform = uniform

        # Adapters by seat id, with the inputs each was built from so a change rebuilds it.
        self.adapters: dict[str, SeatAdapter] = {}
        self._adapter_signatures: dict[str, tuple] = {}

        # A seat composes at most one reply at a time: seat id -> the task composing it.
        self.speaking: dict[str, asyncio.Task] = {}
        # Seats that were asked to speak while composing, with the reason; they run once more
        # when they finish, and then see the new messages.
        self.follow_up: dict[str, str] = {}
        # Consecutive `[more]` continuations per seat.
        self.more_streak: dict[str, int] = {}
        self.afterthought: asyncio.Task | None = None
        self.afterthoughts_this_spell = 0
        self.roundrobin_task: asyncio.Task | None = None
        # One task per posted message (and per silent end of a turn) that decides what happens
        # next. Tracked so `stop` can cancel them and tests can wait for them.
        self._reactions: set[asyncio.Task] = set()
        # Bidding rounds run one at a time so each sees who is speaking after the previous one.
        self._bidding_lock = asyncio.Lock()
        # All automatic starts, for the safety cap; and bid-driven starts only, which raise the
        # threshold (k in liveliness_threshold). Both reset on an owner message or wake.
        self.auto_turns_since_owner = 0
        self.bid_turns_since_owner = 0
        self.last_spoke_at: dict[str, float] = {}
        # Seats that passed since the last posted message; they are not asked again until
        # someone speaks.
        self.passed_seats: set[str] = set()
        # Id of the newest message each seat has already been shown, so later prompts carry
        # only what is new to that seat's resumed tool session.
        self.seen_until: dict[str, str] = {}
        self.router_failure_reported = False
        # Best bid of the latest round, for the log line when the room goes quiet.
        self._last_top_seat: str | None = None
        self._last_top_score: float | None = None
        # Budget value for which the "budget reached" system message was already posted.
        self._budget_notice_for: float | None = None

    # Room view

    def current_room(self) -> Room:
        """The room from the store with live facts (availability, version) on every seat."""
        return self.describe(self.store.get_room(self.room_id))

    def describe(self, room: Room) -> Room:
        self._sync_adapters(room)
        versions = self.harness_versions()
        seats = []
        for seat in room.seats:
            adapter = self.adapters[seat.id]
            seats.append(
                seat.model_copy(
                    update={
                        "available": adapter.available(),
                        "harness_version": versions.get(seat.program),
                    }
                )
            )
        return room.model_copy(update={"seats": seats})

    def update_settings(self, settings: Settings, router: Router | None = None) -> None:
        """New settings take effect on the next turn; adapters rebuild when defaults changed."""
        self.settings = settings
        if router is not None:
            self.router = router
            self.router_failure_reported = False

    def _working_directory(self, room: Room) -> Path:
        return Path(room.folder) if room.folder else self.settings.seat_workdir

    def _sync_adapters(self, room: Room) -> None:
        """Build adapters for new seats, rebuild those whose program, model, thinking or
        effective access changed (or whose harness defaults changed), and drop those of removed
        seats."""
        working_directory = self._working_directory(room)
        live_ids = {seat.id for seat in room.seats}
        for seat_id in list(self.adapters):
            if seat_id not in live_ids:
                del self.adapters[seat_id]
                self._adapter_signatures.pop(seat_id, None)
        for seat in room.seats:
            harness_settings = self.settings.harnesses.get(seat.program)
            signature = (
                seat.program,
                seat.model,
                seat.thinking,
                seat.effective_access,
                str(working_directory),
                harness_settings,
            )
            if self._adapter_signatures.get(seat.id) != signature:
                self.adapters[seat.id] = self.adapter_builder(
                    seat, harness_settings, working_directory, self.settings.data_dir
                )
                self._adapter_signatures[seat.id] = signature

    def _effective_model(self, seat: Seat) -> str | None:
        harness_settings = self.settings.harnesses.get(seat.program)
        return seat.model or (harness_settings.model if harness_settings else "") or None

    async def publish_room(self) -> Room:
        """Send the current room to every tab of every thread of the room (any change: spend,
        seats, liveliness, threads...)."""
        room = self.current_room()
        await self.broadcast_room({"type": "room", "room": room.model_dump(mode="json")})
        return room

    async def publish_thread(self) -> Thread:
        """Send this thread's summary to every tab of the room (title change or end)."""
        summary = self.store.thread_summary(self.thread_id)
        await self.broadcast_room({"type": "thread", "thread": summary.model_dump(mode="json")})
        return summary

    def current_thread(self) -> Thread:
        return self.store.get_thread(self.thread_id)

    async def rename(self, title: str) -> Thread:
        self.store.rename_thread(self.thread_id, title)
        return await self.publish_thread()

    # Patches (one code path for REST and WebSocket)

    async def apply_room_patch(self, patch: dict[str, Any]) -> Room:
        """Change title, mode, liveliness, budget or voices at once. Raises ValueError for
        anything invalid."""
        unknown_fields = set(patch) - set(ROOM_PATCH_FIELDS)
        if unknown_fields:
            raise ValueError(f"cannot change room fields {sorted(unknown_fields)}")
        fields: dict[str, Any] = {}
        if "title" in patch:
            title = str(patch["title"]).strip()
            if not title:
                raise ValueError("title must not be empty")
            fields["title"] = title
        if "mode" in patch:
            if patch["mode"] not in MODES:
                raise ValueError(f"mode must be one of {MODES}, got {patch['mode']!r}")
            fields["mode"] = patch["mode"]
        if "access" in patch:
            if patch["access"] not in ACCESS_LEVELS:
                raise ValueError(f"access must be one of {ACCESS_LEVELS}, got {patch['access']!r}")
            fields["access"] = patch["access"]
        if "liveliness" in patch:
            fields["liveliness"] = _whole_number(patch["liveliness"], "liveliness")
        if "budget_usd" in patch:
            fields["budget_usd"] = _number(patch["budget_usd"], "budget_usd")
        if "max_concurrent" in patch:
            # The store checks the 1..4 range.
            fields["max_concurrent"] = _whole_number(patch["max_concurrent"], "max_concurrent")
        self.store.update_room(self.room_id, **fields)
        if "budget_usd" in fields:
            self.forget_budget_notice()
        return await self.publish_room()

    def forget_budget_notice(self) -> None:
        """A new budget deserves a fresh notice if it is still exceeded."""
        self._budget_notice_for = None

    async def apply_seat_patch(self, seat_id: str, patch: dict[str, Any]) -> Room:
        """Change one seat. Name, persona and instruction apply from the next prompt; model and
        thinking rebuild the adapter; a new program also cancels the seat's current reply (its
        session cannot be resumed by another program). Raises ValueError or UnknownSeatError."""
        seat = self._require_seat(seat_id)
        unknown_fields = set(patch) - set(SEAT_PATCH_FIELDS)
        if unknown_fields:
            raise ValueError(f"cannot change seat fields {sorted(unknown_fields)}")
        fields: dict[str, Any] = {}
        for field_name, value in patch.items():
            if field_name in EMPTY_MEANS_NONE_FIELDS:
                value = None if value is None or not str(value).strip() else str(value)
            elif field_name == "name":
                value = str(value).strip()
            elif field_name == "enabled":
                if not isinstance(value, bool):
                    raise ValueError("enabled must be true or false")
            elif field_name == "position":
                value = _whole_number(value, "position")
            elif field_name == "access" and value is not None and value not in ACCESS_LEVELS:
                raise ValueError(f"access must be one of {ACCESS_LEVELS} or null, got {value!r}")
            elif field_name == "thinking" and value not in THINKING_LEVELS:
                raise ValueError(f"thinking must be one of {THINKING_LEVELS}, got {value!r}")
            fields[field_name] = value
        if fields.get("persona"):
            load_persona(
                self.settings.personas_dir, fields["persona"]
            )  # PersonaError is a ValueError
        self.store.update_seat(seat_id, **fields)
        if "program" in fields and fields["program"] != seat.program:
            await self.cancel_seat(seat_id)
        return await self.publish_room()

    async def remove_seat(self, seat_id: str) -> Room:
        """Remove a seat; if it is speaking right now, cancel its reply first. The caller
        cancels it in the room's other threads (see `forget_seat`)."""
        self._require_seat(seat_id)
        await self.forget_seat(seat_id)
        self.store.remove_seat(seat_id)
        return await self.publish_room()

    async def forget_seat(self, seat_id: str) -> None:
        """Cancel the seat's reply in this thread and drop what this thread knows about it."""
        await self.cancel_seat(seat_id)
        self.seen_until.pop(seat_id, None)
        self.last_spoke_at.pop(seat_id, None)
        self.passed_seats.discard(seat_id)

    def _require_seat(self, seat_id: str) -> Seat:
        for seat in self.store.get_room(self.room_id).seats:
            if seat.id == seat_id:
                return seat
        raise UnknownSeatError(f"no seat {seat_id!r} in room {self.room_id}")

    def _reset_turn_counters(self) -> None:
        """A new quiet spell starts: the owner spoke or woke someone."""
        self.auto_turns_since_owner = 0
        self.bid_turns_since_owner = 0
        self.afterthoughts_this_spell = 0

    # Owner actions

    async def say(self, text: str) -> Message:
        """Post an owner message. Seats that are composing carry on; the room reacts to it."""
        self._require_open_room()
        room = self.current_room()
        wakes = resolve_mentions(text, room.seats, author_seat_id=None)
        message = Message(
            room_id=self.room_id,
            thread_id=self.thread_id,
            author="owner",
            role="owner",
            text=text,
            mentions=list(wakes),
        )
        await self._post(message)
        self._reset_turn_counters()
        if wakes and budget_exhausted(room):
            await self.broadcast({"type": "error", "text": budget_message(room)})
        self._spawn_reaction(message)
        return message

    async def wake(self, seat_id: str) -> None:
        """Same as the owner mentioning the seat, without posting a message."""
        room = self._require_open_room()
        seat = self._require_seat(seat_id)
        if not seat.enabled:
            raise ValueError(f"seat {seat.name} is disabled")
        self._refuse_when_over_budget(room)
        self._reset_turn_counters()
        self._cancel_afterthought()
        self._start_seat(seat_id, WOKEN_BY_WAKE, automatic=False)

    async def wake_all(self) -> None:
        """Wake every enabled, installed seat in position order."""
        room = self._require_open_room()
        self._refuse_when_over_budget(room)
        room = self.current_room()
        seat_ids = [seat.id for seat in room.seats if seat.enabled and seat.available]
        if not seat_ids:
            raise ValueError("no enabled seat is available to wake")
        self._reset_turn_counters()
        self._cancel_afterthought()
        for seat_id in seat_ids:
            self._start_seat(seat_id, WOKEN_BY_ALL, automatic=False)

    async def stop(self) -> None:
        """Cancel every speaking seat, the pending afterthought and any round-robin pass.

        A seat runner kills its child process on cancellation. Nothing is scheduled afterwards:
        the room stays silent until the owner speaks or wakes someone.
        """
        while True:
            tasks = self._live_tasks(include_afterthought=True)
            self.follow_up.clear()
            self.more_streak.clear()
            if not tasks:
                return
            for task in tasks:
                task.cancel()
            # asyncio.wait does not re-raise the tasks' CancelledError into this coroutine.
            await asyncio.wait(tasks)

    async def end(self) -> dict[str, list[str]]:
        """Cancel everything, then ask every persona-backed seat that spoke in this thread what
        to remember, write the notes, mark the thread ended. Seats without a persona write no
        notes. The room stays open for other threads."""
        self._require_open_room()
        await self.stop()
        room = self.current_room()
        messages = self.store.list_thread_messages(self.thread_id)
        seats_by_id = {seat.id: seat for seat in room.seats}
        speaker_ids: list[str] = []
        for message in messages:
            if message.role == "seat" and message.seat_id not in speaker_ids:
                speaker_ids.append(message.seat_id)

        notes: dict[str, list[str]] = {}
        for seat_id in speaker_ids:
            seat = seats_by_id.get(seat_id)
            if seat is None or not seat.persona:
                continue
            try:
                persona = load_persona(self.settings.personas_dir, seat.persona)
            except PersonaError as error:
                await self._post_system(f"Memory notes for {seat.name} not written: {error}")
                continue
            notes[seat_id] = await self._collect_memory_notes(seat, persona, room, messages)

        self.store.end_thread(self.thread_id)
        await self.broadcast({"type": "ended", "notes": notes})
        await self.publish_thread()
        await self.publish_room()
        return notes

    # Tasks

    def _live_tasks(self, include_afterthought: bool) -> list[asyncio.Task]:
        """Unfinished tasks of this room other than the caller's own."""
        candidates = [*self.speaking.values(), *self._reactions]
        if self.roundrobin_task is not None:
            candidates.append(self.roundrobin_task)
        if include_afterthought and self.afterthought is not None:
            candidates.append(self.afterthought)
        current = asyncio.current_task()
        return [task for task in candidates if not task.done() and task is not current]

    async def wait_until_idle(self, include_afterthought: bool = False) -> None:
        """Wait until no seat is speaking and no reaction is pending (used by tests).

        The afterthought timer is left out by default because it may sleep for minutes.
        """
        while True:
            tasks = self._live_tasks(include_afterthought)
            if not tasks:
                return
            await asyncio.wait(tasks)

    def _is_idle(self) -> bool:
        """Nobody is speaking and nothing else (another reaction, a round-robin pass) is about
        to start someone. The afterthought timer does not count: it only fires in a quiet room."""
        return not self._live_tasks(include_afterthought=False)

    def _spawn_reaction(self, message: Message | None, wants_more: bool = False) -> None:
        task = asyncio.create_task(self._guarded(self._react, message, wants_more))
        self._reactions.add(task)
        task.add_done_callback(self._reactions.discard)

    async def _guarded(self, work: Callable[..., Awaitable[None]], *arguments: Any) -> None:
        # The coroutine is created here, not by the caller: a task cancelled before its first
        # step would otherwise leave a coroutine that was never awaited.
        try:
            await work(*arguments)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Nobody awaits these tasks, so report here instead of losing the error.
            logger.exception("turn-taking failed in thread %s", self.thread_id)
            await self.broadcast({"type": "error", "text": f"turn-taking failed: {error}"})

    def _start_seat(
        self,
        seat_id: str,
        woken_by: str,
        bid_score: float | None = None,
        automatic: bool = True,
    ) -> asyncio.Task | None:
        """Start a seat composing now. Returns its task, or None if it was not started.

        A seat that is already composing is not started twice: a mention or wake marks it for a
        follow-up instead. `automatic` starts (bids, afterthoughts, `[more]`, seat mentions,
        round-robin) count toward the safety cap and are refused once it is reached.
        """
        if automatic and self.auto_turns_since_owner >= self.settings.max_auto_turns:
            return None
        if seat_id in self.speaking:
            if woken_by in FOLLOW_UP_REASONS:
                self.follow_up[seat_id] = woken_by
            return None
        self._cancel_afterthought()
        if automatic:
            self.auto_turns_since_owner += 1
        task = asyncio.create_task(self._speak(seat_id, woken_by, bid_score))
        self.speaking[seat_id] = task
        return task

    async def _speak(self, seat_id: str, woken_by: str, bid_score: float | None) -> None:
        """One reply of one seat, then whatever follows from it."""
        message: Message | None = None
        wants_more = False
        try:
            message, wants_more = await self._run_seat_turn(seat_id, woken_by, bid_score)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("seat turn failed in thread %s", self.thread_id)
            await self.broadcast({"type": "error", "text": f"turn-taking failed: {error}"})
        finally:
            if self.speaking.get(seat_id) is asyncio.current_task():
                del self.speaking[seat_id]
        # Everything below is synchronous, so `stop` can never find this task half-way.
        follow_up_reason = self.follow_up.pop(seat_id, None)
        if follow_up_reason is not None and self._room_accepts_starts():
            self._start_seat(seat_id, follow_up_reason)
        self._spawn_reaction(message, wants_more)

    def _room_accepts_starts(self) -> bool:
        room = self.store.get_room(self.room_id)
        return not self._is_closed(room) and not budget_exhausted(room)

    def _is_closed(self, room: Room) -> bool:
        """The room is archived or this thread has ended."""
        return room.ended_at is not None or self.current_thread().ended_at is not None

    async def cancel_seat(self, seat_id: str) -> None:
        """Cancel one seat's reply (removed, or switched to another program)."""
        task = self.speaking.get(seat_id)
        self.follow_up.pop(seat_id, None)
        self.more_streak.pop(seat_id, None)
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.wait([task])
        # The cancelled seat will not report back, so let the room check whether it is quiet.
        self._spawn_reaction(None)

    # Reaction to a message

    async def _react(self, message: Message | None, wants_more: bool) -> None:
        """What a posted message sets in motion (SPEC v0.4 E1.on_message).

        `message` is None when a turn ended without one (PASS or failure); the room then only
        checks whether it has gone quiet.
        """
        room = self.store.get_room(self.room_id)
        if self._is_closed(room):
            return
        if message is not None:
            self.passed_seats.clear()
            self._cancel_afterthought()
            if message.seat_id is not None and not wants_more:
                self.more_streak.pop(message.seat_id, None)
        blocked = budget_exhausted(room) or self._safety_reached()
        if message is not None and not blocked:
            await self._start_from_message(room, message, wants_more)
        await self._settle_if_idle()

    async def _start_from_message(self, room: Room, message: Message, wants_more: bool) -> None:
        fresh_room = self.current_room()
        mentioned = resolve_mentions(message.text, fresh_room.seats, message.seat_id)
        for seat_id, reason in mentioned.items():
            self._start_seat(seat_id, reason, automatic=message.role != "owner")
        continued = wants_more and self._continue_seat(message.seat_id)
        if room.liveliness == 0:
            return
        if room.mode == "roundrobin" and message.role == "owner":
            self._start_roundrobin_pass(set(mentioned))
        elif room.mode == "bidding" and not continued:
            await self._bid_and_start(MESSAGE_QUESTION)

    def _continue_seat(self, seat_id: str | None) -> bool:
        """Restart a seat that ended with `[more]`, at most `more_limit` times in a row."""
        if seat_id is None:
            return False
        streak = self.more_streak.get(seat_id, 0)
        if streak >= self.settings.more_limit:
            # The seat used up its continuations; its next reply starts a fresh count.
            self.more_streak.pop(seat_id, None)
            return False
        if self._start_seat(seat_id, WOKEN_BY_MORE) is None:
            return False
        self.more_streak[seat_id] = streak + 1
        return True

    def _safety_reached(self) -> bool:
        return self.auto_turns_since_owner >= self.settings.max_auto_turns

    # Quiet and afterthoughts

    async def _settle_if_idle(self) -> None:
        """If nobody is speaking or about to, announce why and maybe schedule an afterthought."""
        if not self._is_idle():
            return
        room = self.store.get_room(self.room_id)
        if self._is_closed(room):
            return
        if budget_exhausted(room):
            await self._stop_for_budget(room)
        elif self._safety_reached():
            await self._quiet("safety", room)
        elif room.liveliness == 0:
            await self._quiet("paused", room)
        elif self._afterthought_possible(room):
            delay = self._afterthought_delay(room)
            await self._quiet("nobody", room, delay)
            self._cancel_afterthought()
            self.afterthought = asyncio.create_task(self._guarded(self._afterthought_loop, delay))
        else:
            await self._quiet("nobody", room)

    def _cancel_afterthought(self) -> None:
        task = self.afterthought
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        if task is not asyncio.current_task():
            self.afterthought = None

    def _ticks_left(self, room: Room) -> int:
        if room.mode != "bidding" or room.liveliness == 0:
            return 0
        return max(0, self.settings.afterthought_limit - self.afterthoughts_this_spell)

    def _afterthought_possible(self, room: Room) -> bool:
        """An afterthought tick may be scheduled: a bidding room that is open, within budget and
        the safety cap, still has ticks in this quiet spell and has a seat that could bid."""
        if self._is_closed(room) or budget_exhausted(room) or self._safety_reached():
            return False
        if room.mode != "bidding" or room.liveliness == 0 or self._ticks_left(room) == 0:
            return False
        return any(seat.enabled and seat.eagerness > 0 for seat in room.seats)

    def _afterthought_delay(self, room: Room) -> float:
        """(11 - L) * 20 s, jittered 0.7..1.3, scaled by `afterthought_scale`; the first tick of
        a spell is shorter when the room has an eager seat and longer when all are shy."""
        base_seconds = (11 - room.liveliness) * AFTERTHOUGHT_SECONDS_PER_LIVELINESS_STEP
        delay = base_seconds * self._uniform(AFTERTHOUGHT_JITTER_LOW, AFTERTHOUGHT_JITTER_HIGH)
        delay *= self.settings.afterthought_scale
        if self.afterthoughts_this_spell == 0:
            eagerness_values = [seat.eagerness for seat in room.seats if seat.enabled]
            highest_eagerness = max(eagerness_values, default=5)
            delay *= 1 - AFTERTHOUGHT_EAGERNESS_STEP * (highest_eagerness - 5)
        return delay

    async def _afterthought_loop(self, delay: float) -> None:
        """Wait, run an afterthought Jev round, repeat until someone speaks or ticks run out."""
        while True:
            await self._sleep(delay)
            room = self.store.get_room(self.room_id)
            if self.speaking or not self._afterthought_possible(room):
                return
            self.afterthoughts_this_spell += 1
            if await self._bid_and_start(AFTERTHOUGHT_QUESTION) > 0:
                # The speaker's message reacts and settles the room again.
                return
            room = self.store.get_room(self.room_id)
            if not self._afterthought_possible(room):
                await self._quiet("nobody", room)
                return
            delay = self._afterthought_delay(room)
            await self._quiet("nobody", room, delay)

    async def _stop_for_budget(self, room: Room) -> None:
        if self._budget_notice_for != room.budget_usd:
            self._budget_notice_for = room.budget_usd
            await self._post_system(budget_message(room))
        await self._quiet("budget", room)

    async def _quiet(self, reason: str, room: Room, next_tick_in_s: float | None = None) -> None:
        """Tell the tabs the room went quiet and log why, with the numbers that decided it."""
        threshold = liveliness_threshold(
            room.liveliness, self.bid_turns_since_owner, self.settings.router_threshold_floor
        )
        logger.info(
            "room %s quiet: reason=%s threshold=%.2f top_seat=%s top_adjusted_score=%s",
            self.room_id,
            reason,
            threshold,
            self._last_top_seat,
            "none" if self._last_top_score is None else f"{self._last_top_score:.2f}",
        )
        await self.broadcast(
            {
                "type": "quiet",
                "reason": reason,
                "next_tick_in_s": next_tick_in_s,
                "ticks_left": self._ticks_left(room) if reason == "nobody" else 0,
            }
        )

    def _refuse_when_over_budget(self, room: Room) -> None:
        if budget_exhausted(room):
            raise BudgetReachedError(budget_message(room))

    # Round-robin

    def _start_roundrobin_pass(self, already_started: set[str]) -> None:
        room = self.current_room()
        queue = [
            seat.id
            for seat in room.seats
            if seat.enabled and seat.available and seat.id not in already_started
        ]
        self.roundrobin_task = asyncio.create_task(self._guarded(self._roundrobin_pass, queue))

    async def _roundrobin_pass(self, queue: list[str]) -> None:
        """One pass over the seats, one at a time, then quiet."""
        for seat_id in queue:
            if not self._room_accepts_starts() or seat_id in self.speaking:
                continue
            task = self._start_seat(seat_id, WOKEN_BY_ROUNDROBIN)
            if task is None:
                break  # the safety cap is reached
            await asyncio.wait([task])
        await self._settle_if_idle()

    # Bidding

    async def _bid_and_start(self, question_kind: str) -> int:
        """One Jev round over the seats that are not speaking; start the winners.

        Returns how many seats were started. At most `room.max_concurrent` seats speak at once,
        so the winners are the best bids at or above the threshold, as many as there are free
        places (and safety budget).
        """
        async with self._bidding_lock:
            room = self.store.get_room(self.room_id)
            if self._is_closed(room) or budget_exhausted(room):
                return 0
            described = self.describe(room)
            bidders = [
                seat
                for seat in described.seats
                if seat.enabled and seat.available and seat.id not in self.speaking
            ]
            if not bidders:
                return 0
            window = self.store.list_thread_messages(
                self.thread_id, last=self.settings.router_window
            )
            latest_author_id = window[-1].seat_id if window else None
            transcript = render_transcript(window, self.settings.owner_name)
            router, bids = await self._ask_router(transcript, bidders, question_kind)

            now = self.clock()
            seats_by_id = {seat.id: seat for seat in bidders}
            # `score` is what the threshold compares against; `raw_score` is the router's number.
            adjusted_bids: list[Bid] = []
            for bid in bids:
                # A router may answer for seats it was not asked about; only bidders count.
                seat = seats_by_id.get(bid.seat)
                if seat is None:
                    continue
                last_spoke = self.last_spoke_at.get(bid.seat)
                cooling_down = (
                    last_spoke is not None and now - last_spoke < self.settings.cooldown_s
                )
                # The author of the latest message may answer itself, but at a price.
                self_reply_penalty = (
                    self.settings.self_reply_penalty if bid.seat == latest_author_id else 0.0
                )
                adjusted_bids.append(
                    bid.model_copy(
                        update={
                            "score": adjusted_bid_score(
                                bid.score, seat.eagerness, cooling_down, self_reply_penalty
                            ),
                            "raw_score": bid.score,
                        }
                    )
                )

            # The router call took time: seats may have started speaking meanwhile.
            eligible_bids = [
                bid
                for bid in adjusted_bids
                if bid.seat not in self.passed_seats
                and bid.seat not in self.speaking
                and seats_by_id[bid.seat].eagerness > 0
            ]
            threshold = liveliness_threshold(
                room.liveliness, self.bid_turns_since_owner, self.settings.router_threshold_floor
            )
            self._last_top_seat = None
            self._last_top_score = None
            if eligible_bids:
                top_bid = max(eligible_bids, key=lambda bid: bid.score)
                self._last_top_seat = top_bid.seat
                self._last_top_score = top_bid.score
            qualified = sorted(
                (bid for bid in eligible_bids if bid.score >= threshold),
                key=lambda bid: bid.score,
                reverse=True,
            )
            free_places = room.max_concurrent - len(self.speaking)
            safety_left = self.settings.max_auto_turns - self.auto_turns_since_owner
            chosen = qualified[: max(0, min(free_places, safety_left))]
            await self.broadcast(
                {
                    "type": "bids",
                    "bids": [bid.model_dump(mode="json") for bid in adjusted_bids],
                    "chosen": [bid.seat for bid in chosen],
                    "threshold": threshold,
                    "router": router.name,
                    "kind": question_kind,
                }
            )
            started = 0
            woken_by = (
                WOKEN_BY_AFTERTHOUGHT if question_kind == AFTERTHOUGHT_QUESTION else WOKEN_BY_BID
            )
            for bid in chosen:
                if self._start_seat(bid.seat, woken_by, bid.score) is not None:
                    started += 1
                    self.bid_turns_since_owner += 1
            return started

    async def _ask_router(
        self, transcript: str, bidders: list[Seat], question_kind: str
    ) -> tuple[Router, list[Bid]]:
        library = self._persona_library()
        router = self.router
        try:
            return router, await router.bids(transcript, bidders, library, question_kind)
        except RouterError as error:
            logger.warning(
                "router %s failed, using %s: %s", router.name, self.fallback_router.name, error
            )
            if not self.router_failure_reported:
                self.router_failure_reported = True
                await self._post_system(
                    f"Router {router.name} failed ({error}); using {self.fallback_router.name}."
                )
            router = self.fallback_router
            return router, await router.bids(transcript, bidders, library, question_kind)

    # One reply

    async def _run_seat_turn(
        self, seat_id: str, woken_by: str, bid_score: float | None
    ) -> tuple[Message | None, bool]:
        """Run one seat; return its posted message and whether it asked to continue with
        `[more]`. The message is None if the seat passed, failed or is gone."""
        room = self.current_room()
        seat = next((candidate for candidate in room.seats if candidate.id == seat_id), None)
        if seat is None or not seat.enabled:
            return None, False  # removed or disabled after it was queued
        adapter = self.adapters[seat.id]
        if not seat.available:
            await self._post_system(f"Seat {seat.name}: program {seat.program} not found.")
            return None, False
        try:
            persona = self._persona_for(seat)
        except PersonaError as error:
            await self._post_system(f"Seat {seat.name}: {error}")
            return None, False

        # The prompt holds what the seat has not seen at the moment it starts; replies posted
        # while it composes reach it with its next prompt.
        messages = self.store.list_thread_messages(self.thread_id)
        unseen = self._unseen_messages(seat.id, messages)
        prompt = turn_prompt(
            room,
            unseen,
            seat,
            self.settings.owner_name,
            woken_by,
            thread_title=self.current_thread().title,
        )
        other_seats = [other for other in room.seats if other.id != seat.id and other.enabled]
        seat_system_prompt = system_prompt(
            seat,
            persona,
            other_seats,
            self.settings.owner_name,
            self.settings.owner_line,
            str(self._working_directory(room)),
        )
        session_id = self.store.get_session(self.thread_id, seat.id)

        started_at = self.clock()
        await self.broadcast({"type": "typing", "seat": seat.id, "state": "start"})
        try:
            reply = await adapter.run(
                prompt, seat_system_prompt, session_id, self.settings.seat_timeout_s
            )
        except SeatError as error:
            await self._post_system(f"Seat {seat.name} failed: {error.detail}")
            return None, False
        finally:
            self.last_spoke_at[seat.id] = self.clock()
            await self.broadcast({"type": "typing", "seat": seat.id, "state": "stop"})

        latency_ms = round((self.clock() - started_at) * 1000)
        if messages:
            self.seen_until[seat.id] = messages[-1].id
        if reply.session_id:
            self.store.set_session(self.thread_id, seat.id, reply.session_id)
        text, wants_more = split_more_marker(reply.text.strip())
        text = text.strip()
        if is_pass(reply.text) or not text:
            logger.info("seat %s passed", seat.name)
            self.passed_seats.add(seat.id)
            await self.broadcast({"type": "passed", "seat": seat.id})
            await self._record_spend(reply.cost_usd)  # a PASS still cost money
            return None, False

        meta: dict[str, Any] = {
            "model": self._effective_model(seat),
            "latency_ms": latency_ms,
            "woken_by": woken_by,
        }
        if bid_score is not None:
            meta["bid_score"] = bid_score
        message = Message(
            room_id=self.room_id,
            thread_id=self.thread_id,
            seat_id=seat.id,
            author=seat.name,
            role="seat",
            text=text,
            mentions=list(resolve_mentions(text, room.seats, seat.id)),
            cost_usd=reply.cost_usd,
            meta=meta,
        )
        await self._post(message)
        await self._record_spend(reply.cost_usd)
        return message, wants_more

    async def _record_spend(self, cost_usd: float | None) -> None:
        if cost_usd:
            self.store.add_spend(self.room_id, cost_usd)
            await self.publish_room()

    def _unseen_messages(self, seat_id: str, messages: list[Message]) -> list[Message]:
        """Messages by others after the last one this seat saw, or after its own last message.

        The seat's own replies are left out: its resumed session already holds them."""
        boundary_id = self.seen_until.get(seat_id)
        if boundary_id is None:
            for message in reversed(messages):
                if message.seat_id == seat_id:
                    boundary_id = message.id
                    break
        if boundary_id is not None:
            for index, message in enumerate(messages):
                if message.id == boundary_id:
                    return [m for m in messages[index + 1 :] if m.seat_id != seat_id]
        return messages[-FIRST_TURN_HISTORY_LIMIT:]

    async def _collect_memory_notes(
        self, seat: Seat, persona: Persona, room: Room, messages: list[Message]
    ) -> list[str]:
        adapter = self.adapters[seat.id]
        unseen = self._unseen_messages(seat.id, messages)
        prompt = END_OF_DISCUSSION_PROMPT
        if unseen:
            owner_name = self.settings.owner_name
            thread_title = self.current_thread().title
            catch_up = turn_prompt(room, unseen, seat, owner_name, thread_title=thread_title)
            prompt = f"{catch_up}\n\n{END_OF_DISCUSSION_PROMPT}"
        other_seats = [other for other in room.seats if other.id != seat.id and other.enabled]
        session_id = self.store.get_session(self.thread_id, seat.id)
        await self.broadcast({"type": "typing", "seat": seat.id, "state": "start"})
        try:
            reply = await adapter.run(
                prompt,
                system_prompt(
                    seat,
                    persona,
                    other_seats,
                    self.settings.owner_name,
                    self.settings.owner_line,
                    str(self._working_directory(room)),
                ),
                session_id,
                self.settings.seat_timeout_s,
            )
        except SeatError as error:
            await self._post_system(f"Memory notes for {seat.name} not written: {error.detail}")
            return []
        finally:
            await self.broadcast({"type": "typing", "seat": seat.id, "state": "stop"})
        if reply.cost_usd:
            self.store.add_spend(self.room_id, reply.cost_usd)

        bullets = parse_memory_bullets(reply.text)
        thread = self.current_thread()
        # Recorded in the note so the owner can review what each thread taught a persona.
        source = MemorySource(
            room_id=room.id, room_title=room.title, thread_id=thread.id, thread_title=thread.title
        )
        for bullet in bullets:
            title = note_title_from_bullet(bullet)
            write_memory_note(persona, title, bullet, source=source)
        return bullets

    # Personas

    def _persona_for(self, seat: Seat) -> Persona | None:
        """Loaded fresh each turn so edits and new memory notes show up immediately."""
        if not seat.persona:
            return None
        return load_persona(self.settings.personas_dir, seat.persona)

    def _persona_library(self) -> dict[str, Persona]:
        try:
            return load_personas(self.settings.personas_dir)
        except PersonaError as error:
            logger.warning("persona library unreadable, bidding without it: %s", error)
            return {}

    # Posting

    async def _post(self, message: Message) -> None:
        self.store.add_message(message)
        await self.broadcast({"type": "message", "message": message.model_dump(mode="json")})

    async def _post_system(self, text: str) -> None:
        await self._post(
            Message(
                room_id=self.room_id,
                thread_id=self.thread_id,
                author="system",
                role="system",
                text=text,
            )
        )

    def _require_open_room(self) -> Room:
        room = self.store.get_room(self.room_id)
        if room.ended_at is not None:
            raise RoomEndedError(f"room {self.room_id} is archived")
        if self.current_thread().ended_at is not None:
            raise ThreadEndedError(f"thread {self.thread_id} has ended")
        return room


def _whole_number(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float) or value != int(value):
        raise ValueError(f"{label} must be a whole number, got {value!r}")
    return int(value)


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label} must be a number, got {value!r}")
    return float(value)
