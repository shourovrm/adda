"""Turn-taking for one room (SPEC section 5).

One Scheduler per room owns at most one asyncio.Task, the conversation driver. The driver picks
a speaker (queued mentions first, then round-robin or bidding), runs the seat, posts its message
and loops until the room goes quiet. An owner message cancels the driver (interrupt) and starts
a new one. REST and WebSocket handlers both change rooms and seats through `apply_room_patch`
and `apply_seat_patch`, so there is one code path for validation and for the `room` frame.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from adda.config import Settings
from adda.models import MODES, THINKING_LEVELS, Bid, Message, Room, Seat
from adda.personas import Persona, PersonaError, load_persona, load_personas, write_memory_note
from adda.prompts import (
    END_OF_DISCUSSION_PROMPT,
    parse_memory_bullets,
    render_transcript,
    resolve_mentions,
    system_prompt,
    turn_prompt,
)
from adda.routers.base import Router, RouterError
from adda.seats.base import SeatAdapter, SeatError, is_pass
from adda.seats.registry import build_adapter
from adda.store import Store

logger = logging.getLogger(__name__)

Broadcast = Callable[[dict[str, Any]], Awaitable[None]]
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

ROOM_PATCH_FIELDS = ("title", "mode", "liveliness", "budget_usd")
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
)
# Text fields where the UI sends "" to mean "unset".
EMPTY_MEANS_NONE_FIELDS = ("model", "persona", "instruction")


class RoomEndedError(RuntimeError):
    """The room has ended; it takes no new messages."""


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


def adjusted_bid_score(raw_score: float, eagerness: int, cooling_down: bool) -> float:
    """Jev's score shifted by the seat's eagerness and the cooldown penalty, kept within 0..10.

    Eagerness 5 changes nothing; each step moves the score by 0.4. A seat that spoke less than
    `cooldown_s` ago loses COOLDOWN_PENALTY, so it can still win when it is far ahead.
    """
    adjusted = raw_score + EAGERNESS_STEP * (eagerness - 5)
    if cooling_down:
        adjusted -= COOLDOWN_PENALTY
    return min(10.0, max(0.0, adjusted))


def budget_exhausted(room: Room) -> bool:
    return room.budget_usd > 0 and room.spent_usd >= room.budget_usd


def budget_message(room: Room) -> str:
    return f"Budget of ${room.budget_usd:.2f} reached; raise it to continue."


class Scheduler:
    def __init__(
        self,
        room_id: str,
        store: Store,
        settings: Settings,
        router: Router,
        fallback_router: Router,
        broadcast: Broadcast,
        adapter_builder: AdapterBuilder = build_adapter,
        harness_versions: Callable[[], dict[str, str | None]] = dict,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.room_id = room_id
        self.store = store
        self.settings = settings
        self.router = router
        self.fallback_router = fallback_router
        self.broadcast = broadcast
        self.adapter_builder = adapter_builder
        self.harness_versions = harness_versions
        self.clock = clock

        # Adapters by seat id, with the inputs each was built from so a change rebuilds it.
        self.adapters: dict[str, SeatAdapter] = {}
        self._adapter_signatures: dict[str, tuple] = {}

        self.driver_task: asyncio.Task | None = None
        self.speaking_seat: str | None = None
        # All automatic turns, for the safety cap; and bid-driven turns only, which raise the
        # threshold (k in liveliness_threshold). Both reset on an owner message or wake.
        self.auto_turns_since_owner = 0
        self.bid_turns_since_owner = 0
        self.last_spoke_at: dict[str, float] = {}
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
        """Build adapters for new seats, rebuild those whose program, model or thinking changed
        (or whose harness defaults changed), and drop those of removed seats."""
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
                str(working_directory),
                harness_settings,
            )
            if self._adapter_signatures.get(seat.id) != signature:
                self.adapters[seat.id] = self.adapter_builder(
                    seat, harness_settings, working_directory
                )
                self._adapter_signatures[seat.id] = signature

    def _effective_model(self, seat: Seat) -> str | None:
        harness_settings = self.settings.harnesses.get(seat.program)
        return seat.model or (harness_settings.model if harness_settings else "") or None

    async def publish_room(self) -> Room:
        """Send the current room to every tab (any change: spend, seats, liveliness...)."""
        room = self.current_room()
        await self.broadcast({"type": "room", "room": room.model_dump(mode="json")})
        return room

    # Patches (one code path for REST and WebSocket)

    async def apply_room_patch(self, patch: dict[str, Any]) -> Room:
        """Change title, mode, liveliness or budget. Raises ValueError for anything invalid."""
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
        if "liveliness" in patch:
            fields["liveliness"] = _whole_number(patch["liveliness"], "liveliness")
        if "budget_usd" in patch:
            fields["budget_usd"] = _number(patch["budget_usd"], "budget_usd")
        self.store.update_room(self.room_id, **fields)
        if "budget_usd" in fields:
            # A new budget deserves a fresh notice if it is still exceeded.
            self._budget_notice_for = None
        return await self.publish_room()

    async def apply_seat_patch(self, seat_id: str, patch: dict[str, Any]) -> Room:
        """Change one seat. Name, persona and instruction apply from the next prompt; program,
        model and thinking rebuild the adapter. Raises ValueError or UnknownSeatError."""
        self._require_seat(seat_id)
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
            elif field_name == "thinking" and value not in THINKING_LEVELS:
                raise ValueError(f"thinking must be one of {THINKING_LEVELS}, got {value!r}")
            fields[field_name] = value
        if fields.get("persona"):
            load_persona(
                self.settings.personas_dir, fields["persona"]
            )  # PersonaError is a ValueError
        self.store.update_seat(seat_id, **fields)
        return await self.publish_room()

    async def remove_seat(self, seat_id: str) -> Room:
        """Remove a seat; if it is speaking right now, cancel the driver first."""
        self._require_seat(seat_id)
        if self.speaking_seat == seat_id:
            await self.stop()
        self.store.remove_seat(seat_id)
        self.seen_until.pop(seat_id, None)
        self.last_spoke_at.pop(seat_id, None)
        return await self.publish_room()

    def _require_seat(self, seat_id: str) -> Seat:
        for seat in self.store.get_room(self.room_id).seats:
            if seat.id == seat_id:
                return seat
        raise UnknownSeatError(f"no seat {seat_id!r} in room {self.room_id}")

    def _reset_turn_counters(self) -> None:
        self.auto_turns_since_owner = 0
        self.bid_turns_since_owner = 0

    # Owner actions

    async def say(self, text: str) -> Message:
        """Post an owner message: interrupt the speaker, then let the room respond."""
        room = self._require_open_room()
        await self.stop()
        room = self.current_room()
        wakes = resolve_mentions(text, room.seats, author_seat_id=None)
        message = Message(
            room_id=self.room_id,
            author="owner",
            role="owner",
            text=text,
            mentions=list(wakes),
        )
        await self._post(message)
        self._reset_turn_counters()
        forced = list(wakes.items())
        if forced and budget_exhausted(room):
            forced = []
            await self.broadcast({"type": "error", "text": budget_message(room)})
        self._start_driver(message, forced)
        return message

    async def wake(self, seat_id: str) -> None:
        """Same as the owner mentioning the seat, without posting a message."""
        room = self._require_open_room()
        seat = self._require_seat(seat_id)
        if not seat.enabled:
            raise ValueError(f"seat {seat.name} is disabled")
        self._refuse_when_over_budget(room)
        await self.stop()
        self._reset_turn_counters()
        self._start_driver(None, [(seat_id, WOKEN_BY_WAKE)])

    async def wake_all(self) -> None:
        """Wake every enabled, installed seat in position order."""
        room = self._require_open_room()
        self._refuse_when_over_budget(room)
        room = self.current_room()
        forced = [(seat.id, WOKEN_BY_ALL) for seat in room.seats if seat.enabled and seat.available]
        if not forced:
            raise ValueError("no enabled seat is available to wake")
        await self.stop()
        self._reset_turn_counters()
        self._start_driver(None, forced)

    async def stop(self) -> None:
        """Cancel the driver; the seat runner kills the child process on cancellation."""
        task = self.driver_task
        if task is None or task.done():
            return
        task.cancel()
        # asyncio.wait does not re-raise the task's CancelledError into this coroutine.
        await asyncio.wait([task])

    async def end(self) -> dict[str, list[str]]:
        """Ask every persona-backed seat that spoke what to remember, write the notes, mark the
        room ended. Seats without a persona write no notes."""
        self._require_open_room()
        await self.stop()
        room = self.current_room()
        messages = self.store.list_messages(self.room_id)
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

        self.store.end_room(self.room_id)
        await self.broadcast({"type": "ended", "notes": notes})
        await self.publish_room()
        return notes

    # Driver

    def _start_driver(self, trigger: Message | None, forced: list[tuple[str, str]]) -> None:
        self.driver_task = asyncio.create_task(self._drive(trigger, forced))

    async def _drive(self, trigger: Message | None, forced: list[tuple[str, str]]) -> None:
        try:
            await self._drive_until_quiet(trigger, forced)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Nobody awaits the driver task, so report here instead of losing the error.
            logger.exception("turn-taking failed in room %s", self.room_id)
            await self.broadcast({"type": "error", "text": f"turn-taking failed: {error}"})

    async def _drive_until_quiet(self, trigger: Message | None, forced: list[tuple[str, str]]):
        pending: list[tuple[str, str]] = list(forced)
        self._last_top_seat = None
        self._last_top_score = None
        latest_message = trigger
        room = self.current_room()
        roundrobin_queue: list[str] = []
        if room.mode == "roundrobin" and trigger is not None and trigger.role == "owner":
            roundrobin_queue = [seat.id for seat in room.seats if seat.enabled and seat.available]
        passed_seats: set[str] = set()

        while True:
            room = self.store.get_room(self.room_id)
            if room.ended_at is not None:
                return
            if budget_exhausted(room):
                await self._stop_for_budget(room)
                return
            if self.auto_turns_since_owner >= self.settings.max_auto_turns:
                await self._quiet("safety", room)
                return

            bid_score: float | None = None
            if pending:
                seat_id, woken_by = pending.pop(0)
            elif room.liveliness == 0:
                await self._quiet("paused", room)
                return
            elif room.mode == "roundrobin":
                if not roundrobin_queue:
                    await self._quiet("nobody", room)
                    return
                seat_id, woken_by = roundrobin_queue.pop(0), WOKEN_BY_ROUNDROBIN
            elif room.mode == "bidding" and latest_message is not None:
                chosen_id, bid_score = await self._choose_by_bids(
                    room, latest_message, passed_seats
                )
                if chosen_id is None:
                    await self._quiet("nobody", room)
                    return
                seat_id, woken_by = chosen_id, WOKEN_BY_BID
            else:
                await self._quiet("nobody", room)
                return

            if seat_id in roundrobin_queue:
                roundrobin_queue.remove(seat_id)
            posted = await self._run_seat_turn(seat_id, woken_by, bid_score)
            if posted is None:
                passed_seats.add(seat_id)
                continue
            self.auto_turns_since_owner += 1
            if woken_by == WOKEN_BY_BID:
                self.bid_turns_since_owner += 1
            latest_message = posted
            passed_seats = set()
            fresh_room = self.current_room()
            queued_ids = {queued_id for queued_id, _ in pending}
            for mentioned_id, reason in resolve_mentions(
                posted.text, fresh_room.seats, posted.seat_id
            ).items():
                if mentioned_id not in queued_ids:
                    pending.append((mentioned_id, reason))

    async def _stop_for_budget(self, room: Room) -> None:
        if self._budget_notice_for != room.budget_usd:
            self._budget_notice_for = room.budget_usd
            await self._post_system(budget_message(room))
        await self._quiet("budget", room)

    async def _quiet(self, reason: str, room: Room) -> None:
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
        await self.broadcast({"type": "quiet", "reason": reason})

    def _refuse_when_over_budget(self, room: Room) -> None:
        if budget_exhausted(room):
            raise BudgetReachedError(budget_message(room))

    async def _choose_by_bids(
        self, room: Room, latest_message: Message, passed_seats: set[str]
    ) -> tuple[str | None, float | None]:
        described = self.describe(room)
        bidders = [seat for seat in described.seats if seat.enabled and seat.available]
        if not bidders:
            return None, None
        window = self.store.list_messages(self.room_id, last=self.settings.router_window)
        transcript = render_transcript(window, self.settings.owner_name)
        library = self._persona_library()
        router = self.router
        try:
            bids = await router.bids(transcript, bidders, library)
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
            bids = await router.bids(transcript, bidders, library)

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
            cooling_down = last_spoke is not None and now - last_spoke < self.settings.cooldown_s
            adjusted_bids.append(
                bid.model_copy(
                    update={
                        "score": adjusted_bid_score(bid.score, seat.eagerness, cooling_down),
                        "raw_score": bid.score,
                    }
                )
            )

        def may_speak(bid: Bid) -> bool:
            # The author of the latest message never answers itself; a seat that just passed
            # stays out until someone else speaks; eagerness 0 never bids.
            return (
                bid.seat != latest_message.seat_id
                and bid.seat not in passed_seats
                and seats_by_id[bid.seat].eagerness > 0
            )

        eligible_bids = [bid for bid in adjusted_bids if may_speak(bid)]
        threshold = liveliness_threshold(
            room.liveliness, self.bid_turns_since_owner, self.settings.router_threshold_floor
        )
        chosen = None
        self._last_top_seat = None
        self._last_top_score = None
        if eligible_bids:
            top_bid = max(eligible_bids, key=lambda bid: bid.score)
            self._last_top_seat = top_bid.seat
            self._last_top_score = top_bid.score
            if top_bid.score >= threshold:
                chosen = top_bid
        await self.broadcast(
            {
                "type": "bids",
                "bids": [bid.model_dump(mode="json") for bid in adjusted_bids],
                "chosen": None if chosen is None else chosen.seat,
                "threshold": threshold,
                "router": router.name,
            }
        )
        if chosen is None:
            return None, None
        return chosen.seat, chosen.score

    async def _run_seat_turn(
        self, seat_id: str, woken_by: str, bid_score: float | None
    ) -> Message | None:
        """Run one seat; return its posted message, or None if it passed, failed or is gone."""
        room = self.current_room()
        seat = next((candidate for candidate in room.seats if candidate.id == seat_id), None)
        if seat is None or not seat.enabled:
            return None  # removed or disabled after it was queued
        adapter = self.adapters[seat.id]
        if not seat.available:
            await self._post_system(f"Seat {seat.name}: program {seat.program} not found.")
            return None
        try:
            persona = self._persona_for(seat)
        except PersonaError as error:
            await self._post_system(f"Seat {seat.name}: {error}")
            return None

        messages = self.store.list_messages(self.room_id)
        unseen = self._unseen_messages(seat.id, messages)
        prompt = turn_prompt(room, unseen, seat, self.settings.owner_name)
        other_seats = [other for other in room.seats if other.id != seat.id and other.enabled]
        seat_system_prompt = system_prompt(
            seat, persona, other_seats, self.settings.owner_name, self.settings.owner_line
        )
        session_id = self.store.get_session(self.room_id, seat.id)

        started_at = self.clock()
        self.speaking_seat = seat.id
        await self.broadcast({"type": "typing", "seat": seat.id, "state": "start"})
        try:
            reply = await adapter.run(
                prompt, seat_system_prompt, session_id, self.settings.seat_timeout_s
            )
        except SeatError as error:
            await self._post_system(f"Seat {seat.name} failed: {error.detail}")
            return None
        finally:
            self.speaking_seat = None
            self.last_spoke_at[seat.id] = self.clock()
            await self.broadcast({"type": "typing", "seat": seat.id, "state": "stop"})

        latency_ms = round((self.clock() - started_at) * 1000)
        if messages:
            self.seen_until[seat.id] = messages[-1].id
        if reply.session_id:
            self.store.set_session(self.room_id, seat.id, reply.session_id)
        if is_pass(reply.text):
            logger.info("seat %s passed", seat.name)
            await self.broadcast({"type": "passed", "seat": seat.id})
            await self._record_spend(reply.cost_usd)  # a PASS still cost money
            return None

        meta: dict[str, Any] = {
            "model": self._effective_model(seat),
            "latency_ms": latency_ms,
            "woken_by": woken_by,
        }
        if bid_score is not None:
            meta["bid_score"] = bid_score
        text = reply.text.strip()
        message = Message(
            room_id=self.room_id,
            seat_id=seat.id,
            author=seat.name,
            role="seat",
            text=text,
            mentions=list(resolve_mentions(text, room.seats, seat.id)),
            cost_usd=reply.cost_usd,
            meta=meta,
        )
        await self._post(message)
        self.seen_until[seat.id] = message.id
        await self._record_spend(reply.cost_usd)
        return message

    async def _record_spend(self, cost_usd: float | None) -> None:
        if cost_usd:
            self.store.add_spend(self.room_id, cost_usd)
            await self.publish_room()

    def _unseen_messages(self, seat_id: str, messages: list[Message]) -> list[Message]:
        """Messages after the last one this seat saw, or after its own last message."""
        boundary_id = self.seen_until.get(seat_id)
        if boundary_id is None:
            for message in reversed(messages):
                if message.seat_id == seat_id:
                    boundary_id = message.id
                    break
        if boundary_id is not None:
            for index, message in enumerate(messages):
                if message.id == boundary_id:
                    return messages[index + 1 :]
        return messages[-FIRST_TURN_HISTORY_LIMIT:]

    async def _collect_memory_notes(
        self, seat: Seat, persona: Persona, room: Room, messages: list[Message]
    ) -> list[str]:
        adapter = self.adapters[seat.id]
        unseen = self._unseen_messages(seat.id, messages)
        prompt = END_OF_DISCUSSION_PROMPT
        if unseen:
            owner_name = self.settings.owner_name
            prompt = f"{turn_prompt(room, unseen, seat, owner_name)}\n\n{END_OF_DISCUSSION_PROMPT}"
        other_seats = [other for other in room.seats if other.id != seat.id and other.enabled]
        session_id = self.store.get_session(self.room_id, seat.id)
        await self.broadcast({"type": "typing", "seat": seat.id, "state": "start"})
        try:
            reply = await adapter.run(
                prompt,
                system_prompt(
                    seat, persona, other_seats, self.settings.owner_name, self.settings.owner_line
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
        for bullet in bullets:
            title = bullet[:MEMORY_NOTE_TITLE_LENGTH].rstrip()
            write_memory_note(persona, title, bullet)
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
        await self._post(Message(room_id=self.room_id, author="system", role="system", text=text))

    def _require_open_room(self) -> Room:
        room = self.store.get_room(self.room_id)
        if room.ended_at is not None:
            raise RoomEndedError(f"room {self.room_id} has ended")
        return room


def _whole_number(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float) or value != int(value):
        raise ValueError(f"{label} must be a whole number, got {value!r}")
    return int(value)


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label} must be a number, got {value!r}")
    return float(value)
