"""Turn-taking (SPEC section 5) with fake seats and routers only."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from adda.models import Room, SeatReply
from adda.personas import PersonaError, list_memory_notes, load_persona
from adda.prompts import END_OF_DISCUSSION_PROMPT
from adda.routers.base import RouterError
from adda.routers.heuristic import HeuristicRouter
from adda.scheduler import (
    BudgetReachedError,
    RoomEndedError,
    Scheduler,
    UnknownSeatError,
    adjusted_bid_score,
    liveliness_threshold,
)
from adda.seats.base import SeatError
from adda.store import Store
from tests.fakes import FakeAdapterFactory, FakeClock, FakeRouter, FakeSleeper, make_settings


@dataclass
class Harness:
    scheduler: Scheduler
    store: Store
    room_id: str
    clock: FakeClock
    router: FakeRouter
    factory: FakeAdapterFactory
    sleeper: FakeSleeper
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
        """Wait until nobody speaks and no reaction is pending; the afterthought timer is left
        alone unless `settle_afterthoughts` is used."""
        await self.scheduler.wait_until_idle()

    async def settle_afterthoughts(self) -> None:
        await self.scheduler.wait_until_idle(include_afterthought=True)

    def frame_types(self) -> list[str]:
        return [frame["type"] for frame in self.frames]


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
    max_concurrent=1,
    sleeper=None,
    uniform=None,
    **setting_overrides,
) -> Harness:
    """A room of fake seats. Most tests want strict one-at-a-time turns and no afterthoughts,
    so those are the defaults here; tests of the new behaviour ask for more."""
    setting_overrides.setdefault("afterthought_limit", 0)
    clock = FakeClock()
    factory = FakeAdapterFactory(clock)
    router = FakeRouter()
    store = Store(":memory:")
    room = store.create_room(
        "design talk",
        str(tmp_path / "room"),
        mode,
        liveliness=liveliness,
        budget_usd=budget_usd,
        max_concurrent=max_concurrent,
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
            eagerness=spec.get("eagerness", 5),
        )
    sleeper = sleeper or FakeSleeper()
    harness = Harness(None, store, room.id, clock, router, factory, sleeper)

    async def record(frame: dict) -> None:
        harness.frames.append(frame)

    harness.scheduler = Scheduler(
        room.threads[0].id,
        store,
        make_settings(tmp_path, **setting_overrides),
        router,
        HeuristicRouter(),
        record,
        adapter_builder=factory,
        clock=clock,
        sleep=sleeper,
        uniform=uniform or (lambda low, high: 1.0),
    )
    return harness


@pytest.fixture
def harness(tmp_path) -> Harness:
    return build_harness(tmp_path)


# Threshold --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("liveliness", "turns", "floor", "expected"),
    [
        (10, 0, 2.5, 2.5),
        (10, 3, 2.5, 2.65),
        (8, 0, 2.5, 3.5),
        (8, 5, 2.5, 4.0),
        (6, 0, 2.5, 4.5),
        (6, 2, 2.5, 4.8),
        (1, 0, 2.5, 7.0),
        (1, 10, 2.5, 9.75),
        (10, 0, 4.5, 4.5),  # the floor wins over a low computed value
        (0, 0, 2.5, 7.5),
    ],
)
def test_liveliness_threshold_table(liveliness, turns, floor, expected):
    assert liveliness_threshold(liveliness, turns, floor) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("raw_score", "eagerness", "cooling_down", "expected"),
    [
        (5.0, 5, False, 5.0),
        (5.0, 10, False, 7.0),
        (5.0, 0, False, 3.0),
        (5.0, 6, False, 5.4),
        (9.5, 10, False, 10.0),  # clamped at the top
        (1.0, 0, False, 0.0),  # clamped at the bottom
        (5.0, 5, True, 3.5),
        (5.0, 10, True, 5.5),
    ],
)
def test_adjusted_bid_score_table(raw_score, eagerness, cooling_down, expected):
    assert adjusted_bid_score(raw_score, eagerness, cooling_down) == pytest.approx(expected)


async def test_bid_below_threshold_stays_quiet_and_above_speaks(tmp_path):
    quiet_room = build_harness(tmp_path / "a", liveliness=1)  # threshold 7.0
    quiet_room.router.script({"Ravi": 5.0})
    await quiet_room.scheduler.say("anyone?")
    await quiet_room.settle()
    assert quiet_room.spoken() == []
    assert quiet_room.quiet_reasons() == ["nobody"]
    assert quiet_room.frames_of("bids")[0]["threshold"] == pytest.approx(7.0)

    lively_room = build_harness(tmp_path / "b", liveliness=10)  # threshold 2.5
    lively_room.router.script({"Ravi": 5.0})
    await lively_room.scheduler.say("anyone?")
    await lively_room.settle()
    assert [author for author, _ in lively_room.spoken()] == ["Ravi"]
    bids_frame = lively_room.frames_of("bids")[0]
    assert bids_frame["chosen"] == [lively_room.seat_id("Ravi")]
    assert bids_frame["kind"] == "message"
    assert bids_frame["router"] == "jev"
    assert {bid["seat"] for bid in bids_frame["bids"]} == {
        lively_room.seat_id(name) for name in ("Ravi", "Tuli", "Pi")
    }


async def test_threshold_ramps_with_bid_turns(tmp_path):
    # L=10: threshold is 2.5, 2.55, 2.6, 2.65 for k = 0..3. A constant bid of 2.62 speaks three
    # times and is refused on the fourth round.
    room = build_harness(tmp_path, liveliness=10)
    room.router.script(*({"Ravi": 2.62, "Tuli": 2.62, "Pi": 2.62} for _ in range(6)))
    await room.scheduler.say("go")
    await room.settle()
    assert len(room.spoken()) == 3
    thresholds = [frame["threshold"] for frame in room.frames_of("bids")]
    assert thresholds == pytest.approx([2.5, 2.55, 2.6, 2.65])


async def test_mention_turns_do_not_raise_the_threshold(tmp_path):
    room = build_harness(tmp_path, liveliness=10)
    room.router.script({"Ravi": 0.0, "Tuli": 0.0, "Pi": 0.0})
    await room.scheduler.say("@ravi and @tuli, thoughts?")
    await room.settle()
    # Two owner mentions started two turns, yet no turn counts as automatic or bid-driven.
    assert [author for author, _ in room.spoken()] == ["Ravi", "Tuli"]
    assert room.scheduler.auto_turns_since_owner == 0
    assert room.scheduler.bid_turns_since_owner == 0
    assert room.frames_of("bids")[0]["threshold"] == pytest.approx(2.5)


async def test_wake_and_roundrobin_turns_do_not_raise_the_threshold(tmp_path):
    room = build_harness(tmp_path, mode="roundrobin")
    await room.scheduler.say("go round")
    await room.settle()
    assert len(room.spoken()) == 3
    assert room.scheduler.bid_turns_since_owner == 0
    await room.scheduler.wake(room.seat_id("Ravi"))
    await room.settle()
    assert room.scheduler.bid_turns_since_owner == 0


async def test_bid_turns_raise_k_and_an_owner_message_resets_it(tmp_path):
    room = build_harness(tmp_path, liveliness=10)
    room.router.script({"Ravi": 9.0}, {"Tuli": 0.0})
    await room.scheduler.say("go")
    await room.settle()
    assert room.scheduler.bid_turns_since_owner == 1
    await room.scheduler.say("again")
    assert room.scheduler.bid_turns_since_owner == 0
    await room.settle()


async def test_author_of_the_latest_message_bids_with_the_self_reply_penalty(tmp_path):
    room = build_harness(tmp_path, liveliness=10, self_reply_penalty=1.5)
    # Ravi speaks first. In round 2 his raw 9.0 becomes 7.5 and still beats Tuli's 5.0; the
    # penalty lowers a bid, it does not exclude the author.
    room.router.script({"Ravi": 9.0, "Tuli": 8.0}, {"Ravi": 9.0, "Tuli": 5.0}, {"Ravi": 1.0})
    await room.scheduler.say("go")
    await room.settle()
    assert [author for author, _ in room.spoken()][:2] == ["Ravi", "Ravi"]
    second_round = {bid["seat"]: bid for bid in room.frames_of("bids")[1]["bids"]}
    ravi_bid = second_round[room.seat_id("Ravi")]
    assert ravi_bid["score"] == pytest.approx(7.5)
    assert ravi_bid["raw_score"] == pytest.approx(9.0)
    # The first round answered an owner message, so nobody was penalised.
    first_round = {bid["seat"]: bid for bid in room.frames_of("bids")[0]["bids"]}
    assert first_round[room.seat_id("Ravi")]["score"] == pytest.approx(9.0)


async def test_self_reply_penalty_can_hand_the_floor_to_someone_else(tmp_path):
    room = build_harness(tmp_path, liveliness=10, self_reply_penalty=1.5)
    room.router.script({"Ravi": 9.0}, {"Ravi": 4.0, "Tuli": 3.0})
    await room.scheduler.say("go")
    await room.settle()
    # Ravi's 4.0 becomes 2.5 and Tuli's 3.0 wins at threshold 2.55.
    assert [author for author, _ in room.spoken()][:2] == ["Ravi", "Tuli"]


async def test_cooldown_is_a_penalty_not_an_exclusion(tmp_path):
    room = build_harness(tmp_path, liveliness=10, cooldown_s=20.0)
    # Round 3: Ravi spoke 0 s ago on the fake clock, so his raw 3.5 becomes 2.0, under the
    # threshold of 2.6. Round 4: raw 9.0 becomes 7.5 and still wins.
    room.router.script({"Ravi": 9.0}, {"Tuli": 9.0}, {"Ravi": 3.5}, {"Ravi": 9.0})
    await room.scheduler.say("go")
    await room.settle()
    assert [author for author, _ in room.spoken()] == ["Ravi", "Tuli"]
    cooling_bid = next(
        bid for bid in room.frames_of("bids")[2]["bids"] if bid["seat"] == room.seat_id("Ravi")
    )
    assert cooling_bid["score"] == pytest.approx(2.0)
    assert cooling_bid["raw_score"] == pytest.approx(3.5)


async def test_cooling_seat_still_wins_with_a_strong_bid(tmp_path):
    room = build_harness(tmp_path, liveliness=10, cooldown_s=20.0)
    room.router.script({"Ravi": 9.0}, {"Tuli": 9.0}, {"Ravi": 9.0})
    await room.scheduler.say("go")
    await room.settle()
    assert [author for author, _ in room.spoken()][:3] == ["Ravi", "Tuli", "Ravi"]


async def test_eagerness_shifts_scores_and_zero_never_bids(tmp_path):
    seats = (
        {"name": "Ravi", "eagerness": 0},
        {"name": "Tuli", "eagerness": 10},
        {"name": "Pi"},
    )
    room = build_harness(tmp_path, seats=seats, liveliness=10)
    room.router.script({"Ravi": 10.0, "Tuli": 4.0, "Pi": 4.0})
    await room.scheduler.say("go")
    await room.settle()
    bids = {bid["seat"]: bid for bid in room.frames_of("bids")[0]["bids"]}
    assert bids[room.seat_id("Tuli")]["score"] == pytest.approx(6.0)
    assert bids[room.seat_id("Tuli")]["raw_score"] == pytest.approx(4.0)
    assert bids[room.seat_id("Pi")]["score"] == pytest.approx(4.0)
    # Ravi's raw 10.0 is the best, but eagerness 0 keeps him out; Tuli is chosen.
    assert room.frames_of("bids")[0]["chosen"] == [room.seat_id("Tuli")]
    assert "Ravi" not in [author for author, _ in room.spoken()]


async def test_eagerness_zero_seat_still_answers_a_mention(tmp_path):
    room = build_harness(tmp_path, seats=({"name": "Ravi", "eagerness": 0},), liveliness=10)
    await room.scheduler.say("@ravi hello")
    await room.settle()
    assert [author for author, _ in room.spoken()] == ["Ravi"]


# Mentions ---------------------------------------------------------------------------------


async def test_mention_bypasses_bidding_and_records_woken_by(harness):
    await harness.scheduler.say("what do you think, @ravi?")
    await harness.settle()
    # The owner message and Ravi's reply each get one bidding round (Ravi is out of the first
    # round because he is already speaking).
    assert harness.router.call_count == 2
    assert harness.seat_id("Ravi") not in {
        bid["seat"] for bid in harness.frames_of("bids")[0]["bids"]
    }
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


async def test_safety_stop_counts_automatic_starts_only(tmp_path):
    room = build_harness(tmp_path, seats=({"name": "Ravi"}, {"name": "Tuli"}), max_auto_turns=3)
    room.adapter("Ravi").default_text = "@tuli ping"
    room.adapter("Tuli").default_text = "@ravi pong"
    await room.scheduler.say("@ravi start")
    await room.settle()
    # The owner's own mention is not automatic: 1 forced start plus 3 automatic ones.
    assert len(room.spoken()) == 4
    assert room.quiet_reasons() == ["safety"]


async def test_budget_stop_posts_one_system_message_and_goes_quiet(tmp_path):
    room = build_harness(tmp_path, budget_usd=0.01)
    room.adapter("Ravi").script(SeatReply(text="costly", cost_usd=0.02))
    await room.scheduler.say("@ravi status")
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
    assert harness.frames_of("passed") == [{"type": "passed", "seat": harness.seat_id("Ravi")}]


# Seats and patches ------------------------------------------------------------------------


async def test_rename_mid_room_rewrites_authors_and_keeps_the_session(harness):
    harness.adapter("Ravi").script(SeatReply(text="first", session_id="session-1"))
    await harness.scheduler.say("@ravi hello")
    await harness.settle()
    ravi_id = harness.seat_id("Ravi")

    room = await harness.scheduler.apply_seat_patch(ravi_id, {"name": "Rabindra"})
    assert [seat.name for seat in room.seats][0] == "Rabindra"
    assert [m.author for m in harness.messages() if m.role == "seat"] == ["Rabindra"]
    assert harness.store.get_session(harness.scheduler.thread_id, ravi_id) == "session-1"
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
    assert harness.store.get_session(harness.scheduler.thread_id, ravi_id) == "session-1"

    await harness.scheduler.apply_seat_patch(ravi_id, {"program": "pi"})
    assert harness.factory.build_count("Ravi") == 4
    assert harness.adapter("Ravi").program == "pi"
    assert (
        harness.store.get_session(harness.scheduler.thread_id, ravi_id) is None
    )  # new program, new session


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


async def test_owner_message_does_not_cancel_a_speaking_seat(harness):
    adapter = harness.adapter("Ravi")
    adapter.block_until = asyncio.Event()
    await harness.scheduler.say("@ravi think hard")
    await asyncio.wait_for(adapter.started.wait(), 2)

    await harness.scheduler.say("@tuli meanwhile you")
    await asyncio.sleep(0)
    assert harness.scheduler.speaking.keys() <= {harness.seat_id("Ravi"), harness.seat_id("Tuli")}
    adapter.block_until.set()
    await harness.settle()

    assert not adapter.was_cancelled
    assert sorted(author for author, _ in harness.spoken()) == ["Ravi", "Tuli"]


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
    # Ending a thread leaves the room open for other threads.
    assert room.store.get_thread(room.scheduler.thread_id).ended_at is not None
    assert room.store.get_room(room.room_id).ended_at is None
    assert room.frames_of("ended")[0]["notes"] == {ravi_id: ["Keep designs small"]}
    assert room.frames_of("thread")[-1]["thread"]["ended_at"] is not None
    assert room.frames_of("room")[-1]["room"]["threads"][0]["ended_at"] is not None
    # The note records where it came from.
    note = list_memory_notes(load_persona(tmp_path / "personas", "alpha"))[0]
    assert (note.thread_id, note.thread_title) == (room.scheduler.thread_id, "design talk")
    assert (note.room_id, note.room_title) == (room.room_id, "design talk")


async def test_ended_room_takes_no_more_input(harness):
    await harness.scheduler.end()
    with pytest.raises(RoomEndedError):
        await harness.scheduler.say("hello?")
    with pytest.raises(RoomEndedError):
        await harness.scheduler.end()
    with pytest.raises(RoomEndedError):
        await harness.scheduler.wake(harness.seat_id("Ravi"))


async def test_cooldown_is_off_by_default(tmp_path):
    room = build_harness(tmp_path, liveliness=10)  # cooldown_s = 0 in make_settings
    room.router.script({"Ravi": 9.0}, {"Tuli": 9.0}, {"Ravi": 3.5})
    await room.scheduler.say("go")
    await room.settle()
    third_bids = room.frames_of("bids")[2]["bids"]
    ravi_bid = next(bid for bid in third_bids if bid["seat"] == room.seat_id("Ravi"))
    assert ravi_bid["score"] == pytest.approx(3.5)


async def test_quiet_decision_is_logged_with_reason_threshold_and_top_bid(tmp_path, caplog):
    room = build_harness(tmp_path, liveliness=1)
    room.router.script({"Ravi": 5.0})
    with caplog.at_level("INFO", logger="adda.scheduler"):
        await room.scheduler.say("anyone?")
        await room.settle()
    quiet_lines = [
        record.getMessage() for record in caplog.records if "quiet" in record.getMessage()
    ]
    assert len(quiet_lines) == 1
    assert "reason=nobody" in quiet_lines[0]
    assert "threshold=7.00" in quiet_lines[0]
    assert "top_adjusted_score=5.00" in quiet_lines[0]


async def test_pass_is_logged_at_info(harness, caplog):
    harness.adapter("Ravi").script(SeatReply(text="PASS"))
    with caplog.at_level("INFO", logger="adda.scheduler"):
        await harness.scheduler.say("@ravi anything?")
        await harness.settle()
    assert any("seat Ravi passed" in record.getMessage() for record in caplog.records)


# Concurrency (SPEC v0.4 E1) -----------------------------------------------------------------


def block(room: Harness, *names: str) -> dict[str, asyncio.Event]:
    """Make the named seats wait inside their reply until the test sets their event."""
    events = {}
    for name in names:
        adapter = room.adapter(name)
        adapter.block_until = asyncio.Event()
        events[name] = adapter.block_until
    return events


async def wait_started(room: Harness, *names: str) -> None:
    await asyncio.wait_for(
        asyncio.gather(*(room.adapter(name).started.wait() for name in names)), 2
    )


def speaking_names(room: Harness) -> set[str]:
    names_by_id = {seat.id: seat.name for seat in room.current_room().seats}
    return {names_by_id[seat_id] for seat_id in room.scheduler.speaking}


@pytest.mark.parametrize(
    ("max_concurrent", "expected_speakers"),
    [
        (1, {"Ravi"}),
        (2, {"Ravi", "Tuli"}),
        (3, {"Ravi", "Tuli", "Pi"}),
        (4, {"Ravi", "Tuli", "Pi"}),
    ],
)
async def test_bids_start_at_most_max_concurrent_seats_at_once(
    tmp_path, max_concurrent, expected_speakers
):
    room = build_harness(tmp_path, liveliness=10, max_concurrent=max_concurrent)
    room.router.script({"Ravi": 9.0, "Tuli": 8.0, "Pi": 7.0})
    events = block(room, "Ravi", "Tuli", "Pi")
    await room.scheduler.say("go")
    await wait_started(room, *expected_speakers)
    await asyncio.sleep(0)

    assert speaking_names(room) == expected_speakers
    chosen = room.frames_of("bids")[0]["chosen"]
    ranked_ids = [room.seat_id(name) for name in ("Ravi", "Tuli", "Pi")]
    assert chosen == ranked_ids[:max_concurrent]
    starts = [f["seat"] for f in room.frames_of("typing") if f["state"] == "start"]
    assert sorted(starts) == sorted(room.seat_id(name) for name in expected_speakers)
    assert room.scheduler.bid_turns_since_owner == len(expected_speakers)

    for event in events.values():
        event.set()
    await room.settle()


async def test_places_taken_by_mentioned_seats_count_against_the_cap(tmp_path):
    room = build_harness(tmp_path, liveliness=10, max_concurrent=2)
    room.router.script({"Tuli": 9.0, "Pi": 8.0})
    events = block(room, "Ravi", "Tuli", "Pi")
    await room.scheduler.say("@ravi what do you think?")
    await wait_started(room, "Ravi", "Tuli")
    await asyncio.sleep(0)
    # Ravi was named, so he is not a bidder; one free place goes to the best bid.
    assert speaking_names(room) == {"Ravi", "Tuli"}
    assert room.frames_of("bids")[0]["chosen"] == [room.seat_id("Tuli")]
    for event in events.values():
        event.set()
    await room.settle()


async def test_crossing_replies_each_see_the_other_only_in_the_next_prompt(tmp_path):
    room = build_harness(tmp_path, liveliness=0, max_concurrent=2)
    events = block(room, "Ravi", "Tuli")
    await room.scheduler.say("@ravi @tuli what do you think?")
    await wait_started(room, "Ravi", "Tuli")
    events["Tuli"].set()
    await asyncio.sleep(0.01)  # let Tuli finish while Ravi is still composing
    events["Ravi"].set()
    await room.settle()

    assert [author for author, _ in room.spoken()] == ["Tuli", "Ravi"]
    first_prompt = room.adapter("Ravi").calls[0]["prompt"]
    assert "Tuli:" not in first_prompt  # composed before Tuli's reply existed

    await room.scheduler.say("@ravi and now?")
    await room.settle()
    second_prompt = room.adapter("Ravi").calls[1]["prompt"]
    assert "Tuli: default reply from Tuli" in second_prompt
    assert "Ravi: default reply from Ravi" not in second_prompt  # his own reply is not repeated


async def test_a_seat_never_composes_two_replies_at_once(tmp_path):
    room = build_harness(tmp_path, liveliness=10, max_concurrent=4)
    room.router.script({"Ravi": 9.0, "Tuli": 0.0, "Pi": 0.0}, {"Ravi": 9.0})
    events = block(room, "Ravi")
    await room.scheduler.say("go")
    await wait_started(room, "Ravi")
    await room.scheduler.say("more input")  # second bid round while Ravi still composes
    await asyncio.sleep(0.01)
    assert len(room.adapter("Ravi").calls) == 1
    events["Ravi"].set()
    await room.settle()


async def test_owner_message_during_speech_neither_cancels_nor_resets_other_seats(tmp_path):
    room = build_harness(tmp_path, liveliness=0, max_concurrent=2)
    events = block(room, "Ravi", "Tuli")
    await room.scheduler.say("@ravi @tuli go")
    await wait_started(room, "Ravi", "Tuli")
    await room.scheduler.say("a note while you write")
    for event in events.values():
        event.set()
    await room.settle()
    assert not room.adapter("Ravi").was_cancelled
    assert not room.adapter("Tuli").was_cancelled
    assert sorted(author for author, _ in room.spoken()) == ["Ravi", "Tuli"]


# [more] continuation --------------------------------------------------------------------------


async def test_more_marker_is_stripped_and_the_seat_continues_at_once(tmp_path):
    room = build_harness(tmp_path, liveliness=10)
    room.adapter("Ravi").script("part one\n[more]", "part two")
    await room.scheduler.say("@ravi tell me")
    await room.settle()

    ravi_messages = [m for m in room.messages() if m.author == "Ravi"]
    assert [m.text for m in ravi_messages] == ["part one", "part two"]
    assert [m.meta["woken_by"] for m in ravi_messages] == ["mention", "more"]
    assert "[more]" not in " ".join(m.text for m in room.messages())
    assert "carry on" in room.adapter("Ravi").calls[1]["prompt"]
    # Rounds: the owner message, and the end of "part two"; none for the continued "part one".
    assert room.router.call_count == 2


async def test_more_marker_continues_at_most_more_limit_times_in_a_row(tmp_path):
    room = build_harness(tmp_path, liveliness=0, more_limit=2)
    room.adapter("Ravi").default_text = "and again\n[more]"
    await room.scheduler.say("@ravi go on")
    await room.settle()
    assert [m.text for m in room.messages() if m.author == "Ravi"] == ["and again"] * 3
    assert [m.meta["woken_by"] for m in room.messages() if m.author == "Ravi"] == [
        "mention",
        "more",
        "more",
    ]
    assert len(room.adapter("Ravi").calls) == 3

    # The streak starts over with the seat's next independent reply.
    await room.scheduler.say("@ravi once more")
    await room.settle()
    assert len(room.adapter("Ravi").calls) == 6


async def test_more_limit_zero_strips_the_marker_but_never_continues(tmp_path):
    room = build_harness(tmp_path, liveliness=0, more_limit=0)
    room.adapter("Ravi").script("one thing\n[more]")
    await room.scheduler.say("@ravi go")
    await room.settle()
    assert [text for _, text in room.spoken()] == ["one thing"]


async def test_more_marker_is_case_insensitive_and_only_counts_on_the_last_line(tmp_path):
    room = build_harness(tmp_path, liveliness=0)
    room.adapter("Ravi").script("first\n[MORE]  ", "second [more] in a sentence")
    await room.scheduler.say("@ravi go")
    await room.settle()
    assert [text for _, text in room.spoken()] == ["first", "second [more] in a sentence"]
    assert len(room.adapter("Ravi").calls) == 2


async def test_more_continuation_still_honours_mentions_in_the_reply(tmp_path):
    room = build_harness(tmp_path, liveliness=0)
    room.adapter("Ravi").script("@tuli look at this\n[more]", "done")
    await room.scheduler.say("@ravi go")
    await room.settle()
    assert sorted(author for author, _ in room.spoken()) == ["Ravi", "Ravi", "Tuli"]


async def test_more_continuation_counts_toward_the_safety_cap(tmp_path):
    room = build_harness(tmp_path, liveliness=0, more_limit=5, max_auto_turns=1)
    room.adapter("Ravi").default_text = "more\n[more]"
    await room.scheduler.say("@ravi go")
    await room.settle()
    assert len(room.spoken()) == 2  # the forced start plus one automatic continuation
    assert room.quiet_reasons() == ["safety"]


# Follow-up, wake, stop, end -------------------------------------------------------------------


async def test_mention_while_speaking_runs_the_seat_once_more_with_the_new_messages(tmp_path):
    room = build_harness(tmp_path, liveliness=0)
    events = block(room, "Ravi")
    await room.scheduler.say("@ravi first question")
    await wait_started(room, "Ravi")
    await room.scheduler.say("@ravi second question")
    await asyncio.sleep(0.01)
    assert room.scheduler.follow_up == {room.seat_id("Ravi"): "mention"}
    assert len(room.adapter("Ravi").calls) == 1

    events["Ravi"].set()
    await room.settle()

    assert len(room.adapter("Ravi").calls) == 2
    assert "second question" in room.adapter("Ravi").calls[1]["prompt"]
    assert "first question" not in room.adapter("Ravi").calls[1]["prompt"]
    assert room.scheduler.follow_up == {}
    ravi_messages = [m for m in room.messages() if m.author == "Ravi"]
    assert [m.meta["woken_by"] for m in ravi_messages] == ["mention", "mention"]


async def test_wake_while_speaking_marks_a_follow_up_and_wake_all_does_not_stop_anyone(tmp_path):
    room = build_harness(tmp_path, liveliness=0)
    events = block(room, "Ravi")
    await room.scheduler.say("@ravi hold on")
    await wait_started(room, "Ravi")
    await room.scheduler.wake_all()
    await asyncio.sleep(0.01)
    assert not room.adapter("Ravi").was_cancelled
    assert room.scheduler.follow_up == {room.seat_id("Ravi"): "all"}
    assert sorted(author for author, _ in room.spoken()) == ["Pi", "Tuli"]
    events["Ravi"].set()
    await room.settle()
    assert len(room.adapter("Ravi").calls) == 2


async def test_wake_starts_the_seat_directly_and_resets_the_counters(tmp_path):
    room = build_harness(tmp_path, liveliness=10)
    room.scheduler.bid_turns_since_owner = 5
    room.scheduler.afterthoughts_this_spell = 2
    await room.scheduler.wake(room.seat_id("Tuli"))
    assert room.scheduler.bid_turns_since_owner == 0
    assert room.scheduler.afterthoughts_this_spell == 0
    await room.settle()
    assert room.messages()[0].meta["woken_by"] == "wake"


async def test_stop_cancels_every_speaking_seat(tmp_path):
    room = build_harness(tmp_path, liveliness=0, max_concurrent=2)
    block(room, "Ravi", "Tuli")
    await room.scheduler.say("@ravi @tuli go")
    await wait_started(room, "Ravi", "Tuli")

    await room.scheduler.stop()

    assert room.adapter("Ravi").was_cancelled
    assert room.adapter("Tuli").was_cancelled
    assert room.scheduler.speaking == {}
    assert room.spoken() == []
    stops = [f["seat"] for f in room.frames_of("typing") if f["state"] == "stop"]
    assert sorted(stops) == sorted([room.seat_id("Ravi"), room.seat_id("Tuli")])
    frames_before = len(room.frames)
    await asyncio.sleep(0.01)
    assert len(room.frames) == frames_before  # nothing restarts after a stop


async def test_stop_cancels_the_pending_afterthought_timer(tmp_path):
    sleeper = FakeSleeper(hold=True)
    room = build_harness(tmp_path, afterthought_limit=3, sleeper=sleeper)
    await room.scheduler.say("hello")
    await room.settle()
    await asyncio.sleep(0)
    timer = room.scheduler.afterthought
    assert timer is not None and not timer.done()

    await room.scheduler.stop()

    assert timer.cancelled()
    sleeper.release()
    await asyncio.sleep(0.01)
    assert room.router.call_count == 1  # the tick never ran its round


async def test_stop_clears_follow_ups(tmp_path):
    room = build_harness(tmp_path, liveliness=0)
    block(room, "Ravi")
    await room.scheduler.say("@ravi one")
    await wait_started(room, "Ravi")
    await room.scheduler.say("@ravi two")
    await room.scheduler.stop()
    await asyncio.sleep(0.01)
    assert len(room.adapter("Ravi").calls) == 1
    assert room.scheduler.follow_up == {}


async def test_end_cancels_speakers_before_collecting_notes(tmp_path):
    room = build_harness(tmp_path, liveliness=0)
    block(room, "Ravi")
    await room.scheduler.say("@ravi go")
    await wait_started(room, "Ravi")
    await room.scheduler.end()
    assert room.adapter("Ravi").was_cancelled
    assert room.store.get_thread(room.scheduler.thread_id).ended_at is not None
    assert room.scheduler.speaking == {}


async def test_changing_a_seats_program_cancels_only_that_seat(tmp_path):
    room = build_harness(tmp_path, liveliness=0, max_concurrent=2)
    events = block(room, "Ravi", "Tuli")
    await room.scheduler.say("@ravi @tuli go")
    await wait_started(room, "Ravi", "Tuli")
    old_ravi = room.adapter("Ravi")

    await room.scheduler.apply_seat_patch(room.seat_id("Ravi"), {"program": "pi"})

    assert old_ravi.was_cancelled
    assert not room.adapter("Tuli").was_cancelled
    events["Tuli"].set()
    await room.settle()
    assert [author for author, _ in room.spoken()] == ["Tuli"]
    assert room.quiet_reasons() == ["paused"]


async def test_changing_other_seat_fields_does_not_cancel_a_speaker(tmp_path):
    room = build_harness(tmp_path, liveliness=0)
    events = block(room, "Ravi")
    await room.scheduler.say("@ravi go")
    await wait_started(room, "Ravi")
    await room.scheduler.apply_seat_patch(room.seat_id("Ravi"), {"color": "#123456"})
    events["Ravi"].set()
    await room.settle()
    assert [author for author, _ in room.spoken()] == ["Ravi"]


async def test_roundrobin_stays_sequential_even_with_many_places(tmp_path):
    room = build_harness(tmp_path, mode="roundrobin", max_concurrent=4)
    events = block(room, "Ravi", "Tuli", "Pi")
    await room.scheduler.say("round the table")
    await wait_started(room, "Ravi")
    await asyncio.sleep(0.01)
    assert speaking_names(room) == {"Ravi"}
    assert not room.adapter("Tuli").started.is_set()
    events["Ravi"].set()
    await wait_started(room, "Tuli")
    assert not room.adapter("Pi").started.is_set()
    events["Tuli"].set()
    events["Pi"].set()
    await room.settle()
    assert [author for author, _ in room.spoken()] == ["Ravi", "Tuli", "Pi"]
    assert room.quiet_reasons() == ["nobody"]


async def test_removing_a_seat_cancels_it_but_not_the_others(tmp_path):
    room = build_harness(tmp_path, liveliness=0, max_concurrent=2)
    events = block(room, "Ravi", "Tuli")
    await room.scheduler.say("@ravi @tuli go")
    await wait_started(room, "Ravi", "Tuli")
    await room.scheduler.remove_seat(room.seat_id("Ravi"))
    assert room.adapter("Tuli").was_cancelled is False
    events["Tuli"].set()
    await room.settle()
    assert [author for author, _ in room.spoken()] == ["Tuli"]


# Safety and budget with several speakers ------------------------------------------------------


async def test_a_bidding_round_never_starts_more_seats_than_the_safety_cap_allows(tmp_path):
    room = build_harness(tmp_path, liveliness=10, max_concurrent=3, max_auto_turns=2)
    room.router.script({"Ravi": 9.0, "Tuli": 8.0, "Pi": 7.0})
    await room.scheduler.say("go")
    await room.settle()
    assert room.frames_of("bids")[0]["chosen"] == [room.seat_id("Ravi"), room.seat_id("Tuli")]
    assert room.quiet_reasons() == ["safety"]
    assert len(room.spoken()) == 2


async def test_quiet_is_announced_once_after_the_last_of_several_speakers(tmp_path):
    room = build_harness(tmp_path, liveliness=0, max_concurrent=2)
    events = block(room, "Ravi", "Tuli")
    await room.scheduler.say("@ravi @tuli go")
    await wait_started(room, "Ravi", "Tuli")
    events["Ravi"].set()
    await asyncio.sleep(0.01)
    assert room.quiet_reasons() == []  # Tuli is still composing
    events["Tuli"].set()
    await room.settle()
    assert room.quiet_reasons() == ["paused"]


# max_concurrent patches -----------------------------------------------------------------------


async def test_room_patch_changes_max_concurrent_and_rejects_bad_values(harness):
    room = await harness.scheduler.apply_room_patch({"max_concurrent": 3})
    assert room.max_concurrent == 3
    assert harness.frames_of("room")[-1]["room"]["max_concurrent"] == 3
    for bad_value in (0, 5, -1, 2.5, True, "2"):
        with pytest.raises(ValueError, match="max_concurrent"):
            await harness.scheduler.apply_room_patch({"max_concurrent": bad_value})
    assert harness.store.get_room(harness.room_id).max_concurrent == 3


# Afterthoughts (SPEC v0.4 E2) -----------------------------------------------------------------


def quiet_frames(room: Harness) -> list[dict]:
    return room.frames_of("quiet")


async def test_a_quiet_room_schedules_an_afterthought_with_the_delay_formula(tmp_path):
    sleeper = FakeSleeper(hold=True)
    room = build_harness(tmp_path, liveliness=6, afterthought_limit=3, sleeper=sleeper)
    await room.scheduler.say("hello")
    await room.settle()
    await asyncio.sleep(0)

    # (11 - 6) * 20 s = 100 s; the jitter is 1.0 in the test and eagerness 5 changes nothing.
    assert quiet_frames(room) == [
        {"type": "quiet", "reason": "nobody", "next_tick_in_s": 100.0, "ticks_left": 3}
    ]
    assert sleeper.delays == [100.0]
    sleeper.release()
    await room.settle_afterthoughts()


@pytest.mark.parametrize(
    ("liveliness", "scale", "jitter", "eagerness", "expected"),
    [
        (10, 1.0, 1.0, 5, 20.0),
        (1, 1.0, 1.0, 5, 200.0),
        (6, 0.5, 1.0, 5, 50.0),
        (6, 2.0, 1.0, 5, 200.0),
        (6, 1.0, 0.7, 5, 70.0),
        (6, 1.0, 1.3, 5, 130.0),
        (6, 1.0, 1.0, 10, 75.0),  # 1 - 0.05 * 5
        (6, 1.0, 1.0, 1, 120.0),  # 1 - 0.05 * -4; eagerness 0 would mean nobody bids
    ],
)
async def test_afterthought_delay_table(tmp_path, liveliness, scale, jitter, eagerness, expected):
    asked_ranges = []

    def uniform(low, high):
        asked_ranges.append((low, high))
        return jitter

    room = build_harness(
        tmp_path,
        seats=({"name": "Ravi", "eagerness": eagerness},),
        liveliness=liveliness,
        afterthought_limit=2,
        afterthought_scale=scale,
        sleeper=FakeSleeper(hold=True),
        uniform=uniform,
    )
    await room.scheduler.say("hello")
    await room.settle()
    assert quiet_frames(room)[0]["next_tick_in_s"] == pytest.approx(expected)
    assert asked_ranges[0] == (0.7, 1.3)
    room.sleeper.release()
    await room.settle_afterthoughts()


async def test_eagerness_shortens_only_the_first_tick_of_a_spell(tmp_path):
    room = build_harness(
        tmp_path,
        seats=({"name": "Ravi", "eagerness": 10},),
        liveliness=6,
        afterthought_limit=2,
    )
    await room.scheduler.say("hello")
    await room.settle_afterthoughts()
    assert room.sleeper.delays == pytest.approx([75.0, 100.0])


async def test_the_afterthought_tick_asks_the_afterthought_question_and_starts_the_winner(tmp_path):
    room = build_harness(tmp_path, liveliness=10, afterthought_limit=3, max_concurrent=2)
    room.router.script({}, {"Ravi": 0.0, "Tuli": 7.0, "Pi": 6.0})
    await room.scheduler.say("hello")
    await room.settle_afterthoughts()

    assert room.router.question_kinds[:2] == ["message", "afterthought"]
    bids_frames = room.frames_of("bids")
    assert [frame["kind"] for frame in bids_frames[:2]] == ["message", "afterthought"]
    assert bids_frames[1]["chosen"] == [room.seat_id("Tuli"), room.seat_id("Pi")]
    afterthoughts = [m for m in room.messages() if m.role == "seat"]
    assert [m.author for m in afterthoughts] == ["Tuli", "Pi"]
    assert {m.meta["woken_by"] for m in afterthoughts} == {"afterthought"}
    assert afterthoughts[0].meta["bid_score"] == pytest.approx(7.0)


async def test_afterthought_ticks_stop_at_the_limit_with_a_countdown(tmp_path):
    room = build_harness(tmp_path, liveliness=6, afterthought_limit=2)
    await room.scheduler.say("hello")
    await room.settle_afterthoughts()

    assert room.router.question_kinds == ["message", "afterthought", "afterthought"]
    assert [(f["next_tick_in_s"], f["ticks_left"]) for f in quiet_frames(room)] == [
        (100.0, 2),
        (100.0, 1),
        (None, 0),
    ]
    assert room.sleeper.delays == [100.0, 100.0]
    assert room.scheduler.afterthoughts_this_spell == 2


async def test_an_owner_message_starts_a_new_spell_with_fresh_ticks(tmp_path):
    room = build_harness(tmp_path, liveliness=6, afterthought_limit=1)
    await room.scheduler.say("hello")
    await room.settle_afterthoughts()
    assert room.router.question_kinds == ["message", "afterthought"]

    await room.scheduler.say("anyone there?")
    await room.settle_afterthoughts()
    assert room.router.question_kinds == ["message", "afterthought"] * 2


async def test_an_owner_message_cancels_the_pending_timer(tmp_path):
    sleeper = FakeSleeper(hold=True)
    room = build_harness(tmp_path, liveliness=6, afterthought_limit=2, sleeper=sleeper)
    await room.scheduler.say("hello")
    await room.settle()
    await asyncio.sleep(0)
    first_timer = room.scheduler.afterthought
    await room.scheduler.say("wait, one more thing")
    await room.settle()
    await asyncio.sleep(0)
    assert first_timer.cancelled()
    assert room.scheduler.afterthought is not first_timer
    sleeper.release()
    await room.settle_afterthoughts()


async def test_a_seat_message_does_not_reset_the_spell(tmp_path):
    room = build_harness(tmp_path, liveliness=10, afterthought_limit=2, max_concurrent=1)
    room.router.script({}, {"Ravi": 9.0})  # the first tick makes Ravi speak
    await room.scheduler.say("hello")
    await room.settle_afterthoughts()
    # One tick spent on Ravi, one left: message round, tick 1, (Ravi's message round), tick 2.
    assert room.router.question_kinds == ["message", "afterthought", "message", "afterthought"]
    assert room.scheduler.afterthoughts_this_spell == 2


async def test_a_seat_that_passed_is_not_asked_again_until_someone_speaks(tmp_path):
    room = build_harness(tmp_path, liveliness=10, afterthought_limit=2)
    room.adapter("Ravi").script("PASS")
    room.router.script({"Ravi": 9.0}, {"Ravi": 9.0, "Tuli": 0.0})
    await room.scheduler.say("hello")
    await room.settle_afterthoughts()
    assert room.spoken() == []
    afterthought_frames = [f for f in room.frames_of("bids") if f["kind"] == "afterthought"]
    assert afterthought_frames
    assert all(f["chosen"] == [] for f in afterthought_frames)


@pytest.mark.parametrize(
    "room_options",
    [{"mode": "manual"}, {"mode": "roundrobin"}, {"liveliness": 0}],
)
async def test_no_afterthoughts_unless_the_room_is_bidding_and_lively(tmp_path, room_options):
    room = build_harness(tmp_path, afterthought_limit=3, **room_options)
    await room.scheduler.say("hello")
    await room.settle_afterthoughts()
    assert room.sleeper.delays == []
    assert room.scheduler.afterthought is None
    assert quiet_frames(room)[-1]["next_tick_in_s"] is None
    assert quiet_frames(room)[-1]["ticks_left"] == 0


async def test_afterthoughts_off_when_the_limit_is_zero_or_all_seats_never_bid(tmp_path):
    room = build_harness(tmp_path, afterthought_limit=0)
    await room.scheduler.say("hello")
    await room.settle_afterthoughts()
    assert room.sleeper.delays == []

    shy = build_harness(
        tmp_path / "shy", seats=({"name": "Ravi", "eagerness": 0},), afterthought_limit=3
    )
    await shy.scheduler.say("hello")
    await shy.settle_afterthoughts()
    assert shy.sleeper.delays == []


async def test_budget_and_safety_quiet_frames_carry_no_countdown(tmp_path):
    over_budget = build_harness(tmp_path / "a", budget_usd=0.01, afterthought_limit=3)
    over_budget.adapter("Ravi").script(SeatReply(text="costly", cost_usd=0.02))
    await over_budget.scheduler.say("@ravi hi")
    await over_budget.settle_afterthoughts()
    assert quiet_frames(over_budget) == [
        {"type": "quiet", "reason": "budget", "next_tick_in_s": None, "ticks_left": 0}
    ]
    assert over_budget.sleeper.delays == []

    capped = build_harness(tmp_path / "b", liveliness=0, max_auto_turns=0, afterthought_limit=3)
    await capped.scheduler.say("@ravi hi")
    await capped.settle_afterthoughts()
    assert quiet_frames(capped)[-1] == {
        "type": "quiet",
        "reason": "safety",
        "next_tick_in_s": None,
        "ticks_left": 0,
    }


async def test_afterthought_starts_count_as_bid_turns_and_automatic_turns(tmp_path):
    room = build_harness(tmp_path, liveliness=10, afterthought_limit=1)
    room.router.script({}, {"Ravi": 9.0})
    await room.scheduler.say("hello")
    await room.settle_afterthoughts()
    assert room.scheduler.bid_turns_since_owner == 1
    assert room.scheduler.auto_turns_since_owner == 1


# Frame shapes (SPEC v0.4 E4) ------------------------------------------------------------------


async def test_frame_shapes_follow_the_spec(tmp_path):
    room = build_harness(tmp_path, liveliness=10, max_concurrent=2, afterthought_limit=1)
    room.router.script({"Ravi": 9.0, "Tuli": 8.0})
    await room.scheduler.say("go")
    await room.settle_afterthoughts()

    bids = room.frames_of("bids")[0]
    assert set(bids) == {"type", "bids", "chosen", "threshold", "router", "kind"}
    assert isinstance(bids["chosen"], list)
    assert bids["kind"] == "message"
    assert set(bids["bids"][0]) == {"seat", "score", "confidence", "raw_score"}
    for quiet in quiet_frames(room):
        assert set(quiet) == {"type", "reason", "next_tick_in_s", "ticks_left"}
        assert quiet["reason"] in {"nobody", "paused", "safety", "budget"}
        assert isinstance(quiet["ticks_left"], int)
    for typing in room.frames_of("typing"):
        assert set(typing) == {"type", "seat", "state"}
    await room.scheduler.publish_room()
    assert room.frames_of("room")[-1]["room"]["max_concurrent"] == 2


# Threads (SPEC v0.5 F2) ---------------------------------------------------------------------


def second_thread(room: Harness, title: str = "second topic") -> tuple[Scheduler, list[dict]]:
    """Another thread of the same room with its own scheduler; its thread-only frames are
    recorded apart, while room-wide frames land in the room's own list as well."""
    thread = room.store.create_thread(room.room_id, title)
    thread_frames: list[dict] = []

    async def record_thread(frame: dict) -> None:
        thread_frames.append(frame)

    async def record_room(frame: dict) -> None:
        room.frames.append(frame)
        thread_frames.append(frame)

    scheduler = Scheduler(
        thread.id,
        room.store,
        room.scheduler.settings,
        room.router,
        HeuristicRouter(),
        record_thread,
        adapter_builder=room.factory,
        clock=room.clock,
        sleep=room.sleeper,
        uniform=lambda low, high: 1.0,
        broadcast_room=record_room,
    )
    return scheduler, thread_frames


async def test_threads_of_a_room_keep_their_own_messages_and_sessions(tmp_path):
    room = build_harness(tmp_path, liveliness=0)
    other, other_frames = second_thread(room)
    room.adapter("Ravi").script(SeatReply(text="in the first", session_id="session-a"))
    await room.scheduler.say("@ravi first thread")
    await room.settle()
    room.adapter("Ravi").script(SeatReply(text="in the second", session_id="session-b"))
    await other.say("@ravi second thread")
    await other.wait_until_idle()

    first_texts = [m.text for m in room.store.list_thread_messages(room.scheduler.thread_id)]
    second_texts = [m.text for m in room.store.list_thread_messages(other.thread_id)]
    assert first_texts == ["@ravi first thread", "in the first"]
    assert second_texts == ["@ravi second thread", "in the second"]
    ravi_id = room.seat_id("Ravi")
    assert room.store.get_session(room.scheduler.thread_id, ravi_id) == "session-a"
    assert room.store.get_session(other.thread_id, ravi_id) == "session-b"
    # The second thread's first prompt holds only its own messages, under a fresh session.
    second_call = room.adapter("Ravi").calls[-1]
    assert second_call["session_id"] is None
    assert "first thread" not in second_call["prompt"]
    assert "Thread: second topic" in second_call["prompt"]
    # Thread-only frames stay in their thread.
    assert all(
        frame["message"]["thread_id"] == other.thread_id
        for frame in other_frames
        if frame["type"] == "message"
    )


async def test_threads_share_the_room_budget_and_room_frames(tmp_path):
    room = build_harness(tmp_path, liveliness=0, budget_usd=0.05)
    other, _ = second_thread(room)
    room.adapter("Ravi").script(SeatReply(text="costly", cost_usd=0.06))
    await room.scheduler.say("@ravi spend it")
    await room.settle()

    assert room.store.get_room(room.room_id).spent_usd == pytest.approx(0.06)
    assert room.frames_of("room")[-1]["room"]["spent_usd"] == pytest.approx(0.06)
    # The other thread is over the same budget: wakes are refused there.
    with pytest.raises(BudgetReachedError):
        await other.wake(room.seat_id("Tuli"))


async def test_ending_one_thread_leaves_the_others_open(tmp_path):
    room = build_harness(tmp_path, liveliness=0)
    other, other_frames = second_thread(room)
    await other.end()
    with pytest.raises(RoomEndedError):
        await other.say("still there?")
    await room.scheduler.say("@pi this thread still works")
    await room.settle()
    assert room.spoken()[-1][0] == "Pi"
    thread_frames = [frame for frame in other_frames if frame["type"] == "thread"]
    assert thread_frames[-1]["thread"]["id"] == other.thread_id
    assert thread_frames[-1]["thread"]["ended_at"] is not None


async def test_rename_broadcasts_a_thread_frame(harness):
    summary = await harness.scheduler.rename("  New name ")
    assert summary.title == "New name"
    assert harness.frames_of("thread")[-1]["thread"]["title"] == "New name"
    with pytest.raises(ValueError):
        await harness.scheduler.rename(" ")


async def test_archived_room_refuses_input_in_every_thread(harness):
    harness.store.end_room(harness.room_id)
    with pytest.raises(RoomEndedError, match="archived"):
        await harness.scheduler.say("hello?")


def test_note_title_prefers_first_sentence_and_word_boundary():
    from adda.scheduler import note_title_from_bullet

    assert note_title_from_bullet("Keep sessions per thread. Notes carry the memory.") == (
        "Keep sessions per thread"
    )
    long_bullet = (
        'Any question that asks people to admit a failure needs an explicit "a yes fixes the '
        'rule, not the policy," or the answers will be biased toward no'
    )
    title = note_title_from_bullet(long_bullet)
    assert len(title) <= 80
    assert not title.endswith('"a')
    assert title.endswith("explicit")
