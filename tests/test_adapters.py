"""Command construction and output parsing for each seat adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from adda.personas import Persona
from adda.seats.base import SeatError
from adda.seats.claude import ClaudeSeat
from adda.seats.codex import CodexSeat
from adda.seats.opencode import OpenCodeSeat
from adda.seats.pi import PiSeat
from adda.seats.registry import build_adapter, build_adapters

FIXTURES = Path(__file__).parent / "fixtures" / "seat_outputs"
SYSTEM = "You are Ada."


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_persona(name: str, program: str, model: str | None = None) -> Persona:
    return Persona(
        name=name,
        program=program,
        model=model,
        color="#000000",
        role="",
        stance="",
        body="",
        directory=Path("/nonexistent"),
    )


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


# --- registry -------------------------------------------------------------------------------


def test_registry_builds_classes_in_order(tmp_path):
    personas = {
        "ada": make_persona("ada", "claude", "haiku"),
        "bo": make_persona("bo", "opencode"),
        "cy": make_persona("cy", "pi"),
        "di": make_persona("di", "codex"),
    }
    adapters = build_adapters(personas, tmp_path)
    assert list(adapters) == ["ada", "bo", "cy", "di"]
    assert [type(adapter) for adapter in adapters.values()] == [
        ClaudeSeat,
        OpenCodeSeat,
        PiSeat,
        CodexSeat,
    ]
    assert adapters["ada"].name == "ada"
    assert adapters["ada"].model == "haiku"
    assert adapters["ada"].working_directory == tmp_path


def test_registry_unknown_program(tmp_path):
    with pytest.raises(ValueError, match="unknown program"):
        build_adapter(make_persona("x", "emacs"), tmp_path)
