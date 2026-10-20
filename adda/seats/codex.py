"""Codex seat: `codex exec --json`.

Verified on 2026-10-19 against codex-cli 0.163.0 by reading `codex exec --help` and
`codex exec resume --help` and by running two real calls (a first turn and a resume, model
`openai/gpt-5-nano` through OpenRouter). The recorded output is in
tests/fixtures/seat_outputs/codex_real_first_turn.jsonl and codex_real_resume.jsonl.

Event shapes seen on stdout (one JSON object per line):
    {"type":"thread.started","thread_id":"<uuid>"}   the session id; a resume repeats the same id
    {"type":"item.completed","item":{"id","type":"agent_message","text":...}}   the reply
    {"type":"item.completed","item":{"id","type":"error","message":...}}   a warning, not a
        failure (here: "Model metadata for ... not found"); the turn still completes
    {"type":"turn.started"}
    {"type":"turn.completed","usage":{"input_tokens","cached_input_tokens",
        "cache_write_input_tokens","output_tokens","reasoning_output_tokens"}}
    "Reading additional input from stdin..." goes to stderr; the runner closes stdin.
Failure events ({"type":"error"} and {"type":"turn.failed"}) were not provoked in the two calls;
their shape is a guess and stays UNVERIFIED.

`codex exec` has no system-prompt flag, so the persona is prepended to the first message only;
a resumed session already holds it.

Access levels: `codex exec` has `--sandbox read-only|workspace-write|danger-full-access` and no
`--ask-for-approval` (exec never stops to ask). Look maps to read-only and work to
workspace-write. `codex exec resume` has no `--sandbox` flag, so a resumed turn sets the same
policy with `-c sandbox_mode=...`. Talk is read-only too, because codex cannot be started with
no tools; the access sentence in the room rules tells it to only talk.
"""

from __future__ import annotations

from typing import Any

from adda.models import Access, SeatReply
from adda.seats.base import CommandSeat, SeatError, parse_json_lines

SANDBOX_FOR_ACCESS = {"talk": "read-only", "look": "read-only", "work": "workspace-write"}


class CodexSeat(CommandSeat):
    program = "codex"
    binary = "codex"

    def build_command(
        self, prompt: str, system_prompt: str, session_id: str | None, access: Access
    ) -> list[str]:
        sandbox_mode = SANDBOX_FOR_ACCESS[access]
        if session_id:
            # `exec resume` has no --sandbox flag, so the policy goes in as a config override.
            command = [self.binary, "exec", "resume", session_id, "--json"]
            command += ["-c", f'sandbox_mode="{sandbox_mode}"']
            message = prompt
        else:
            command = [self.binary, "exec", "--json", "--sandbox", sandbox_mode]
            message = f"{system_prompt}\n\n{prompt}"
        # The seat's working folder is not necessarily a git repository.
        command.append("--skip-git-repo-check")
        if self.model:
            command += ["--model", self.model]
        command += self.thinking_arguments  # `-c model_reasoning_effort=<level>`
        return command + [message]

    def parse_output(self, stdout: str, session_id: str | None) -> SeatReply:
        events = parse_json_lines(self.name, stdout)
        found_session_id = session_id
        message_texts: list[str] = []
        input_tokens: int | None = None
        cached_input_tokens: int | None = None
        output_tokens: int | None = None
        for event in events:
            event_type = event.get("type")
            if event_type == "thread.started" and event.get("thread_id"):
                found_session_id = event["thread_id"]
            elif event_type in ("error", "turn.failed"):
                raise SeatError(self.name, self._error_message(event))
            elif event_type == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    message_texts.append(item["text"])
            elif event_type == "turn.completed":
                usage = event.get("usage") or {}
                input_tokens = _added(input_tokens, usage.get("input_tokens"))
                cached_input_tokens = _added(cached_input_tokens, usage.get("cached_input_tokens"))
                output_tokens = _added(output_tokens, usage.get("output_tokens"))
        reply_text = "\n\n".join(message_texts).strip()
        if not reply_text:
            raise SeatError(self.name, "output has no agent message")
        return SeatReply(
            text=reply_text,
            session_id=found_session_id,
            cost_usd=None,  # codex reports tokens but no price
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            raw={"events": events},
        )

    @staticmethod
    def _error_message(event: dict[str, Any]) -> str:
        error = event.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        return str(event.get("message") or error or "codex reported an error")


def _added(total: int | None, count: object) -> int | None:
    """Add a reported token count to a running total; absent counts leave it unchanged."""
    if not isinstance(count, int):
        return total
    return count if total is None else total + count
