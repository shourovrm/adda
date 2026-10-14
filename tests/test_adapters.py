"""Command construction and output parsing for each seat adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from adda.config import HarnessSettings
from adda.models import SeatConfig
from adda.seats.base import SeatError
from adda.seats.claude import ClaudeSeat
from adda.seats.codex import CodexSeat
from adda.seats.opencode import OpenCodeSeat
from adda.seats.pi import PiSeat
from adda.seats.registry import build_adapter

FIXTURES = Path(__file__).parent / "fixtures" / "seat_outputs"
SYSTEM = "You are Ada."


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_config(name: str, program: str, **fields) -> SeatConfig:
    return SeatConfig(room_id="room1", name=name, program=program, **fields)


# --- claude ---------------------------------------------------------------------------------


def test_claude_first_turn_command():
    command = ClaudeSeat("ada").build_command("hi", SYSTEM, None)
    assert command[:3] == ["claude", "-p", "hi"]
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--append-system-prompt") + 1] == SYSTEM
    assert "--strict-mcp-config" in command
    assert "--resume" not in command
    assert "--model" not in command


def test_claude_resume_and_model():
    command = ClaudeSeat("ada", model="haiku").build_command("hi", SYSTEM, "sess-1")
    assert command[command.index("--resume") + 1] == "sess-1"
    assert command[command.index("--model") + 1] == "haiku"
    assert command[command.index("--append-system-prompt") + 1] == SYSTEM


def test_claude_parse_success():
    reply = ClaudeSeat("ada").parse_output(fixture_text("claude_success.json"), None)
    assert reply.text == "I would keep the queue in SQLite."
    assert reply.session_id == "5f0c1d7e-2b44-4c1e-9a53-0d9f6a1b7c10"
    assert reply.cost_usd == pytest.approx(0.0123)


def test_claude_parse_error_result():
    with pytest.raises(SeatError, match="No conversation found"):
        ClaudeSeat("ada").parse_output(fixture_text("claude_error.json"), "x")


def test_claude_garbage():
    with pytest.raises(SeatError):
        ClaudeSeat("ada").parse_output("not json at all", None)


# --- opencode -------------------------------------------------------------------------------


def test_opencode_first_turn_prepends_system_prompt():
    command = OpenCodeSeat("bo").build_command("hi", SYSTEM, None)
    assert command[:2] == ["opencode", "run"]
    assert command[-2] == "--"
    assert command[-1] == f"{SYSTEM}\n\nhi"
    assert "--session" not in command


def test_opencode_resume_does_not_repeat_system_prompt():
    command = OpenCodeSeat("bo", model="anthropic/haiku").build_command("- hi", SYSTEM, "ses_1")
    assert command[command.index("--session") + 1] == "ses_1"
    assert command[command.index("--model") + 1] == "anthropic/haiku"
    assert command[-2:] == ["--", "- hi"]


def test_opencode_parse_success():
    reply = OpenCodeSeat("bo").parse_output(fixture_text("opencode_success.jsonl"), None)
    assert reply.text == "First point.\n\nSecond point."
    assert reply.session_id == "ses_abc123"
    assert reply.cost_usd == pytest.approx(0.004)


def test_opencode_parse_error_line():
    with pytest.raises(SeatError, match="model not found"):
        OpenCodeSeat("bo").parse_output(fixture_text("opencode_error.jsonl"), None)


def test_opencode_garbage():
    with pytest.raises(SeatError):
        OpenCodeSeat("bo").parse_output("garbage", None)


# --- pi -------------------------------------------------------------------------------------


def test_pi_first_turn_command():
    command = PiSeat("cy").build_command("hi", SYSTEM, None)
    assert command[:4] == ["pi", "-p", "--mode", "json"]
    for flag in ("--no-tools", "--no-mcp", "--no-context-files", "--no-skills"):
        assert flag in command
    assert command[command.index("--system-prompt") + 1] == SYSTEM
    assert command[-2:] == ["--", "hi"]
    assert "--session-id" not in command
    assert "--model" not in command


def test_pi_resume_and_model():
    command = PiSeat("cy", model="some/model").build_command("hi", SYSTEM, "abc")
    assert command[command.index("--session-id") + 1] == "abc"
    assert command[command.index("--model") + 1] == "some/model"


def test_pi_parse_success():
    reply = PiSeat("cy").parse_output(fixture_text("pi_success.jsonl"), None)
    assert reply.text == "Pi says hello."
    assert reply.session_id == "0b1f6c52-7d5a-4e0e-8a3b-111122223333"
    assert reply.cost_usd == pytest.approx(0.002)


def test_pi_error_stop_reason():
    with pytest.raises(SeatError, match="401 invalid credentials"):
        PiSeat("cy").parse_output(fixture_text("pi_error.jsonl"), None)


def test_pi_no_assistant_message():
    header_only = fixture_text("pi_success.jsonl").splitlines()[0]
    with pytest.raises(SeatError, match="no assistant message"):
        PiSeat("cy").parse_output(header_only, None)


def test_pi_garbage():
    with pytest.raises(SeatError):
        PiSeat("cy").parse_output("garbage", None)


# --- codex (command shape unverified: codex is not installed) --------------------------------


def test_codex_first_turn_command():
    command = CodexSeat("di", model="m").build_command("hi", SYSTEM, None)
    assert command[:3] == ["codex", "exec", "--json"]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--model") + 1] == "m"
    assert command[-1] == f"{SYSTEM}\n\nhi"


def test_codex_resume_command():
    command = CodexSeat("di").build_command("hi", SYSTEM, "thread-1")
    assert command[:4] == ["codex", "exec", "resume", "thread-1"]
    assert command[-1] == "hi"


def test_codex_parse_success():
    reply = CodexSeat("di").parse_output(fixture_text("codex_success.jsonl"), None)
    assert reply.text == "Codex answer."
    assert reply.session_id == "019a-thread-0001"


def test_codex_failure_event():
    output = '{"type":"turn.failed","error":{"message":"rate limited"}}'
    with pytest.raises(SeatError, match="rate limited"):
        CodexSeat("di").parse_output(output, None)


def test_codex_garbage():
    with pytest.raises(SeatError):
        CodexSeat("di").parse_output("garbage", None)


# --- thinking flags -------------------------------------------------------------------------


def test_claude_thinking_flag_comes_before_resume():
    seat = ClaudeSeat("ada", thinking_arguments=["--effort", "high"])
    command = seat.build_command("hi", SYSTEM, "sess-1")
    assert command[command.index("--effort") + 1] == "high"


def test_opencode_and_pi_thinking_flags_stay_before_the_prompt_separator():
    opencode = OpenCodeSeat("bo", thinking_arguments=["--variant", "low"])
    command = opencode.build_command("hi", SYSTEM, None)
    assert command.index("--variant") < command.index("--")
    assert command[command.index("--variant") + 1] == "low"

    pi = PiSeat("cy", thinking_arguments=["--thinking", "medium"])
    command = pi.build_command("hi", SYSTEM, None)
    assert command.index("--thinking") < command.index("--")
    assert command[command.index("--thinking") + 1] == "medium"


def test_codex_thinking_flag_precedes_the_message():
    command = CodexSeat(
        "di", thinking_arguments=["-c", "model_reasoning_effort=high"]
    ).build_command("hi", SYSTEM, None)
    assert command[-3:-1] == ["-c", "model_reasoning_effort=high"]


def test_no_thinking_arguments_means_no_flag():
    for seat_class in (ClaudeSeat, OpenCodeSeat, PiSeat, CodexSeat):
        command = seat_class("x").build_command("hi", SYSTEM, None)
        assert not {"--effort", "--variant", "--thinking"} & set(command)


# --- registry -------------------------------------------------------------------------------


def test_registry_builds_each_program(tmp_path):
    programs = {
        "claude": ClaudeSeat,
        "opencode": OpenCodeSeat,
        "pi": PiSeat,
        "codex": CodexSeat,
    }
    for program, expected_class in programs.items():
        adapter = build_adapter(make_config("Ada", program, model="m"), None, tmp_path)
        assert type(adapter) is expected_class
        assert adapter.name == "Ada"
        assert adapter.model == "m"
        assert adapter.working_directory == tmp_path


def test_registry_uses_harness_default_model_when_seat_has_none(tmp_path):
    defaults = HarnessSettings(model="sonnet", thinking="default")
    inherited = build_adapter(make_config("Ada", "claude"), defaults, tmp_path)
    assert inherited.model == "sonnet"
    explicit = build_adapter(make_config("Ada", "claude", model="haiku"), defaults, tmp_path)
    assert explicit.model == "haiku"
    blank_default = build_adapter(
        make_config("Ada", "pi"), HarnessSettings(model="", thinking="default"), tmp_path
    )
    assert blank_default.model is None


@pytest.mark.parametrize(
    ("program", "seat_thinking", "default_thinking", "expected"),
    [
        ("claude", "high", "medium", ["--effort", "high"]),
        ("claude", "default", "medium", ["--effort", "medium"]),  # harness default applies
        ("claude", "default", "default", []),
        ("pi", "low", "default", ["--thinking", "low"]),
        ("opencode", "medium", "default", ["--variant", "medium"]),
        ("codex", "high", "default", ["-c", "model_reasoning_effort=high"]),
    ],
)
def test_registry_maps_thinking_to_flags(
    tmp_path, program, seat_thinking, default_thinking, expected
):
    defaults = HarnessSettings(thinking=default_thinking)
    config = make_config("Ada", program, thinking=seat_thinking)
    assert build_adapter(config, defaults, tmp_path).thinking_arguments == expected


def test_registry_unknown_program(tmp_path):
    config = make_config("x", "claude").model_copy(update={"program": "emacs"})
    with pytest.raises(ValueError, match="unknown program"):
        build_adapter(config, None, tmp_path)
