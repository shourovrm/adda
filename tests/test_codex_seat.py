from pathlib import Path

import pytest

from adda.seats.base import SeatError
from adda.seats.codex import CodexSeat

FIXTURES = Path(__file__).parent / "fixtures" / "seat_outputs"
SYSTEM = "You are Di."


def fixture_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_real_first_turn_gives_reply_session_and_tokens():
    reply = CodexSeat("di").parse_output(fixture_text("codex_real_first_turn.jsonl"), None)
    assert reply.text == "ok"
    assert reply.session_id == "01a152ed-2775-77f2-b158-486b676fa5b5"
    assert (reply.input_tokens, reply.cached_input_tokens, reply.output_tokens) == (12096, 0, 74)
    assert reply.cost_usd is None


def test_real_resume_reports_cached_input():
    reply = CodexSeat("di").parse_output(fixture_text("codex_real_resume.jsonl"), "other")
    assert reply.session_id == "01a152ed-2775-77f2-b158-486b676fa5b5"
    assert (reply.input_tokens, reply.cached_input_tokens, reply.output_tokens) == (
        24329,
        11904,
        169,
    )


def test_error_item_is_a_warning_not_a_failure():
    # The real first turn contains an item of type "error" ("Model metadata ... not found").
    assert '"type":"error"' in fixture_text("codex_real_first_turn.jsonl")
    CodexSeat("di").parse_output(fixture_text("codex_real_first_turn.jsonl"), None)


def test_usage_is_summed_over_several_turns():
    output = "\n".join(
        [
            '{"type":"item.completed","item":{"type":"agent_message","text":"a"}}',
            '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":4,"output_tokens":1}}',
            '{"type":"turn.completed","usage":{"input_tokens":20,"cached_input_tokens":6,"output_tokens":2}}',
        ]
    )
    reply = CodexSeat("di").parse_output(output, None)
    assert (reply.input_tokens, reply.cached_input_tokens, reply.output_tokens) == (30, 10, 3)


def test_missing_usage_leaves_tokens_unknown():
    output = '{"type":"item.completed","item":{"type":"agent_message","text":"a"}}'
    reply = CodexSeat("di").parse_output(output, None)
    assert reply.input_tokens is None and reply.output_tokens is None


def test_no_agent_message_fails_loudly():
    with pytest.raises(SeatError, match="no agent message"):
        CodexSeat("di").parse_output('{"type":"turn.completed","usage":{}}', None)


@pytest.mark.parametrize(
    ("access", "sandbox_mode"),
    [("talk", "read-only"), ("look", "read-only"), ("work", "workspace-write")],
)
def test_first_turn_sandbox_follows_access(access, sandbox_mode):
    command = CodexSeat("di").build_command("hi", SYSTEM, None, access)
    assert command[command.index("--sandbox") + 1] == sandbox_mode
    assert "--skip-git-repo-check" in command
    assert command[-1] == f"{SYSTEM}\n\nhi"


@pytest.mark.parametrize(
    ("access", "sandbox_mode"), [("look", "read-only"), ("work", "workspace-write")]
)
def test_resume_sets_sandbox_by_config_override(access, sandbox_mode):
    command = CodexSeat("di", model="m").build_command("hi", SYSTEM, "thread-1", access)
    assert command[:4] == ["codex", "exec", "resume", "thread-1"]
    assert "--sandbox" not in command  # `exec resume` has no such flag
    assert command[command.index("-c") + 1] == f'sandbox_mode="{sandbox_mode}"'
    assert command[command.index("--model") + 1] == "m"
    assert command[-1] == "hi"
