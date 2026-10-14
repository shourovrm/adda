"""System prompt, turn prompt, mention parsing and memory-bullet parsing."""

from __future__ import annotations

import pytest

from adda.models import Message, Room, Seat, SeatConfig
from adda.personas import load_persona
from adda.prompts import (
    END_OF_DISCUSSION_PROMPT,
    find_mentions,
    parse_memory_bullets,
    render_transcript,
    resolve_mentions,
    split_more_marker,
    system_prompt,
    turn_prompt,
)
from tests.fakes import make_settings


def make_seat(name: str, position: int = 0, enabled=True, available=True, **fields) -> Seat:
    config = SeatConfig(
        room_id="room1", name=name, program="claude", position=position, enabled=enabled, **fields
    )
    return Seat.from_config(config, available=available)


ROSTER = [
    make_seat("Ravi", 0),
    make_seat("Tuli", 1),
    make_seat("Scout1", 2),
    make_seat("Scout2", 3),
    make_seat("Off", 4, enabled=False),
    make_seat("Ghost", 5, available=False),
]


def ids(*names: str) -> list[str]:
    by_name = {seat.name: seat.id for seat in ROSTER}
    return [by_name[name] for name in names]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@ravi hi", ["Ravi"]),
        ("@RAVI hi", ["Ravi"]),
        ("hello @Tuli, and @ravi.", ["Tuli", "Ravi"]),
        ("@ravi @ravi again", ["Ravi"]),
        ("mail me@ravi.example", []),
        ("@nobody", []),
        ("@off is disabled", []),
        ("@ghost is not installed but still addressable", ["Ghost"]),
        ("@all", ["Ravi", "Tuli", "Scout1", "Scout2"]),  # disabled and uninstalled are skipped
        ("@All please", ["Ravi", "Tuli", "Scout1", "Scout2"]),
        ("@scout* go", ["Scout1", "Scout2"]),
        ("@sc* go", ["Scout1", "Scout2"]),
        ("@x* go", []),
        ("@tuli then @all", ["Tuli", "Ravi", "Scout1", "Scout2"]),
        ("@ ravi", []),
        ("@*", []),
    ],
)
def test_find_mentions(text, expected):
    assert find_mentions(text, ROSTER) == ids(*expected)


def test_author_is_never_mentioned_by_itself():
    ravi = ROSTER[0]
    assert find_mentions("@ravi @tuli @all", ROSTER, author_seat_id=ravi.id) == ids(
        "Tuli", "Scout1", "Scout2"
    )


def test_resolve_mentions_reports_why_each_seat_was_woken():
    reasons = resolve_mentions("@all and @ravi", ROSTER, author_seat_id=None)
    assert reasons[ROSTER[0].id] == "mention"  # named explicitly, even though @all covers it
    assert reasons[ROSTER[1].id] == "all"


def test_handles_ignore_whitespace_and_case():
    roster = [make_seat("Scout Two")]
    assert find_mentions("@scouttwo", roster) == [roster[0].id]


def test_transcript_shows_the_owner_by_name():
    messages = [
        Message(room_id="r", thread_id="t", author="owner", role="owner", text="hello"),
        Message(room_id="r", thread_id="t", author="Ravi", role="seat", text="hi"),
        Message(room_id="r", thread_id="t", author="system", role="system", text="note"),
    ]
    assert render_transcript(messages, "Riad") == "Riad: hello\nRavi: hi\nsystem: note"


def test_system_prompt_order_and_content(tmp_path):
    settings = make_settings(tmp_path)
    persona = load_persona(settings.personas_dir, "alpha")
    seat = make_seat("Ravi", persona="alpha", instruction="  Answer in one line.  ")
    others = [make_seat("Tuli", 1), make_seat("Scout 2", 2)]

    prompt = system_prompt(seat, persona, others, "Riad", "Prefers short answers.")

    positions = [
        prompt.index(fragment)
        for fragment in (
            "Prefers short answers.",  # owner line
            "Argue for simple designs.",  # persona body
            "Answer in one line.",  # custom instruction
            "[First fact]",  # memory index
            "You are the seat Ravi (@ravi)",  # room rules
        )
    ]
    assert positions == sorted(positions)
    assert "@tuli (Tuli), @scout2 (Scout 2)" in prompt
    assert "@all" in prompt
    assert "PASS" in prompt
    assert "at the same moment" in prompt  # others compose at the same time; replies can cross
    assert "[more]" in prompt
    assert "You have no tools." in prompt
    assert "Riad" in prompt


def test_system_prompt_without_persona_or_instruction_is_just_the_rules():
    seat = make_seat("Ravi")
    prompt = system_prompt(seat, None, [], "Riad", "")
    assert prompt.startswith("You are the seat Ravi (@ravi)")
    assert "Other seats: none." in prompt
    assert "remember" not in prompt


def test_instruction_without_persona_has_no_memory_section(tmp_path):
    seat = make_seat("Ravi", instruction="Play the sceptic.")
    prompt = system_prompt(seat, None, [], "Riad", "Line.")
    assert "Play the sceptic." in prompt
    assert "earlier discussions" not in prompt


def test_turn_prompt_names_room_handle_and_owner():
    room = Room(title="Queue design")
    seat = make_seat("Scout 2")
    messages = [
        Message(room_id=room.id, thread_id="t", author="owner", role="owner", text="Which queue?")
    ]
    prompt = turn_prompt(room, messages, seat, "Riad")
    assert "Adda room: Queue design" in prompt
    assert "Riad: Which queue?" in prompt
    assert "speak as @scout2" in prompt
    assert "exactly PASS" in prompt
    assert "(no new messages)" in turn_prompt(room, [], seat, "Riad")


def test_end_of_discussion_prompt_is_unchanged():
    assert END_OF_DISCUSSION_PROMPT == (
        "In at most 3 bullets, what should you remember from this discussion for future ones? "
        "Reply NONE if nothing."
    )


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("NONE", []),
        ("none.", []),
        ("", []),
        ("- one\n- two", ["one", "two"]),
        ("1. one\n2) two", ["one", "two"]),
        ("- one that\n  wraps\n- two", ["one that wraps", "two"]),
        ("just a sentence", ["just a sentence"]),
        ("- a\n- b\n- c\n- d", ["a", "b", "c"]),
    ],
)
def test_parse_memory_bullets(reply, expected):
    assert parse_memory_bullets(reply) == expected


@pytest.mark.parametrize(
    ("text", "expected_text", "expected_more"),
    [
        ("part one\n[more]", "part one", True),
        ("part one\n[MORE]  \n", "part one", True),
        ("[more]", "", True),
        ("first\n\nsecond\n  [More]", "first\n\nsecond", True),
        ("use [more] sparingly", "use [more] sparingly", False),
        ("[more]\nand then text", "[more]\nand then text", False),
        ("plain", "plain", False),
        ("", "", False),
    ],
)
def test_split_more_marker(text, expected_text, expected_more):
    assert split_more_marker(text) == (expected_text, expected_more)


def test_turn_prompt_explains_afterthoughts_and_continuations():
    room = Room(title="t")
    seat = make_seat("Ravi")
    assert "quiet for a while" in turn_prompt(room, [], seat, "Riad", "afterthought")
    assert "carry on" in turn_prompt(room, [], seat, "Riad", "more")
    plain = turn_prompt(room, [], seat, "Riad", "mention")
    assert "quiet for a while" not in plain and "carry on" not in plain


def test_turn_prompt_names_the_thread_when_it_differs_from_the_room():
    room = Room(title="Billing")
    seat = make_seat("Ravi")
    assert "Thread: Refund flow" in turn_prompt(room, [], seat, "Riad", thread_title="Refund flow")
    assert "Thread:" not in turn_prompt(room, [], seat, "Riad", thread_title="Billing")


@pytest.mark.parametrize(
    ("access", "sentence"),
    [
        ("talk", "You have no tools."),
        ("look", "You may read files in /rooms/project."),
        ("work", "You may read, edit and run commands in /rooms/project."),
    ],
)
def test_system_prompt_states_the_effective_access_in_one_sentence(access, sentence):
    seat = make_seat("Ravi").model_copy(update={"effective_access": access})
    prompt = system_prompt(seat, None, [], "Riad", "", "/rooms/project")
    assert sentence in prompt
    assert "no files, no shell" not in prompt
