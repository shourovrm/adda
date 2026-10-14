"""Command construction and output parsing for each seat adapter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adda.config import HarnessSettings
from adda.models import Seat, SeatConfig
from adda.seats.base import SeatError, seat_environment
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
    command = ClaudeSeat("ada").build_command("hi", SYSTEM, None, "talk")
    assert command[:3] == ["claude", "-p", "hi"]
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--append-system-prompt") + 1] == SYSTEM
    assert "--strict-mcp-config" in command
    assert "--resume" not in command
    assert "--model" not in command


def test_claude_resume_and_model():
    command = ClaudeSeat("ada", model="haiku").build_command("hi", SYSTEM, "sess-1", "talk")
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
    command = OpenCodeSeat("bo").build_command("hi", SYSTEM, None, "talk")
    assert command[:2] == ["opencode", "run"]
    assert command[-2] == "--"
    assert command[-1] == f"{SYSTEM}\n\nhi"
    assert "--session" not in command


def test_opencode_resume_does_not_repeat_system_prompt():
    command = OpenCodeSeat("bo", model="anthropic/haiku").build_command(
        "- hi", SYSTEM, "ses_1", "talk"
    )
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
    command = PiSeat("cy").build_command("hi", SYSTEM, None, "talk")
    assert command[:4] == ["pi", "-p", "--mode", "json"]
    for flag in ("--no-tools", "--no-mcp", "--no-context-files", "--no-skills"):
        assert flag in command
    assert command[command.index("--system-prompt") + 1] == SYSTEM
    assert command[-2:] == ["--", "hi"]
    assert "--session-id" not in command
    assert "--model" not in command


def test_pi_resume_and_model():
    command = PiSeat("cy", model="some/model").build_command("hi", SYSTEM, "abc", "talk")
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
    command = CodexSeat("di", model="m").build_command("hi", SYSTEM, None, "talk")
    assert command[:3] == ["codex", "exec", "--json"]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--model") + 1] == "m"
    assert command[-1] == f"{SYSTEM}\n\nhi"


def test_codex_resume_command():
    command = CodexSeat("di").build_command("hi", SYSTEM, "thread-1", "talk")
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
    command = seat.build_command("hi", SYSTEM, "sess-1", "talk")
    assert command[command.index("--effort") + 1] == "high"


def test_opencode_and_pi_thinking_flags_stay_before_the_prompt_separator():
    opencode = OpenCodeSeat("bo", thinking_arguments=["--variant", "low"])
    command = opencode.build_command("hi", SYSTEM, None, "talk")
    assert command.index("--variant") < command.index("--")
    assert command[command.index("--variant") + 1] == "low"

    pi = PiSeat("cy", thinking_arguments=["--thinking", "medium"])
    command = pi.build_command("hi", SYSTEM, None, "talk")
    assert command.index("--thinking") < command.index("--")
    assert command[command.index("--thinking") + 1] == "medium"


def test_codex_thinking_flag_precedes_the_message():
    command = CodexSeat(
        "di", thinking_arguments=["-c", "model_reasoning_effort=high"]
    ).build_command("hi", SYSTEM, None, "talk")
    assert command[-3:-1] == ["-c", "model_reasoning_effort=high"]


def test_no_thinking_arguments_means_no_flag():
    for seat_class in (ClaudeSeat, OpenCodeSeat, PiSeat, CodexSeat):
        command = seat_class("x").build_command("hi", SYSTEM, None, "talk")
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


# --- access levels (SPEC v0.6 G2) -----------------------------------------------------------

ROOM_FOLDER = Path("/work/room")
READ_ONLY_CLAUDE_TOOLS = "Read,Grep,Glob,LS"
WORK_CLAUDE_TOOLS = "Read,Grep,Glob,LS,Edit,Write,MultiEdit,Bash"


def claude_command(access: str) -> list[str]:
    seat = ClaudeSeat("ada", working_directory=ROOM_FOLDER)
    return seat.build_command("hi", SYSTEM, None, access)


def test_claude_talk_sends_no_tools_and_no_folder():
    command = claude_command("talk")
    assert command[command.index("--tools") + 1] == ""
    assert "--add-dir" not in command
    assert "--allowedTools" not in command
    assert "--permission-prompts" not in command


def test_claude_look_allows_only_read_tools_in_the_room_folder():
    command = claude_command("look")
    assert command[command.index("--tools") + 1] == READ_ONLY_CLAUDE_TOOLS
    assert command[command.index("--add-dir") + 1] == str(ROOM_FOLDER)
    assert command[command.index("--permission-prompts") + 1] == "none"
    assert "--allowedTools" not in command
    assert "--permission-mode" not in command
    assert not {"Edit", "Write", "Bash"} & set(command[command.index("--tools") + 1].split(","))


def test_claude_work_allows_edit_write_and_shell_without_prompts():
    command = claude_command("work")
    assert command[command.index("--tools") + 1] == WORK_CLAUDE_TOOLS
    assert command[command.index("--allowedTools") + 1] == WORK_CLAUDE_TOOLS
    assert command[command.index("--permission-mode") + 1] == "acceptEdits"
    assert command[command.index("--permission-prompts") + 1] == "none"
    assert command[command.index("--add-dir") + 1] == str(ROOM_FOLDER)


@pytest.mark.parametrize("access", ["talk", "look", "work"])
def test_claude_variadic_flags_never_swallow_the_next_argument(access):
    command = claude_command(access)
    for flag in ("--tools", "--allowedTools", "--add-dir"):
        if flag in command:
            following = command[command.index(flag) + 2]
            assert following.startswith("--"), (flag, following)
    assert command[command.index("--append-system-prompt") + 1] == SYSTEM


def test_pi_commands_per_access_level():
    pi = PiSeat("cy")
    talk = pi.build_command("hi", SYSTEM, None, "talk")
    for flag in ("--no-tools", "--no-mcp", "--no-context-files", "--no-skills"):
        assert flag in talk
    look = pi.build_command("hi", SYSTEM, None, "look")
    assert look[look.index("--tools") + 1] == "read,grep,find,ls"
    assert "--no-mcp" in look
    assert "--no-tools" not in look
    work = pi.build_command("hi", SYSTEM, None, "work")
    assert "--no-mcp" in work
    assert "--tools" not in work
    assert "--no-tools" not in work
    for command in (talk, look, work):
        assert command[-2:] == ["--", "hi"]


def test_opencode_command_has_no_permission_flag_at_any_level():
    opencode = OpenCodeSeat("bo")
    commands = {level: opencode.build_command("hi", SYSTEM, None, level) for level in LEVELS}
    assert commands["talk"] == commands["look"] == commands["work"]
    assert "--dangerously-skip-permissions" not in commands["work"]


LEVELS = ("talk", "look", "work")


@pytest.mark.parametrize(
    ("access", "allowed", "denied"),
    [
        ("talk", set(), {"read", "glob", "grep", "list", "edit", "bash", "webfetch"}),
        ("look", {"read", "glob", "grep", "list"}, {"edit", "bash", "webfetch", "websearch"}),
        (
            "work",
            {"read", "glob", "grep", "list", "edit", "bash"},
            {"webfetch", "websearch", "external_directory"},
        ),
    ],
)
def test_opencode_writes_one_config_file_per_level(tmp_path, access, allowed, denied):
    seat = OpenCodeSeat("bo", data_dir=tmp_path)
    environment = seat.extra_environment(access)
    config_path = tmp_path / "opencode-config" / f"{access}.json"
    assert environment == {"OPENCODE_CONFIG": str(config_path)}
    permission = json.loads(config_path.read_text(encoding="utf-8"))["permission"]
    assert {key for key, value in permission.items() if value == "allow"} == allowed
    assert denied <= {key for key, value in permission.items() if value == "deny"}
    assert "ask" not in permission.values()  # nobody can answer headless


def test_other_harnesses_need_no_extra_environment():
    for seat_class in (ClaudeSeat, PiSeat, CodexSeat):
        assert seat_class("x").extra_environment("work") == {}


def test_seat_environment_adds_extra_variables_and_still_drops_billing_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    environment = seat_environment({"OPENCODE_CONFIG": "/x.json"})
    assert environment["OPENCODE_CONFIG"] == "/x.json"
    assert "ANTHROPIC_API_KEY" not in environment


@pytest.mark.parametrize("access", LEVELS)
def test_codex_falls_back_to_talk_because_its_flags_are_unverified(access):
    codex = CodexSeat("di")
    assert codex.build_command("hi", SYSTEM, None, access) == codex.build_command(
        "hi", SYSTEM, None, "talk"
    )


def test_registry_gives_the_adapter_the_effective_access_and_data_dir(tmp_path):
    seat = Seat.from_config(make_config("Ada", "opencode", access="look"), room_access="work")
    adapter = build_adapter(seat, None, tmp_path, tmp_path / "data")
    assert adapter.access == "look"
    assert adapter.data_dir == tmp_path / "data"
    inherited = Seat.from_config(make_config("Bo", "pi"), room_access="work")
    assert build_adapter(inherited, None, tmp_path).access == "work"
    assert build_adapter(make_config("Cy", "pi"), None, tmp_path).access == "talk"


async def test_run_builds_the_command_with_the_adapters_access(tmp_path, monkeypatch):
    seen = {}

    async def fake_run_command(seat, command, timeout_s, working_directory, extra_variables):
        seen.update(command=command, extra_variables=extra_variables)
        return fixture_text("opencode_success.jsonl")

    monkeypatch.setattr("adda.seats.base.run_command", fake_run_command)
    adapter = OpenCodeSeat("bo", working_directory=tmp_path, access="look", data_dir=tmp_path)
    await adapter.run("hi", SYSTEM, None, 5)
    assert seen["extra_variables"] == {
        "OPENCODE_CONFIG": str(tmp_path / "opencode-config" / "look.json")
    }
