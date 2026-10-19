"""Claude seat: usage, rate-limit detection, reset-time parsing and the custom provider."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from adda.config import HarnessSettings
from adda.models import SeatConfig
from adda.seats import base
from adda.seats.base import SeatError, SeatRateLimitedError
from adda.seats.claude import ClaudeSeat, is_rate_limit_text, parse_reset_time
from adda.seats.registry import build_adapter

FIXTURES = Path(__file__).parent / "fixtures" / "seat_outputs"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# 2026-10-19 10:00 UTC is 16:00 in Dhaka (UTC+6, no daylight saving).
NOW = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)


def test_usage_adds_cache_parts_to_input():
    reply = ClaudeSeat("ada").parse_output(fixture_text("claude_success.json"), None)
    assert reply.input_tokens == 4 + 1530 + 22410
    assert reply.cached_input_tokens == 22410
    assert reply.output_tokens == 12


def test_usage_missing_fields_count_as_zero_and_missing_block_is_none():
    partial = '{"is_error": false, "result": "x", "usage": {"output_tokens": 5}}'
    reply = ClaudeSeat("ada").parse_output(partial, None)
    assert (reply.input_tokens, reply.cached_input_tokens, reply.output_tokens) == (0, 0, 5)
    bare = ClaudeSeat("ada").parse_output('{"is_error": false, "result": "x"}', None)
    assert (bare.input_tokens, bare.cached_input_tokens, bare.output_tokens) == (None,) * 3


@pytest.mark.parametrize(
    "text",
    [
        "You've hit your limit · resets 3pm (Asia/Dhaka)",
        "Claude usage limit reached. Your limit will reset at 2pm (UTC).",
        "API Error: rate limit exceeded",
        "5-hour LIMIT reached, it RESETS soon",
    ],
)
def test_limit_matcher_accepts(text):
    assert is_rate_limit_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "No conversation found with session ID: abc",
        "The limit of the sum is 3",
        "Session will reset when you restart",
    ],
)
def test_limit_matcher_rejects(text):
    assert not is_rate_limit_text(text)


def test_rate_limited_result_raises_with_reset_time():
    seat = ClaudeSeat("ada")
    with pytest.raises(SeatRateLimitedError) as caught:
        seat.parse_output(fixture_text("claude_rate_limited.json"), None)
    assert caught.value.detail == "You've hit your limit · resets 3pm (Asia/Dhaka)"
    assert caught.value.resets_at is not None
    assert caught.value.resets_at.tzinfo is not None


def test_other_error_stays_a_plain_seat_error():
    with pytest.raises(SeatError) as caught:
        ClaudeSeat("ada").parse_output(fixture_text("claude_error.json"), "x")
    assert not isinstance(caught.value, SeatRateLimitedError)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 15:00 Dhaka = 09:00 UTC, already past at 10:00 UTC, so tomorrow.
        ("resets 3pm (Asia/Dhaka)", datetime(2026, 10, 20, 9, 0, tzinfo=UTC)),
        # 19:00 Dhaka = 13:00 UTC, later today.
        ("resets 7pm (Asia/Dhaka)", datetime(2026, 10, 19, 13, 0, tzinfo=UTC)),
        ("Your limit will reset at 2pm (UTC).", datetime(2026, 10, 19, 14, 0, tzinfo=UTC)),
        ("Your limit will reset at 9am (UTC).", datetime(2026, 10, 20, 9, 0, tzinfo=UTC)),
        ("Your limit will reset at 3:30 pm (UTC).", datetime(2026, 10, 19, 15, 30, tzinfo=UTC)),
        ("resets 14:00 (UTC)", datetime(2026, 10, 19, 14, 0, tzinfo=UTC)),
        ("resets 12am (UTC)", datetime(2026, 10, 20, 0, 0, tzinfo=UTC)),
        ("resets 12pm (UTC)", datetime(2026, 10, 19, 12, 0, tzinfo=UTC)),
    ],
)
def test_reset_time_parsing(text, expected):
    result = parse_reset_time(text, NOW)
    assert result == expected
    assert result.utcoffset().total_seconds() == 0


def test_reset_time_without_zone_is_the_next_local_occurrence():
    result = parse_reset_time("resets 3pm", NOW)
    assert result.utcoffset().total_seconds() == 0
    assert NOW < result <= NOW + timedelta(days=1)
    assert result.astimezone().hour == 15


@pytest.mark.parametrize(
    "text",
    [
        "usage limit reached",
        "resets in 5 hours",
        "resets 3pm (Not/AZone)",
        "no time here",
        "resets 13pm (UTC)",
    ],
)
def test_reset_time_unparseable_is_none(text):
    assert parse_reset_time(text, NOW) is None


def test_nonzero_exit_with_limit_text_becomes_rate_limited(monkeypatch):
    async def fake_run_command(seat, command, timeout_s, working_directory, extra_variables):
        raise SeatError(
            seat, "exit code 1: Claude usage limit reached. Your limit will reset at 2pm (UTC)."
        )

    monkeypatch.setattr("adda.seats.base.run_command", fake_run_command)
    with pytest.raises(SeatRateLimitedError) as caught:
        asyncio.run(ClaudeSeat("ada").run("hi", "sys", None, 5))
    assert caught.value.resets_at is not None


def test_nonzero_exit_without_limit_text_stays_plain(monkeypatch):
    async def fake_run_command(seat, command, timeout_s, working_directory, extra_variables):
        raise SeatError(seat, "exit code 1: boom")

    monkeypatch.setattr("adda.seats.base.run_command", fake_run_command)
    with pytest.raises(SeatError) as caught:
        asyncio.run(ClaudeSeat("ada").run("hi", "sys", None, 5))
    assert not isinstance(caught.value, SeatRateLimitedError)


def test_login_seat_adds_no_provider_environment():
    assert ClaudeSeat("ada").extra_environment("talk") == {}


def test_custom_provider_sets_base_url_and_token():
    seat = ClaudeSeat("ada", base_url="https://example.test/api", auth_token="tok")
    assert seat.extra_environment("talk") == {
        "ANTHROPIC_BASE_URL": "https://example.test/api",
        "ANTHROPIC_AUTH_TOKEN": "tok",
    }


def test_custom_provider_without_token_fails_loudly():
    seat = ClaudeSeat("ada", base_url="https://example.test/api")
    with pytest.raises(
        SeatError, match="claude needs ANTHROPIC_AUTH_TOKEN for https://example.test/api"
    ):
        seat.extra_environment("talk")


def test_api_key_is_still_stripped_from_the_seat_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-pass")
    environment = base.seat_environment({"ANTHROPIC_AUTH_TOKEN": "tok"})
    assert "ANTHROPIC_API_KEY" not in environment
    assert environment["ANTHROPIC_AUTH_TOKEN"] == "tok"


def test_registry_passes_provider_settings_to_claude_only(tmp_path):
    settings = HarnessSettings(model="m", base_url="https://example.test", auth_token="tok")
    config = SeatConfig(room_id="r", name="ada", program="claude")
    adapter = build_adapter(config, settings, tmp_path)
    assert adapter.base_url == "https://example.test"
    assert adapter.auth_token == "tok"
    other = build_adapter(SeatConfig(room_id="r", name="bo", program="pi"), settings, tmp_path)
    assert not hasattr(other, "base_url")
