"""Codex seat: `codex exec --json`.

UNVERIFIED: codex not installed on this machine. Flags follow the SPEC section 4 table and the
JSONL shapes are a best guess: {"type":"thread.started","thread_id":...},
{"type":"item.completed","item":{"type":"agent_message","text":...}},
{"type":"error"|"turn.failed"} for failures. Verify with `codex exec --help` once installed.
No system-prompt flag is assumed, so it is prepended on the first turn only. Sample:
tests/fixtures/seat_outputs/codex_success.jsonl (hand-written, not recorded).
"""

from __future__ import annotations

from typing import Any

from adda.models import SeatReply
from adda.seats.base import CommandSeat, SeatError, parse_json_lines


class CodexSeat(CommandSeat):
    program = "codex"
    binary = "codex"

    def build_command(self, prompt: str, system_prompt: str, session_id: str | None) -> list[str]:
        if session_id:
            command = [self.binary, "exec", "resume", session_id, "--json"]
            message = prompt
        else:
            command = [self.binary, "exec", "--json", "--sandbox", "read-only"]
            message = f"{system_prompt}\n\n{prompt}"
        if self.model:
            command += ["--model", self.model]
        command += self.thinking_arguments  # `-c model_reasoning_effort=<level>` (unverified)
        return command + [message]

    def parse_output(self, stdout: str, session_id: str | None) -> SeatReply:
        events = parse_json_lines(self.name, stdout)
        found_session_id = session_id
        message_texts: list[str] = []
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
        reply_text = "\n\n".join(message_texts).strip()
        if not reply_text:
            raise SeatError(self.name, "output has no agent message")
        return SeatReply(
            text=reply_text, session_id=found_session_id, cost_usd=None, raw={"events": events}
        )

    @staticmethod
    def _error_message(event: dict[str, Any]) -> str:
        error = event.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        return str(event.get("message") or error or "codex reported an error")
