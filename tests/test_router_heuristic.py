from adda.models import Seat
from adda.routers.heuristic import HeuristicRouter


def make_seat(name: str) -> Seat:
    return Seat(room_id="room1", name=name, program="claude", available=True, handle=name)


SEATS = [make_seat("alpha"), make_seat("beta"), make_seat("gamma")]
NAME_BY_ID = {seat.id: seat.name for seat in SEATS}


def scores_of(bids) -> dict[str, float]:
    """Bids are keyed by seat id; the tests read them by name."""
    return {NAME_BY_ID[bid.seat]: bid.score for bid in bids}


async def test_mentioned_seat_gets_ten_and_others_zero():
    transcript = "alpha: hi\nowner: what do you think @beta?"
    bids = await HeuristicRouter().bids(transcript, SEATS, {})
    assert scores_of(bids) == {"alpha": 0.0, "beta": 10.0, "gamma": 0.0}


async def test_never_spoken_seat_is_longest_silent():
    transcript = "owner: hello\nalpha: hi\nbeta: hey"
    bids = await HeuristicRouter().bids(transcript, SEATS, {})
    assert scores_of(bids) == {"alpha": 0.0, "beta": 0.0, "gamma": 6.0}


async def test_longest_silent_among_speakers_gets_six():
    transcript = "alpha: one\nbeta: two\ngamma: three\nbeta: four\nowner: ok"
    bids = await HeuristicRouter().bids(transcript, SEATS, {})
    assert scores_of(bids) == {"alpha": 6.0, "beta": 0.0, "gamma": 0.0}


async def test_at_all_in_the_last_line_scores_every_seat_ten():
    bids = await HeuristicRouter().bids("owner: @all thoughts?", SEATS, {})
    assert scores_of(bids) == {"alpha": 10.0, "beta": 10.0, "gamma": 10.0}


async def test_empty_seats_gives_no_bids():
    assert await HeuristicRouter().bids("owner: hi", [], {}) == []


async def test_empty_transcript_picks_first_seat():
    bids = await HeuristicRouter().bids("", SEATS, {})
    assert scores_of(bids) == {"alpha": 6.0, "beta": 0.0, "gamma": 0.0}
