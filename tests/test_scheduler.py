"""Turn-taking (SPEC section 5) with fake seats and routers only."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from adda.models import Room, SeatReply
from adda.personas import PersonaError
from adda.prompts import END_OF_DISCUSSION_PROMPT
from adda.routers.base import RouterError
from adda.routers.heuristic import HeuristicRouter
from adda.scheduler import (
    BudgetReachedError,
    RoomEndedError,
    Scheduler,
    UnknownSeatError,
    liveliness_threshold,
)
from adda.seats.base import SeatError
from adda.store import Store
from tests.fakes import FakeAdapterFactory, FakeClock, FakeRouter, make_settings


@dataclass
class Harness:
    scheduler: Scheduler
    store: Store
    room_id: str
    clock: FakeClock
    router: FakeRouter
    factory: FakeAdapterFactory
    frames: list[dict] = field(default_factory=list)

    def adapter(self, name: str):
        self.current_room()  # builds adapters for seats added since the last look
        return self.factory.by_name[name]

    def current_room(self) -> Room:
        return self.scheduler.current_room()

    def seat_id(self, name: str) -> str:
        return next(seat.id for seat in self.current_room().seats if seat.name == name)

    def messages(self):
        return self.store.list_messages(self.room_id)

    def spoken(self) -> list[tuple[str, str]]:
        return [(m.author, m.text) for m in self.messages() if m.role == "seat"]

    def system_texts(self) -> list[str]:
        return [m.text for m in self.messages() if m.role == "system"]

    def frames_of(self, frame_type: str) -> list[dict]:
        return [frame for frame in self.frames if frame["type"] == frame_type]

    def quiet_reasons(self) -> list[str]:
        return [frame["reason"] for frame in self.frames_of("quiet")]

    async def settle(self) -> None:
        task = self.scheduler.driver_task
        if task is not None:
            await task


DEFAULT_SEATS = (
    {"name": "Ravi", "persona": "alpha"},
    {"name": "Tuli", "persona": "beta"},
    {"name": "Pi", "persona": None},
)


def build_harness(
    tmp_path,
    seats=DEFAULT_SEATS,
    mode="bidding",
    liveliness=6,
    budget_usd=0.0,
    **setting_overrides,
) -> Harness:
    clock = FakeClock()
    factory = FakeAdapterFactory(clock)
    router = FakeRouter()
    store = Store(":memory:")
    room = store.create_room(
        "design talk", str(tmp_path / "room"), mode, liveliness=liveliness, budget_usd=budget_usd
    )
    for spec in seats:
        store.add_seat(
            room.id,
            name=spec["name"],
            program=spec.get("program", "claude"),
            persona=spec.get("persona"),
            instruction=spec.get("instruction"),
            model=spec.get("model"),
            enabled=spec.get("enabled", True),
        )
    harness = Harness(None, store, room.id, clock, router, factory)

    async def record(frame: dict) -> None:
        harness.frames.append(frame)

    harness.scheduler = Scheduler(
        room.id,
        store,
        make_settings(tmp_path, **setting_overrides),
        router,
        HeuristicRouter(),
        record,
        adapter_builder=factory,
        clock=clock,
    )
    return harness


@pytest.fixture
def harness(tmp_path) -> Harness:
    return build_harness(tmp_path)


# Threshold --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("liveliness", "turns", "floor", "expected"),
    [
        (10, 0, 3.0, 3.0),
        (10, 3, 3.0, 3.3),
        (6, 0, 3.0, 5.6),
        (6, 2, 3.0, 6.2),
        (1, 0, 3.0, 8.85),
        (1, 10, 3.0, 14.35),
        (10, 0, 4.5, 4.5),  # the floor wins over a low computed value
        (0, 0, 3.0, 9.5),
    ],
)
def test_liveliness_threshold_table(liveliness, turns, floor, expected):
    assert liveliness_threshold(liveliness, turns, floor) == pytest.approx(expected)


async def test_bid_below_threshold_stays_quiet_and_above_speaks(tmp_path):
    quiet_room = build_harness(tmp_path / "a", liveliness=1)  # threshold 8.85
    quiet_room.router.script({"Ravi": 5.0})
    await quiet_room.scheduler.say("anyone?")
    await quiet_room.settle()
    assert quiet_room.spoken() == []
    assert quiet_room.quiet_reasons() == ["nobody"]
    assert quiet_room.frames_of("bids")[0]["threshold"] == pytest.approx(8.85)

    lively_room = build_harness(tmp_path / "b", liveliness=10)  # threshold 3.0
    lively_room.router.script({"Ravi": 5.0})
    await lively_room.scheduler.say("anyone?")
    await lively_room.settle()
    assert [author for author, _ in lively_room.spoken()] == ["Ravi"]
    bids_frame = lively_room.frames_of("bids")[0]
    assert bids_frame["chosen"] == lively_room.seat_id("Ravi")
    assert bids_frame["router"] == "jev"
    assert {bid["seat"] for bid in bids_frame["bids"]} == {
        lively_room.seat_id(name) for name in ("Ravi", "Tuli", "Pi")
    }


async def test_threshold_ramps_with_automatic_turns(tmp_path):
    # L=10: threshold is 3.0, 3.1, 3.2, 3.3 for k = 0..3. A constant bid of 3.25 speaks three times.
    room = build_harness(tmp_path, liveliness=10)
    room.router.script(*({"Ravi": 3.25, "Tuli": 3.25, "Pi": 3.25} for _ in range(6)))
    await room.scheduler.say("go")
    await room.settle()
    assert len(room.spoken()) == 3
    thresholds = [frame["threshold"] for frame in room.frames_of("bids")]
    assert thresholds == pytest.approx([3.0, 3.1, 3.2, 3.3])


async def test_bidding_never_picks_the_author_of_the_last_message(tmp_path):
    room = build_harness(tmp_path, liveliness=10)
    room.router.script({"Ravi": 9.0, "Tuli": 8.0}, {"Ravi": 9.0, "Tuli": 1.0})
    await room.scheduler.say("go")
    await room.settle()
    # Ravi speaks first; in the second round his own bid is ignored and Tuli's 1.0 is too low.
    assert [author for author, _ in room.spoken()] == ["Ravi"]


async def test_cooldown_blocks_a_seat_that_just_spoke(tmp_path):
    room = build_harness(tmp_path, liveliness=10, cooldown_s=20.0)
    room.router.script({"Ravi": 9.0}, {"Tuli": 9.0}, {"Ravi": 9.0})
    await room.scheduler.say("go")
    await room.settle()
    # Ravi, then Tuli, then Ravi again is refused: Ravi spoke 0 s ago on the fake clock.
    assert [author for author, _ in room.spoken()] == ["Ravi", "Tuli"]


# Mentions ---------------------------------------------------------------------------------


async def test_mention_bypasses_bidding_and_records_woken_by(harness):
    await harness.scheduler.say("what do you think, @ravi?")
    await harness.settle()
    assert harness.router.call_count == 1  # only after the mention turn, for a follow-up
    first = next(m for m in harness.messages() if m.role == "seat")
    assert first.author == "Ravi"
    assert first.meta["woken_by"] == "mention"
    owner_message = harness.messages()[0]
    assert owner_message.mentions == [harness.seat_id("Ravi")]


@pytest.mark.parametrize("text", ["@RAVI hello", "hey @Ravi,", "(@ravi)"])
async def test_mentions_are_case_insensitive(harness, text):
    await harness.scheduler.say(text)
    await harness.settle()
    assert harness.spoken()[0][0] == "Ravi"


async def test_email_address_is_not_a_mention(harness):
    await harness.scheduler.say("write to me@ravi.example")
    await harness.settle()
    assert harness.spoken() == []


async def test_at_all_wakes_every_enabled_available_seat_in_position_order(tmp_path):
    seats = (
        {"name": "Ravi"},
        {"name": "Tuli", "enabled": False},
        {"name": "Pi"},
        {"name": "Ghost"},
    )
    room = build_harness(tmp_path, seats=seats, liveliness=0)
    room.factory.unavailable_names.add("Ghost")
    await room.scheduler.say("@all status please")
    await room.settle()
    spoken = room.messages()
    assert [m.author for m in spoken if m.role == "seat"] == ["Ravi", "Pi"]
    assert {m.meta["woken_by"] for m in spoken if m.role == "seat"} == {"all"}
    assert spoken[0].mentions == [room.seat_id("Ravi"), room.seat_id("Pi")]


async def test_prefix_mention_wakes_matching_seats(tmp_path):
    seats = ({"name": "Scout1"}, {"name": "Scout2"}, {"name": "Ravi"})
    room = build_harness(tmp_path, seats=seats, liveliness=0)
    await room.scheduler.say("@scout* look around")
    await room.settle()
    assert [author for author, _ in room.spoken()] == ["Scout1", "Scout2"]


async def test_seat_is_never_woken_by_its_own_mention(tmp_path):
    room = build_harness(tmp_path, liveliness=0)
    room.adapter("Ravi")  # build adapters first so the script lands on the right objects
    room.adapter("Ravi").script("@ravi @tuli your turn")
    await room.scheduler.say("@ravi start")
    await room.settle()
    assert [author for author, _ in room.spoken()] == ["Ravi", "Tuli"]
    ravi_message = next(m for m in room.messages() if m.author == "Ravi")
    assert ravi_message.mentions == [room.seat_id("Tuli")]


async def test_at_all_in_a_seat_reply_does_not_wake_the_author(tmp_path):
    room = build_harness(tmp_path, liveliness=0)
    room.adapter("Ravi").script("@all weigh in")
    await room.scheduler.say("@ravi kick off")
    await room.settle()
    assert [author for author, _ in room.spoken()] == ["Ravi", "Tuli", "Pi"]


async def test_mention_of_a_missing_program_posts_a_system_message(tmp_path):
    room = build_harness(tmp_path, liveliness=0)
    room.factory.unavailable_names.add("Ravi")
    await room.scheduler.say("@ravi hello")
    await room.settle()
    assert room.system_texts() == ["Seat Ravi: program claude not found."]


# Liveliness, safety, budget ---------------------------------------------------------------


async def test_paused_room_ignores_bidding_but_answers_mentions(tmp_path):
    room = build_harness(tmp_path, liveliness=0)
    room.router.script({"Ravi": 10.0})
    await room.scheduler.say("anyone?")
    await room.settle()
    assert room.router.call_count == 0
    assert room.spoken() == []
    assert room.quiet_reasons() == ["paused"]

    await room.scheduler.say("@tuli you then")
    await room.settle()
    assert [author for author, _ in room.spoken()] == ["Tuli"]
    assert room.quiet_reasons() == ["paused", "paused"]


async def test_safety_stop_after_max_auto_turns(tmp_path):
    room = build_harness(tmp_path, seats=({"name": "Ravi"}, {"name": "Tuli"}), max_auto_turns=3)
    room.adapter("Ravi").default_text = "@tuli ping"
    room.adapter("Tuli").default_text = "@ravi pong"
    await room.scheduler.say("@ravi start")
    await room.settle()
    assert len(room.spoken()) == 3
    assert room.quiet_reasons() == ["safety"]


async def test_budget_stop_posts_one_system_message_and_goes_quiet(tmp_path):
    room = build_harness(tmp_path, budget_usd=0.01)
    room.adapter("Ravi").script(SeatReply(text="costly", cost_usd=0.02))
    await room.scheduler.say("@all status")
    await room.settle()
    assert [author for author, _ in room.spoken()] == ["Ravi"]
    assert room.system_texts() == ["Budget of $0.01 reached; raise it to continue."]
    assert room.quiet_reasons() == ["budget"]
    assert room.store.get_room(room.room_id).spent_usd == pytest.approx(0.02)
    spend_frames = [f for f in room.frames_of("room") if f["room"]["spent_usd"] > 0]
    assert spend_frames, "a room frame must announce the new spend"

    # The notice is not repeated for the same budget, and mentions are refused with an error.
    await room.scheduler.say("@tuli are you there")
    await room.settle()
    assert len(room.system_texts()) == 1
    assert room.frames_of("error")[-1]["text"].startswith("Budget of $0.01")
    assert room.quiet_reasons() == ["budget", "budget"]
    with pytest.raises(BudgetReachedError):
        await room.scheduler.wake(room.seat_id("Tuli"))
    with pytest.raises(BudgetReachedError):
        await room.scheduler.wake_all()


async def test_raising_the_budget_resumes_the_room(tmp_path):
    room = build_harness(tmp_path, budget_usd=0.01)
    room.adapter("Ravi").script(SeatReply(text="costly", cost_usd=0.02))
    await room.scheduler.say("@ravi hi")
    await room.settle()
    await room.scheduler.apply_room_patch({"budget_usd": 1.0})
    await room.scheduler.say("@tuli go on")
    await room.settle()
    assert [author for author, _ in room.spoken()] == ["Ravi", "Tuli"]


async def test_pass_costs_money_but_posts_nothing(harness):
    harness.adapter("Ravi").script(SeatReply(text="PASS", cost_usd=0.5))
    await harness.scheduler.say("@ravi anything?")
    await harness.settle()
    assert harness.spoken() == []
    assert harness.store.get_room(harness.room_id).spent_usd == pytest.approx(0.5)


# Seats and patches ------------------------------------------------------------------------


async def test_rename_mid_room_rewrites_authors_and_keeps_the_session(harness):
    harness.adapter("Ravi").script(SeatReply(text="first", session_id="session-1"))
    await harness.scheduler.say("@ravi hello")
    await harness.settle()
    ravi_id = harness.seat_id("Ravi")

    room = await harness.scheduler.apply_seat_patch(ravi_id, {"name": "Rabindra"})
    assert [seat.name for seat in room.seats][0] == "Rabindra"
    assert [m.author for m in harness.messages() if m.role == "seat"] == ["Rabindra"]
    assert harness.store.get_session(harness.room_id, ravi_id) == "session-1"
    assert harness.frames_of("room")[-1]["room"]["seats"][0]["handle"] == "rabindra"

    await harness.scheduler.say("@rabindra again")
    await harness.settle()
    call = harness.adapter("Ravi").calls[-1]
    assert call["session_id"] == "session-1"
    assert "Rabindra" in call["system_prompt"]
    assert harness.factory.build_count("Ravi") == 1  # a rename does not rebuild the adapter


async def test_program_model_and_thinking_changes_rebuild_the_adapter(harness):
    ravi_id = harness.seat_id("Ravi")
    harness.adapter("Ravi")
    harness.adapter("Ravi").script(SeatReply(text="hello", session_id="session-1"))
    await harness.scheduler.say("@ravi hi")
    await harness.settle()
    assert harness.factory.build_count("Ravi") == 1

    await harness.scheduler.apply_seat_patch(ravi_id, {"model": "haiku"})
    assert harness.factory.build_count("Ravi") == 2
    await harness.scheduler.apply_seat_patch(ravi_id, {"thinking": "high"})
    assert harness.factory.build_count("Ravi") == 3
    assert harness.adapter("Ravi").thinking == "high"
    await harness.scheduler.apply_seat_patch(ravi_id, {"instruction": "Be brief.", "color": "#fff"})
    assert harness.factory.build_count("Ravi") == 3
    assert harness.store.get_session(harness.room_id, ravi_id) == "session-1"

    await harness.scheduler.apply_seat_patch(ravi_id, {"program": "pi"})
    assert harness.factory.build_count("Ravi") == 4
    assert harness.adapter("Ravi").program == "pi"
    assert harness.store.get_session(harness.room_id, ravi_id) is None  # new program, new session


async def test_empty_strings_in_a_seat_patch_clear_optional_fields(harness):
    ravi_id = harness.seat_id("Ravi")
    room = await harness.scheduler.apply_seat_patch(ravi_id, {"model": "haiku", "persona": ""})
    ravi = room.seats[0]
    assert (ravi.model, ravi.persona) == ("haiku", None)
    room = await harness.scheduler.apply_seat_patch(ravi_id, {"model": ""})
    assert room.seats[0].model is None


@pytest.mark.parametrize(
    ("patch", "error_type"),
    [
        ({"name": "Tuli"}, ValueError),
        ({"name": "  "}, ValueError),
        ({"thinking": "extreme"}, ValueError),
        ({"enabled": "yes"}, ValueError),
        ({"persona": "no-such-persona"}, PersonaError),
        ({"flavour": "mint"}, ValueError),
    ],
)
async def test_invalid_seat_patches_fail_loudly(harness, patch, error_type):
    with pytest.raises(error_type):
        await harness.scheduler.apply_seat_patch(harness.seat_id("Ravi"), patch)


async def test_seat_patch_for_a_seat_of_another_room_is_refused(harness):
    other_room = harness.store.create_room("other")
    stranger = harness.store.add_seat(other_room.id, name="Zed", program="pi")
    with pytest.raises(UnknownSeatError):
        await harness.scheduler.apply_seat_patch(stranger.id, {"color": "#000"})


@pytest.mark.parametrize(
    "patch",
    [
        {"liveliness": 11},
        {"liveliness": 2.5},
        {"budget_usd": -1},
        {"mode": "chaos"},
        {"title": " "},
        {"x": 1},
    ],
)
async def test_invalid_room_patches_fail_loudly(harness, patch):
    with pytest.raises(ValueError):
        await harness.scheduler.apply_room_patch(patch)


async def test_room_patch_updates_store_and_broadcasts_a_room_frame(harness):
    room = await harness.scheduler.apply_room_patch(
        {"title": "New title", "mode": "roundrobin", "liveliness": 3, "budget_usd": 2}
    )
    assert (room.title, room.mode, room.liveliness, room.budget_usd) == (
        "New title",
        "roundrobin",
        3,
        2.0,
    )
    assert harness.frames_of("room")[-1]["room"]["title"] == "New title"


async def test_removing_the_speaking_seat_cancels_its_turn(harness):
    adapter = harness.adapter("Ravi")
    adapter.block_until = asyncio.Event()
    await harness.scheduler.say("@ravi think hard")
    await asyncio.wait_for(adapter.started.wait(), 2)
    ravi_id = harness.seat_id("Ravi")

    room = await harness.scheduler.remove_seat(ravi_id)

    assert adapter.was_cancelled
    assert [seat.name for seat in room.seats] == ["Tuli", "Pi"]
    assert harness.spoken() == []
    assert {"type": "typing", "seat": ravi_id, "state": "stop"} in harness.frames


async def test_owner_message_interrupts_the_speaking_seat(harness):
    adapter = harness.adapter("Ravi")
    adapter.block_until = asyncio.Event()
    await harness.scheduler.say("@ravi think hard")
    await asyncio.wait_for(adapter.started.wait(), 2)

    adapter.block_until = None
    await harness.scheduler.say("@tuli actually you")
    await harness.settle()

    assert adapter.was_cancelled
    assert [author for author, _ in harness.spoken()] == ["Tuli"]


# Modes, wake, errors ----------------------------------------------------------------------


async def test_roundrobin_runs_one_pass_in_position_order_then_stops(tmp_path):
    room = build_harness(tmp_path, mode="roundrobin")
    await room.scheduler.say("round the table")
    await room.settle()
    assert [author for author, _ in room.spoken()] == ["Ravi", "Tuli", "Pi"]
    assert {m.meta["woken_by"] for m in room.messages() if m.role == "seat"} == {"roundrobin"}
    assert room.router.call_count == 0
    assert room.quiet_reasons() == ["nobody"]


async def test_roundrobin_pass_is_disabled_when_paused(tmp_path):
    room = build_harness(tmp_path, mode="roundrobin", liveliness=0)
    await room.scheduler.say("round the table")
    await room.settle()
    assert room.spoken() == []
    assert room.quiet_reasons() == ["paused"]


async def test_manual_mode_only_answers_mentions(tmp_path):
    room = build_harness(tmp_path, mode="manual")
    await room.scheduler.say("anyone?")
    await room.settle()
    assert room.spoken() == []
    await room.scheduler.say("@pi you")
    await room.settle()
    assert [author for author, _ in room.spoken()] == ["Pi"]


async def test_wake_runs_one_seat_without_an_owner_message(harness):
    await harness.scheduler.wake(harness.seat_id("Tuli"))
    await harness.settle()
    assert [m.role for m in harness.messages()] == ["seat"]
    assert harness.messages()[0].meta["woken_by"] == "wake"


async def test_wake_all_runs_every_available_seat(tmp_path):
    room = build_harness(tmp_path, liveliness=0)
    room.factory.unavailable_names.add("Pi")
    await room.scheduler.wake_all()
    await room.settle()
    assert [author for author, _ in room.spoken()] == ["Ravi", "Tuli"]
    assert {m.meta["woken_by"] for m in room.messages()} == {"all"}


async def test_wake_of_unknown_or_disabled_seat_is_an_error(tmp_path):
    room = build_harness(tmp_path, seats=({"name": "Ravi", "enabled": False},))
    with pytest.raises(UnknownSeatError):
        await room.scheduler.wake("nope")
    with pytest.raises(ValueError, match="disabled"):
        await room.scheduler.wake(room.seat_id("Ravi"))


async def test_seat_error_becomes_a_system_message_with_the_current_name(harness):
    harness.adapter("Ravi").script(SeatError("old-name", "exit code 1: boom"))
    await harness.scheduler.say("@ravi hi")
    await harness.settle()
    assert harness.system_texts() == ["Seat Ravi failed: exit code 1: boom"]
    assert harness.spoken() == []


async def test_router_failure_falls_back_to_heuristic_once(tmp_path):
    room = build_harness(tmp_path, liveliness=10)
    room.router.script(RouterError("HTTP 500"), RouterError("HTTP 500"))
    await room.scheduler.say("hello all")
    await room.settle()
    assert len([text for text in room.system_texts() if text.startswith("Router jev failed")]) == 1
    assert room.frames_of("bids")[0]["router"] == "heuristic"


# Prompts reaching the adapter ---------------------------------------------------------------


async def test_adapter_receives_owner_line_persona_instruction_and_handles(tmp_path):
    seats = (
        {"name": "Ravi", "persona": "alpha", "instruction": "Answer in one line."},
        {"name": "Scout 2"},
    )
    room = build_harness(tmp_path, seats=seats, liveliness=0)
    await room.scheduler.say("@ravi hello")
    await room.settle()
    call = room.adapter("Ravi").calls[0]
    system_prompt = call["system_prompt"]
    assert "Prefers short answers." in system_prompt
    assert "Argue for simple designs." in system_prompt
    assert "Answer in one line." in system_prompt
    assert "[First fact]" in system_prompt  # memory index
    assert "@scout2 (Scout 2)" in system_prompt
    assert "Riad: @ravi hello" in call["prompt"]  # the owner is shown by name


async def test_later_prompts_carry_only_unseen_messages(harness):
    await harness.scheduler.say("@ravi first")
    await harness.settle()
    await harness.scheduler.say("@ravi second")
    await harness.settle()
    first_prompt, second_prompt = (call["prompt"] for call in harness.adapter("Ravi").calls[:2])
    assert "first" in first_prompt
    assert "second" in second_prompt
    assert "Riad: @ravi first" not in second_prompt


async def test_missing_persona_posts_a_system_message(tmp_path):
    room = build_harness(tmp_path, seats=({"name": "Ravi", "persona": "alpha"},), liveliness=0)
    (tmp_path / "personas" / "alpha" / "persona.md").unlink()
    await room.scheduler.say("@ravi hi")
    await room.settle()
    assert room.system_texts()[0].startswith("Seat Ravi: persona 'alpha' not found")


# End of room --------------------------------------------------------------------------------


async def test_end_writes_notes_only_for_seats_with_personas(tmp_path):
    room = build_harness(tmp_path, liveliness=0)
    room.adapter("Ravi").script("my view", "- Keep designs small")
    room.adapter("Pi").script("pi view", "- Pi would remember this")  # Pi has no persona
    await room.scheduler.say("@ravi @pi what do you think?")
    await room.settle()

    notes = await room.scheduler.end()

    ravi_id = room.seat_id("Ravi")
    assert notes == {ravi_id: ["Keep designs small"]}
    assert len(room.adapter("Pi").calls) == 1  # never asked for notes
    assert END_OF_DISCUSSION_PROMPT in room.adapter("Ravi").calls[-1]["prompt"]
    note_files = sorted(p.name for p in (tmp_path / "personas" / "alpha" / "memory").glob("00*"))
    assert note_files == ["001-first-fact.md", "002-keep-designs-small.md"]
    assert room.store.get_room(room.room_id).ended_at is not None
    assert room.frames_of("ended")[0]["notes"] == {ravi_id: ["Keep designs small"]}
    assert room.frames_of("room")[-1]["room"]["ended_at"] is not None


async def test_ended_room_takes_no_more_input(harness):
    await harness.scheduler.end()
    with pytest.raises(RoomEndedError):
        await harness.scheduler.say("hello?")
    with pytest.raises(RoomEndedError):
        await harness.scheduler.end()
    with pytest.raises(RoomEndedError):
        await harness.scheduler.wake(harness.seat_id("Ravi"))
