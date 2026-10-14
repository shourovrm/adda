from pathlib import Path

from adda.models import Message, Thread
from adda.personas import Persona
from adda.prompts import (
    END_OF_DISCUSSION_PROMPT,
    find_mentions,
    parse_memory_bullets,
    render_transcript,
    system_prompt,
    turn_prompt,
)

FIXTURES = Path(__file__).parent / "fixtures" / "personas"


def make_persona() -> Persona:
    return Persona(
        name="alpha",
        program="claude",
        model="haiku",
        color="#000000",
        role="Architecture",
        stance="Keep it small.",
        body="You are Alpha.",
        directory=FIXTURES / "alpha",
    )


def make_message(author: str, text: str) -> Message:
    role = "owner" if author == "owner" else "seat"
    return Message(thread_id="t1", author=author, role=role, text=text)


def test_system_prompt_contains_body_other_seats_and_memory():
    prompt = system_prompt(make_persona(), ["alpha", "beta", "gamma"])
    assert "You are Alpha." in prompt
    assert "@beta" in prompt
    assert "@gamma" in prompt
    assert "Other seats: @beta, @gamma." in prompt
    assert "Your role: Architecture" in prompt
    assert "Your stance: Keep it small." in prompt
    assert "- [First fact](001-first-fact.md)" in prompt
    assert "PASS" in prompt


def test_system_prompt_with_no_other_seats():
    assert "Other seats: none." in system_prompt(make_persona(), ["alpha"])


def test_turn_prompt_lists_new_messages_and_names_the_seat():
    thread = Thread(title="Caching")
    prompt = turn_prompt(
        thread, [make_message("owner", "hello"), make_message("beta", "hi")], "alpha"
    )
    assert "Caching" in prompt
    assert "owner: hello\nbeta: hi" in prompt
    assert "@alpha" in prompt
    assert "PASS" in prompt


def test_turn_prompt_without_messages():
    assert "(no new messages)" in turn_prompt(Thread(title="t"), [], "alpha")


def test_end_of_discussion_prompt_mentions_none():
    assert "NONE" in END_OF_DISCUSSION_PROMPT


def test_parse_memory_bullets_none():
    assert parse_memory_bullets("NONE") == []
    assert parse_memory_bullets("none.") == []
    assert parse_memory_bullets("   ") == []


def test_parse_memory_bullets_dash_star_and_numbered():
    reply = "- first\n* second\n1. third"
    assert parse_memory_bullets(reply) == ["first", "second", "third"]
    assert parse_memory_bullets("1) one\n2) two") == ["one", "two"]


def test_parse_memory_bullets_truncates_to_three():
    reply = "\n".join(f"- note {number}" for number in range(5))
    assert parse_memory_bullets(reply) == ["note 0", "note 1", "note 2"]


def test_parse_memory_bullets_joins_continuation_lines():
    reply = "- first part\n  continues here\n- second"
    assert parse_memory_bullets(reply) == ["first part continues here", "second"]


def test_parse_memory_bullets_plain_text_becomes_one_note():
    assert parse_memory_bullets("Remember to be brief.") == ["Remember to be brief."]


def test_find_mentions_order_dedupe_and_unknown_names():
    text = "@beta and @alpha, again @beta, also @nobody and mail@alpha.com"
    assert find_mentions(text, ["alpha", "beta"]) == ["beta", "alpha"]


def test_find_mentions_none():
    assert find_mentions("no mentions", ["alpha"]) == []


def test_render_transcript():
    messages = [make_message("owner", "hi"), make_message("alpha", "hello")]
    assert render_transcript(messages) == "owner: hi\nalpha: hello"
    assert render_transcript([]) == ""
