"""Tests for the turn-taking scheduler (SPEC section 5), using fake seats and routers only."""

import asyncio
from collections import deque
from dataclasses import dataclass, field

import pytest

from adda.config import Settings
from adda.models import Bid, Seat, SeatReply
from adda.personas import Persona
from adda.prompts import END_OF_DISCUSSION_PROMPT
from adda.routers.base import RouterError
from adda.routers.heuristic import HeuristicRouter
from adda.scheduler import Scheduler, ThreadEndedError, UnknownSeatError
from adda.seats.base import SeatError, SeatTimeoutError
from adda.store import Store

START_OF_CLOCK = 1000.0


class FakeClock:
    """A clock the test advances by hand, so cooldowns and latencies are exact."""

    def __init__(self) -> None:
        self.now = START_OF_CLOCK

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeAdapter:
    """Returns scripted replies (SeatReply or an exception to raise) and records every call."""

    def __init__(self, name: str, clock: FakeClock, available: bool = True) -> None:
        self.name = name
        self.program = "claude"
        self.is_available = available
        self.clock = clock
        self.scripted: deque[SeatReply | Exception] = deque()
        self.calls: list[dict] = []
        self.seconds_per_run = 0.0
        # When set, run() waits on this event so a test can interrupt a seat mid-turn.
        self.block_until: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.was_cancelled = False

    def available(self) -> bool:
        return self.is_available

    def script(self, *replies: SeatReply | Exception | str) -> None:
        for reply in replies:
            self.scripted.append(SeatReply(text=reply) if isinstance(reply, str) else reply)

    async def run(
        self, prompt: str, system_prompt: str, session_id: str | None, timeout_s: float
    ) -> SeatReply:
        self.calls.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "session_id": session_id,
                "timeout_s": timeout_s,
            }
        )
        self.started.set()
        if self.block_until is not None:
            try:
                await self.block_until.wait()
            except asyncio.CancelledError:
                self.was_cancelled = True
                raise
        self.clock.advance(self.seconds_per_run)
        if not self.scripted:
            return SeatReply(text=f"default reply from {self.name}")
        reply = self.scripted.popleft()
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeRouter:
    """Returns one scripted bid list per call; when the script runs out nobody wants to speak."""

    def __init__(self, seat_names: list[str], name: str = "jev") -> None:
        self.name = name
        self.seat_names = seat_names
        self.scripted: deque[dict[str, float] | Exception] = deque()
        self.transcripts: list[str] = []

    def script(self, *entries: dict[str, float] | Exception) -> None:
        self.scripted.extend(entries)

    @property
    def call_count(self) -> int:
        return len(self.transcripts)

    async def bids(self, transcript, seats, personas):
        self.transcripts.append(transcript)
        if not self.scripted:
            return [Bid(seat=name, score=0.0) for name in self.seat_names]
        entry = self.scripted.popleft()
        if isinstance(entry, Exception):
            raise entry
        return [Bid(seat=name, score=score) for name, score in entry.items()]


@dataclass
class Room:
    scheduler: Scheduler
    store: Store
    thread_id: str
    clock: FakeClock
    router: FakeRouter
    adapters: dict[str, FakeAdapter]
    personas: dict[str, Persona]
    frames: list[dict] = field(default_factory=list)

    def frames_of_type(self, frame_type: str) -> list[dict]:
        return [frame for frame in self.frames if frame["type"] == frame_type]

    def messages(self, role: str | None = None):
        messages = self.store.list_messages(self.thread_id)
        return [message for message in messages if role is None or message.role == role]

    def seat_texts(self) -> list[str]:
        return [message.text for message in self.messages("seat")]

    async def say(self, text: str) -> None:
        await self.scheduler.say(text)
        await self.scheduler.driver_task


@pytest.fixture
def make_room(tmp_path):
    stores: list[Store] = []

    def build(seat_names=("alpha", "beta", "gamma"), mode="bidding", **settings_overrides) -> Room:
        settings_values = {"cooldown_s": 0.0, "max_auto_turns": 6, "bid_threshold": 5.0}
        settings_values.update(settings_overrides)
        settings = Settings(data_dir=tmp_path, **settings_values)
        store = Store(tmp_path / "adda.db")
        stores.append(store)
        thread = store.create_thread("Test thread", mode=mode)
        clock = FakeClock()
        seats = [
            Seat(
                name=name,
                program="claude",
                model=f"model-{name}",
                color="#123456",
                role="tester",
                available=True,
            )
            for name in seat_names
        ]
        personas = {
            name: Persona(
                name=name,
                program="claude",
                model=f"model-{name}",
                color="#123456",
                role="tester",
                stance="",
                body=f"You are {name}.",
                directory=tmp_path / "personas" / name,
            )
            for name in seat_names
        }
        adapters = {name: FakeAdapter(name, clock) for name in seat_names}
        router = FakeRouter(list(seat_names))
        frames: list[dict] = []

        async def collect_frame(frame: dict) -> None:
            frames.append(frame)

        scheduler = Scheduler(
            thread_id=thread.id,
            store=store,
            settings=settings,
            seats=seats,
            adapters=adapters,
            personas=personas,
            router=router,
            fallback_router=HeuristicRouter(),
            broadcast=collect_frame,
            clock=clock,
        )
        return Room(scheduler, store, thread.id, clock, router, adapters, personas, frames)

    yield build
    for store in stores:
        store.close()


# Bidding


async def test_top_bid_above_threshold_speaks_with_metadata(make_room):
    room = make_room()
    room.adapters["beta"].script(SeatReply(text="  beta answers  ", cost_usd=0.02))
    room.adapters["beta"].seconds_per_run = 0.25
    room.router.script({"alpha": 3.0, "beta": 8.0, "gamma": 6.0})

    await room.say("what do you think?")

    seat_message = room.messages("seat")[0]
    assert seat_message.author == "beta"
    assert seat_message.text == "beta answers"
    assert seat_message.cost_usd == 0.02
    assert seat_message.meta == {"model": "model-beta", "latency_ms": 250, "bid_score": 8.0}
    assert room.adapters["alpha"].calls == []
    assert room.adapters["gamma"].calls == []


async def test_all_bids_below_threshold_goes_quiet_without_running_a_seat(make_room):
    room = make_room()
    room.router.script({"alpha": 4.9, "beta": 2.0, "gamma": 0.0})

    await room.say("anyone?")

    assert room.messages("seat") == []
    assert all(adapter.calls == [] for adapter in room.adapters.values())
    assert room.frames_of_type("bids")[0]["chosen"] is None
    assert room.frames[-1] == {"type": "quiet"}


async def test_bid_exactly_at_threshold_speaks(make_room):
    room = make_room()
    room.router.script({"alpha": 5.0, "beta": 0.0, "gamma": 0.0})

    await room.say("hello")

    assert room.messages("seat")[0].author == "alpha"


async def test_author_of_latest_message_cannot_win_the_next_bid(make_room):
    room = make_room(seat_names=("alpha", "beta"), mode="manual")
    await room.scheduler.set_mode("bidding")
    room.router.script({"alpha": 9.0, "beta": 2.0})

    await room.scheduler.wake("alpha")
    await room.scheduler.driver_task

    # alpha spoke, so its own 9.0 is ignored and beta's 2.0 is below the threshold.
    assert [message.author for message in room.messages("seat")] == ["alpha"]
    assert room.adapters["alpha"].calls[0]["session_id"] is None
    assert len(room.adapters["alpha"].calls) == 1
    assert room.frames_of_type("bids")[0]["chosen"] is None


async def test_cooldown_excludes_recent_speaker_until_clock_advances(make_room):
    room = make_room(cooldown_s=20.0)
    room.router.script(
        {"alpha": 9.0, "beta": 0.0, "gamma": 0.0},
        {"alpha": 0.0, "beta": 0.0, "gamma": 0.0},
        {"alpha": 9.0, "beta": 6.0, "gamma": 0.0},
        {"alpha": 0.0, "beta": 0.0, "gamma": 0.0},
        {"alpha": 9.0, "beta": 6.0, "gamma": 0.0},
    )

    await room.say("first")
    room.clock.advance(5)
    await room.say("second")
    room.clock.advance(30)
    await room.say("third")

    # Second round: alpha spoke 5 s ago, so beta wins. Third round: 35 s later alpha is free.
    assert [message.author for message in room.messages("seat")] == ["alpha", "beta", "alpha"]


async def test_bids_frame_lists_bids_chosen_seat_and_router_name(make_room):
    room = make_room()
    room.router.script({"alpha": 3.0, "beta": 8.0, "gamma": 6.0})

    await room.say("hello")

    assert room.frames_of_type("bids")[0] == {
        "type": "bids",
        "bids": [
            {"seat": "alpha", "score": 3.0, "confidence": 1.0},
            {"seat": "beta", "score": 8.0, "confidence": 1.0},
            {"seat": "gamma", "score": 6.0, "confidence": 1.0},
        ],
        "chosen": "beta",
        "router": "jev",
    }


async def test_frames_arrive_in_turn_order(make_room):
    room = make_room()
    room.router.script({"alpha": 9.0, "beta": 0.0, "gamma": 0.0})

    await room.say("hello")

    frame_summaries = [
        (frame["type"], frame.get("state") or frame.get("message", {}).get("author"))
        for frame in room.frames
    ]
    assert frame_summaries == [
        ("message", "owner"),
        ("bids", None),
        ("typing", "start"),
        ("typing", "stop"),
        ("message", "alpha"),
        ("bids", None),
        ("quiet", None),
    ]


async def test_pass_reply_posts_nothing_and_next_bidder_gets_a_chance(make_room):
    room = make_room()
    room.adapters["alpha"].script("PASS")
    room.adapters["beta"].script("beta speaks")
    two_bidders = {"alpha": 9.0, "beta": 8.0, "gamma": 0.0}
    room.router.script(two_bidders, two_bidders)

    await room.say("hello")

    assert room.seat_texts() == ["beta speaks"]
    assert len(room.adapters["alpha"].calls) == 1


# Router failure


async def test_router_error_uses_fallback_and_reports_once_per_thread(make_room):
    room = make_room(seat_names=("alpha", "beta"), max_auto_turns=1)
    room.router.script(RouterError("http 500"), RouterError("http 500"))

    await room.say("first")
    await room.say("second")

    system_messages = room.messages("system")
    assert len(system_messages) == 1
    assert "jev" in system_messages[0].text
    assert "http 500" in system_messages[0].text
    assert "heuristic" in system_messages[0].text
    bids_frames = room.frames_of_type("bids")
    assert [frame["router"] for frame in bids_frames] == ["heuristic", "heuristic"]
    # The heuristic gives 6.0 to the longest-silent seat, which is above the 5.0 threshold.
    assert len(room.messages("seat")) == 2


async def test_router_error_notice_is_posted_even_when_fallback_picks_nobody(make_room):
    room = make_room(seat_names=("alpha",), mode="manual")
    await room.scheduler.set_mode("bidding")
    room.router.script(RouterError("no key"))

    await room.scheduler.wake("alpha")
    await room.scheduler.driver_task

    assert [message.author for message in room.messages("system")] == ["system"]


# Mentions


async def test_owner_mention_wakes_seat_without_consulting_router_for_it(make_room):
    room = make_room()

    await room.say("@beta what do you think?")

    seat_message = room.messages("seat")[0]
    assert seat_message.author == "beta"
    assert "bid_score" not in seat_message.meta
    # The router is only asked afterwards, about the reply, and the default script says quiet.
    assert room.router.call_count == 1
    assert "beta: default reply from beta" in room.router.transcripts[0]


async def test_mention_in_seat_reply_chain_wakes_the_mentioned_seat(make_room):
    room = make_room(mode="manual")
    room.adapters["alpha"].script("over to you @gamma")
    room.adapters["gamma"].script("thanks")

    await room.say("@alpha start")

    assert [message.author for message in room.messages("seat")] == ["alpha", "gamma"]
    assert room.messages("seat")[0].mentions == ["gamma"]
    assert room.router.call_count == 0
    assert room.adapters["beta"].calls == []


async def test_seat_mentioning_itself_does_not_wake_itself_again(make_room):
    room = make_room(mode="manual")
    room.adapters["alpha"].script("I, @alpha, say so")

    await room.say("@alpha go")

    assert len(room.adapters["alpha"].calls) == 1


async def test_multiple_mentions_wake_seats_in_order_of_appearance(make_room):
    room = make_room(mode="manual")

    await room.say("@gamma then @alpha please")

    assert [message.author for message in room.messages("seat")] == ["gamma", "alpha"]


# Modes


async def test_manual_mode_without_mention_stays_quiet(make_room):
    room = make_room(mode="manual")

    await room.say("nobody is addressed")

    assert room.messages("seat") == []
    assert room.router.call_count == 0
    assert room.frames[-1] == {"type": "quiet"}


async def test_roundrobin_each_seat_speaks_once_in_persona_order_then_quiet(make_room):
    room = make_room(mode="roundrobin")

    await room.say("discuss")

    assert [message.author for message in room.messages("seat")] == ["alpha", "beta", "gamma"]
    assert room.router.call_count == 0
    assert room.frames[-1] == {"type": "quiet"}
    assert all(len(adapter.calls) == 1 for adapter in room.adapters.values())


async def test_roundrobin_mentioned_seat_speaks_first_and_not_twice(make_room):
    room = make_room(mode="roundrobin")

    await room.say("@gamma start")

    assert [message.author for message in room.messages("seat")] == ["gamma", "alpha", "beta"]


async def test_roundrobin_starts_again_after_the_next_owner_message(make_room):
    room = make_room(seat_names=("alpha", "beta"), mode="roundrobin")

    await room.say("one")
    await room.say("two")

    assert [message.author for message in room.messages("seat")] == [
        "alpha",
        "beta",
        "alpha",
        "beta",
    ]


# Auto-turn cap


async def test_max_auto_turns_stops_the_chatter_and_owner_message_resets_it(make_room):
    room = make_room(seat_names=("alpha", "beta"), max_auto_turns=2)
    room.router.script(
        {"alpha": 9.0, "beta": 0.0},
        {"alpha": 0.0, "beta": 9.0},
        {"alpha": 9.0, "beta": 0.0},
        {"alpha": 0.0, "beta": 9.0},
    )

    await room.say("start")

    assert [message.author for message in room.messages("seat")] == ["alpha", "beta"]
    assert room.router.call_count == 2
    assert room.frames[-1] == {"type": "quiet"}

    await room.say("keep going")

    assert len(room.messages("seat")) == 4
    assert room.router.call_count == 4


async def test_cap_counts_mention_chains_too(make_room):
    room = make_room(mode="manual", max_auto_turns=1)
    room.adapters["alpha"].script("handing over to @beta")

    await room.say("@alpha go")

    assert [message.author for message in room.messages("seat")] == ["alpha"]
    assert room.adapters["beta"].calls == []
    assert room.frames[-1] == {"type": "quiet"}


# Seat failures


@pytest.mark.parametrize(
    "error",
    [
        SeatError("alpha", "exit code 1: boom"),
        SeatTimeoutError("alpha", "no reply within 180 s, process killed"),
    ],
)
async def test_seat_error_becomes_a_system_message(make_room, error):
    room = make_room()
    room.adapters["alpha"].script(error)
    room.router.script({"alpha": 9.0, "beta": 0.0, "gamma": 0.0})

    await room.say("hello")

    system_messages = room.messages("system")
    assert [message.text for message in system_messages] == [str(error)]
    assert room.messages("seat") == []
    # The typing indicator is cleared even though the run failed.
    assert room.frames_of_type("typing")[-1] == {"type": "typing", "seat": "alpha", "state": "stop"}
    assert room.scheduler.speaking_seat is None


async def test_unavailable_adapter_posts_system_message_and_is_not_run(make_room):
    room = make_room(mode="manual")
    room.adapters["alpha"].is_available = False

    await room.say("@alpha hello")

    assert [message.text for message in room.messages("system")] == [
        "Seat alpha: program claude not found."
    ]
    assert room.adapters["alpha"].calls == []


async def test_seat_without_adapter_posts_system_message(make_room):
    room = make_room(mode="manual")
    del room.scheduler.adapters["alpha"]

    await room.say("@alpha hello")

    assert "no persona or adapter" in room.messages("system")[0].text


# Sessions and prompts


async def test_session_id_is_stored_per_thread_and_seat_and_sent_on_next_turn(make_room):
    room = make_room(mode="manual")
    room.adapters["alpha"].script(
        SeatReply(text="one", session_id="session-1"),
        SeatReply(text="two", session_id="session-2"),
    )

    await room.say("@alpha first")
    assert room.store.get_session(room.thread_id, "alpha") == "session-1"
    assert room.store.get_session(room.thread_id, "beta") is None

    await room.say("@alpha second")

    session_ids_sent = [call["session_id"] for call in room.adapters["alpha"].calls]
    assert session_ids_sent == [None, "session-1"]
    assert room.store.get_session(room.thread_id, "alpha") == "session-2"


async def test_second_turn_prompt_has_only_messages_the_seat_has_not_seen(make_room):
    room = make_room(mode="manual")
    room.adapters["alpha"].script("alpha one")
    room.adapters["beta"].script("beta one")

    await room.say("@alpha first")
    await room.say("@beta second")
    await room.say("@alpha third")

    first_prompt = room.adapters["alpha"].calls[0]["prompt"]
    third_prompt = room.adapters["alpha"].calls[1]["prompt"]
    assert "owner: @alpha first" in first_prompt
    assert "owner: @beta second" in third_prompt
    assert "beta: beta one" in third_prompt
    assert "owner: @alpha third" in third_prompt
    assert "owner: @alpha first" not in third_prompt
    assert "alpha: alpha one" not in third_prompt


async def test_prompts_carry_persona_system_prompt_and_timeout(make_room):
    room = make_room(mode="manual")

    await room.say("@alpha hello")

    call = room.adapters["alpha"].calls[0]
    assert "You are alpha." in call["system_prompt"]
    assert "@beta" in call["system_prompt"]
    assert call["timeout_s"] == room.scheduler.settings.seat_timeout_s


# Interrupt and stop


async def test_owner_message_interrupts_a_running_seat(make_room):
    room = make_room(mode="manual")
    alpha = room.adapters["alpha"]
    alpha.block_until = asyncio.Event()

    await room.scheduler.say("@alpha think hard")
    await asyncio.wait_for(alpha.started.wait(), timeout=2)
    assert room.scheduler.speaking_seat == "alpha"

    await room.scheduler.say("never mind")
    await room.scheduler.driver_task

    assert alpha.was_cancelled
    assert room.scheduler.speaking_seat is None
    assert room.messages("seat") == []
    assert [message.text for message in room.messages("owner")] == [
        "@alpha think hard",
        "never mind",
    ]
    typing_states = [frame["state"] for frame in room.frames_of_type("typing")]
    assert typing_states == ["start", "stop"]
    stop_index = room.frames.index({"type": "typing", "seat": "alpha", "state": "stop"})
    interrupting_index = next(
        index
        for index, frame in enumerate(room.frames)
        if frame["type"] == "message" and frame["message"]["text"] == "never mind"
    )
    assert stop_index < interrupting_index


async def test_stop_cancels_the_running_seat_and_is_safe_to_repeat(make_room):
    room = make_room(mode="manual")
    alpha = room.adapters["alpha"]
    alpha.block_until = asyncio.Event()
    await room.scheduler.say("@alpha go")
    await asyncio.wait_for(alpha.started.wait(), timeout=2)

    await room.scheduler.stop()
    await room.scheduler.stop()

    assert alpha.was_cancelled
    assert room.scheduler.driver_task.done()
    assert room.scheduler.speaking_seat is None
    assert room.messages("seat") == []


async def test_stop_without_a_driver_does_nothing(make_room):
    room = make_room()

    await room.scheduler.stop()

    assert room.frames == []


# Wake, errors, modes


async def test_wake_runs_the_seat_without_posting_an_owner_message(make_room):
    room = make_room(mode="manual")

    await room.scheduler.wake("beta")
    await room.scheduler.driver_task

    assert room.messages("owner") == []
    assert [message.author for message in room.messages("seat")] == ["beta"]
    assert "(no new messages)" in room.adapters["beta"].calls[0]["prompt"]


async def test_wake_unknown_seat_raises(make_room):
    room = make_room()

    with pytest.raises(UnknownSeatError):
        await room.scheduler.wake("nobody")


async def test_say_on_ended_thread_raises(make_room):
    room = make_room()
    room.store.end_thread(room.thread_id)

    with pytest.raises(ThreadEndedError):
        await room.scheduler.say("too late")
    with pytest.raises(ThreadEndedError):
        await room.scheduler.wake("alpha")
    with pytest.raises(ThreadEndedError):
        await room.scheduler.end()
    assert room.messages() == []


async def test_set_mode_broadcasts_a_mode_frame_and_saves_it(make_room):
    room = make_room()

    thread = await room.scheduler.set_mode("roundrobin")

    assert thread.mode == "roundrobin"
    assert room.store.get_thread(room.thread_id).mode == "roundrobin"
    assert room.frames == [{"type": "mode", "mode": "roundrobin"}]


async def test_unexpected_error_in_driver_is_reported_as_an_error_frame(make_room):
    room = make_room(mode="manual")
    room.adapters["alpha"].script(RuntimeError("adapter bug"))

    await room.say("@alpha go")

    assert room.frames_of_type("error") == [
        {"type": "error", "text": "turn-taking failed: adapter bug"}
    ]


# End of discussion


async def test_end_collects_memory_notes_from_seats_that_spoke(make_room):
    room = make_room(mode="manual")
    await room.say("@alpha hi")
    await room.say("@beta hi")
    room.adapters["alpha"].script("- Owner likes short answers\n- Check the cap first")
    room.adapters["beta"].script("NONE")

    notes = await room.scheduler.end()

    assert notes == {
        "alpha": ["Owner likes short answers", "Check the cap first"],
        "beta": [],
    }
    # gamma never spoke, so it is not asked.
    assert room.adapters["gamma"].calls == []
    assert END_OF_DISCUSSION_PROMPT in room.adapters["alpha"].calls[-1]["prompt"]
    assert END_OF_DISCUSSION_PROMPT in room.adapters["beta"].calls[-1]["prompt"]

    alpha_index = (room.personas["alpha"].memory_directory / "index.md").read_text()
    assert "Owner likes short answers" in alpha_index
    assert "Check the cap first" in alpha_index
    alpha_note_files = sorted(
        path.name for path in room.personas["alpha"].memory_directory.glob("0*.md")
    )
    assert len(alpha_note_files) == 2
    assert not room.personas["beta"].memory_directory.exists()

    ended_frame = room.frames_of_type("ended")[0]
    assert ended_frame == {"type": "ended", "notes": notes}
    assert room.store.get_thread(room.thread_id).ended_at is not None


async def test_end_resumes_the_seat_session(make_room):
    room = make_room(mode="manual")
    room.adapters["alpha"].script(SeatReply(text="hello", session_id="session-9"))
    await room.say("@alpha hi")

    await room.scheduler.end()

    assert room.adapters["alpha"].calls[-1]["session_id"] == "session-9"


async def test_end_with_no_seat_messages_ends_thread_with_empty_notes(make_room):
    room = make_room()

    notes = await room.scheduler.end()

    assert notes == {}
    assert room.frames_of_type("ended") == [{"type": "ended", "notes": {}}]
    assert room.store.get_thread(room.thread_id).ended_at is not None


async def test_end_seat_error_reports_system_message_and_still_ends(make_room):
    room = make_room(mode="manual")
    await room.say("@alpha hi")
    room.adapters["alpha"].script(SeatError("alpha", "exit code 2: crashed"))

    notes = await room.scheduler.end()

    assert notes == {"alpha": []}
    assert "Memory notes for alpha not written" in room.messages("system")[-1].text
    assert room.store.get_thread(room.thread_id).ended_at is not None


async def test_end_cancels_a_running_seat_first(make_room):
    room = make_room(mode="manual")
    alpha = room.adapters["alpha"]
    alpha.block_until = asyncio.Event()
    await room.scheduler.say("@alpha go")
    await asyncio.wait_for(alpha.started.wait(), timeout=2)

    notes = await room.scheduler.end()

    assert alpha.was_cancelled
    assert notes == {}


async def test_unavailable_seat_is_not_scored_or_chosen(make_room):
    room = make_room()
    room.scheduler.seats[2].available = False  # gamma's program is not installed
    room.router.script({"alpha": 3.0, "beta": 4.0, "gamma": 9.0})
    room.adapters["gamma"].script(SeatReply(text="gamma speaks"))

    await room.say("who wants it?")

    assert room.adapters["gamma"].calls == []
    assert room.seat_texts() == []
    scored_seats = {bid["seat"] for bid in room.frames_of_type("bids")[0]["bids"]}
    assert "gamma" not in scored_seats


async def test_roundrobin_skips_unavailable_seat(make_room):
    room = make_room(mode="roundrobin")
    room.scheduler.seats[1].available = False  # beta's program is not installed
    room.adapters["alpha"].script(SeatReply(text="alpha"))
    room.adapters["gamma"].script(SeatReply(text="gamma"))

    await room.say("everyone, once")

    assert room.seat_texts() == ["alpha", "gamma"]
    assert room.adapters["beta"].calls == []
