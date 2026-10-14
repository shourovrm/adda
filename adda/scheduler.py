"""Turn-taking for one thread (SPEC section 5).

One Scheduler per thread owns at most one asyncio.Task: the conversation driver that picks a
speaker, runs the seat, posts its message and loops until the room goes quiet. An owner message
cancels the driver (interrupt) and starts a new one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from adda.config import Settings
from adda.models import Message, Seat, Thread
from adda.personas import Persona, write_memory_note
from adda.prompts import (
    END_OF_DISCUSSION_PROMPT,
    find_mentions,
    parse_memory_bullets,
    render_transcript,
    system_prompt,
    turn_prompt,
)
from adda.routers.base import Router, RouterError
from adda.seats.base import SeatAdapter, SeatError, is_pass
from adda.store import Store

logger = logging.getLogger(__name__)

Broadcast = Callable[[dict[str, Any]], Awaitable[None]]

# A seat with no session yet gets at most this many earlier messages in its first prompt.
FIRST_TURN_HISTORY_LIMIT = 50
MEMORY_NOTE_TITLE_LENGTH = 80


class ThreadEndedError(RuntimeError):
    """The thread has ended; it takes no new messages."""


class UnknownSeatError(LookupError):
    """A seat name that is not in the room."""


class Scheduler:
    def __init__(
        self,
        thread_id: str,
        store: Store,
        settings: Settings,
        seats: list[Seat],
        adapters: dict[str, SeatAdapter],
        personas: dict[str, Persona],
        router: Router,
        fallback_router: Router,
        broadcast: Broadcast,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.thread_id = thread_id
        self.store = store
        self.settings = settings
        self.seats = seats
        self.adapters = adapters
        self.personas = personas
        self.router = router
        self.fallback_router = fallback_router
        self.broadcast = broadcast
        self.clock = clock

        self.driver_task: asyncio.Task | None = None
        self.speaking_seat: str | None = None
        self.auto_turns_since_owner = 0
        self.last_spoke_at: dict[str, float] = {}
        # Id of the newest message each seat has already been shown, so later prompts carry
        # only what is new to that seat's resumed tool session.
        self.seen_until: dict[str, str] = {}
        self.router_failure_reported = False

    @property
    def seat_names(self) -> list[str]:
        return [seat.name for seat in self.seats]

    @property
    def active_seats(self) -> list[Seat]:
        """Seats whose program is installed: the only ones bidding and round-robin may pick.

        A mention of an absent seat still reaches _run_seat_turn, which posts a clear system
        message; but the router should not spend tokens scoring a seat that cannot speak.
        """
        return [seat for seat in self.seats if seat.available]

    # Owner actions

    async def say(self, text: str) -> Message:
        """Post an owner message: interrupt the speaker, then let the room respond."""
        self._require_open_thread()
        await self.stop()
        message = Message(
            thread_id=self.thread_id,
            author="owner",
            role="owner",
            text=text,
            mentions=find_mentions(text, self.seat_names),
        )
        await self._post(message)
        self.auto_turns_since_owner = 0
        self._start_driver(message, forced_seats=[])
        return message

    async def wake(self, seat_name: str) -> None:
        """Same as the owner mentioning the seat, without posting a message."""
        if seat_name not in self.seat_names:
            raise UnknownSeatError(f"no seat named {seat_name!r}")
        self._require_open_thread()
        await self.stop()
        self.auto_turns_since_owner = 0
        self._start_driver(None, forced_seats=[seat_name])

    async def set_mode(self, mode: str) -> Thread:
        thread = self.store.set_mode(self.thread_id, mode)
        await self.broadcast({"type": "mode", "mode": thread.mode})
        return thread

    async def stop(self) -> None:
        """Cancel the driver; the seat runner kills the child process on cancellation."""
        task = self.driver_task
        if task is None or task.done():
            return
        task.cancel()
        # asyncio.wait does not re-raise the task's CancelledError into this coroutine.
        await asyncio.wait([task])

    async def end(self) -> dict[str, list[str]]:
        """Ask every seat that spoke what to remember, write the notes, mark the thread ended."""
        self._require_open_thread()
        await self.stop()
        thread = self.store.get_thread(self.thread_id)
        messages = self.store.list_messages(self.thread_id)
        seats_that_spoke = []
        for message in messages:
            if message.role == "seat" and message.author not in seats_that_spoke:
                seats_that_spoke.append(message.author)

        notes: dict[str, list[str]] = {}
        for seat_name in seats_that_spoke:
            if seat_name not in self.adapters or seat_name not in self.personas:
                continue
            notes[seat_name] = await self._collect_memory_notes(seat_name, thread, messages)

        self.store.end_thread(self.thread_id)
        await self.broadcast({"type": "ended", "notes": notes})
        return notes

    # Driver

    def _start_driver(self, trigger: Message | None, forced_seats: list[str]) -> None:
        self.driver_task = asyncio.create_task(self._drive(trigger, forced_seats))

    async def _drive(self, trigger: Message | None, forced_seats: list[str]) -> None:
        try:
            await self._drive_until_quiet(trigger, forced_seats)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Nobody awaits the driver task, so report here instead of losing the error.
            logger.exception("turn-taking failed in thread %s", self.thread_id)
            await self.broadcast({"type": "error", "text": f"turn-taking failed: {error}"})

    async def _drive_until_quiet(self, trigger: Message | None, forced_seats: list[str]) -> None:
        pending_seats: list[str] = list(forced_seats)
        latest_message = trigger
        if trigger is not None:
            self._queue_mentions(pending_seats, trigger)
        thread = self.store.get_thread(self.thread_id)
        roundrobin_queue: list[str] = []
        if thread.mode == "roundrobin" and trigger is not None and trigger.role == "owner":
            roundrobin_queue = [seat.name for seat in self.active_seats]
        passed_seats: set[str] = set()

        while True:
            if self.auto_turns_since_owner >= self.settings.max_auto_turns:
                await self.broadcast({"type": "quiet"})
                return

            thread = self.store.get_thread(self.thread_id)
            bid_score: float | None = None
            if pending_seats:
                next_seat = pending_seats.pop(0)
            elif thread.mode == "roundrobin":
                next_seat = roundrobin_queue.pop(0) if roundrobin_queue else None
            elif thread.mode == "bidding" and latest_message is not None:
                next_seat, bid_score = await self._choose_by_bids(latest_message, passed_seats)
            else:
                next_seat = None

            if next_seat is None:
                await self.broadcast({"type": "quiet"})
                return

            if next_seat in roundrobin_queue:
                roundrobin_queue.remove(next_seat)
            posted = await self._run_seat_turn(next_seat, bid_score)
            if posted is None:
                passed_seats.add(next_seat)
                continue
            self.auto_turns_since_owner += 1
            latest_message = posted
            passed_seats = set()
            self._queue_mentions(pending_seats, posted)

    def _queue_mentions(self, pending_seats: list[str], message: Message) -> None:
        for seat_name in message.mentions:
            if seat_name != message.author and seat_name not in pending_seats:
                pending_seats.append(seat_name)

    async def _choose_by_bids(
        self, latest_message: Message, passed_seats: set[str]
    ) -> tuple[str | None, float | None]:
        window = self.store.list_messages(self.thread_id, last=self.settings.router_window)
        transcript = render_transcript(window)
        router = self.router
        try:
            bids = await router.bids(transcript, self.active_seats, self.personas)
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
            bids = await router.bids(transcript, self.active_seats, self.personas)

        now = self.clock()
        active_names = {seat.name for seat in self.active_seats}
        eligible_bids = []
        for bid in bids:
            # A router may answer for seats it was not asked about; only installed ones count.
            if bid.seat not in active_names or bid.seat == latest_message.author:
                continue
            if bid.seat in passed_seats:
                continue
            last_spoke = self.last_spoke_at.get(bid.seat)
            if last_spoke is not None and now - last_spoke < self.settings.cooldown_s:
                continue
            eligible_bids.append(bid)

        chosen = None
        if eligible_bids:
            top_bid = max(eligible_bids, key=lambda bid: bid.score)
            if top_bid.score >= self.settings.bid_threshold:
                chosen = top_bid
        await self.broadcast(
            {
                "type": "bids",
                "bids": [bid.model_dump(mode="json") for bid in bids if bid.seat in active_names],
                "chosen": None if chosen is None else chosen.seat,
                "router": router.name,
            }
        )
        if chosen is None:
            return None, None
        return chosen.seat, chosen.score

    async def _run_seat_turn(self, seat_name: str, bid_score: float | None) -> Message | None:
        """Run one seat; return its posted message, or None if it passed, failed or is absent."""
        adapter = self.adapters.get(seat_name)
        persona = self.personas.get(seat_name)
        if adapter is None or persona is None:
            await self._post_system(f"Seat {seat_name} has no persona or adapter.")
            return None
        if not adapter.available():
            await self._post_system(f"Seat {seat_name}: program {adapter.program} not found.")
            return None

        thread = self.store.get_thread(self.thread_id)
        messages = self.store.list_messages(self.thread_id)
        unseen = self._unseen_messages(seat_name, messages)
        prompt = turn_prompt(thread, unseen, seat_name)
        seat_system_prompt = system_prompt(persona, self.seat_names)
        session_id = self.store.get_session(self.thread_id, seat_name)

        started_at = self.clock()
        self.speaking_seat = seat_name
        await self.broadcast({"type": "typing", "seat": seat_name, "state": "start"})
        try:
            reply = await adapter.run(
                prompt, seat_system_prompt, session_id, self.settings.seat_timeout_s
            )
        except SeatError as error:
            await self._post_system(str(error))
            return None
        finally:
            self.speaking_seat = None
            self.last_spoke_at[seat_name] = self.clock()
            await self.broadcast({"type": "typing", "seat": seat_name, "state": "stop"})

        latency_ms = round((self.clock() - started_at) * 1000)
        if messages:
            self.seen_until[seat_name] = messages[-1].id
        if reply.session_id:
            self.store.set_session(self.thread_id, seat_name, reply.session_id)
        if is_pass(reply.text):
            return None

        meta: dict[str, Any] = {"model": persona.model, "latency_ms": latency_ms}
        if bid_score is not None:
            meta["bid_score"] = bid_score
        message = Message(
            thread_id=self.thread_id,
            author=seat_name,
            role="seat",
            text=reply.text.strip(),
            mentions=find_mentions(reply.text, self.seat_names),
            cost_usd=reply.cost_usd,
            meta=meta,
        )
        await self._post(message)
        self.seen_until[seat_name] = message.id
        return message

    def _unseen_messages(self, seat_name: str, messages: list[Message]) -> list[Message]:
        """Messages after the last one this seat saw, or after its own last message."""
        boundary_id = self.seen_until.get(seat_name)
        if boundary_id is None:
            for message in reversed(messages):
                if message.author == seat_name:
                    boundary_id = message.id
                    break
        if boundary_id is not None:
            for index, message in enumerate(messages):
                if message.id == boundary_id:
                    return messages[index + 1 :]
        return messages[-FIRST_TURN_HISTORY_LIMIT:]

    async def _collect_memory_notes(
        self, seat_name: str, thread: Thread, messages: list[Message]
    ) -> list[str]:
        adapter = self.adapters[seat_name]
        persona = self.personas[seat_name]
        unseen = self._unseen_messages(seat_name, messages)
        prompt = END_OF_DISCUSSION_PROMPT
        if unseen:
            prompt = f"{turn_prompt(thread, unseen, seat_name)}\n\n{END_OF_DISCUSSION_PROMPT}"
        session_id = self.store.get_session(self.thread_id, seat_name)
        await self.broadcast({"type": "typing", "seat": seat_name, "state": "start"})
        try:
            reply = await adapter.run(
                prompt,
                system_prompt(persona, self.seat_names),
                session_id,
                self.settings.seat_timeout_s,
            )
        except SeatError as error:
            await self._post_system(f"Memory notes for {seat_name} not written: {error}")
            return []
        finally:
            await self.broadcast({"type": "typing", "seat": seat_name, "state": "stop"})

        bullets = parse_memory_bullets(reply.text)
        for bullet in bullets:
            title = bullet[:MEMORY_NOTE_TITLE_LENGTH].rstrip()
            write_memory_note(persona, title, bullet)
        return bullets

    # Posting

    async def _post(self, message: Message) -> None:
        self.store.add_message(message)
        await self.broadcast({"type": "message", "message": message.model_dump(mode="json")})

    async def _post_system(self, text: str) -> None:
        await self._post(
            Message(thread_id=self.thread_id, author="system", role="system", text=text)
        )

    def _require_open_thread(self) -> None:
        if self.store.get_thread(self.thread_id).ended_at is not None:
            raise ThreadEndedError(f"thread {self.thread_id} has ended")
