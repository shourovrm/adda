"""OpenCode seat: `opencode run --format json`.

Verified 2026-10-14 against opencode 1.17.7 from `opencode run --help` and strings in the
binary (no prompt was run): `-s/--session <id>`, `-m/--model provider/model`, `--format json`.
There is no system-prompt flag, so the system prompt is prepended to the prompt on the first
turn only; the session keeps it afterwards. Options come first and the message last after "--"
so a message starting with "-" is not read as a flag; the binary joins
`[...message, ...argv["--"]]`, so text after "--" is the message. Output is JSONL, one object
per line with type step_start/step_finish/text/tool_use/reasoning/error and a sessionID. Samples:
tests/fixtures/seat_outputs/opencode_*.jsonl.
"""

from __future__ import annotations

from typing import Any

from adda.models import SeatReply
from adda.seats.base import CommandSeat, SeatError, parse_json_lines


class OpenCodeSeat(CommandSeat):
    program = "opencode"
    binary = "opencode"

    def build_command(self, prompt: str, system_prompt: str, session_id: str | None) -> list[str]:
        command = [self.binary, "run", "--format", "json"]
        if self.model:
            command += ["--model", self.model]
        if session_id:
            command += ["--session", session_id]
            message = prompt
        else:
            message = f"{system_prompt}\n\n{prompt}"
        return command + ["--", message]

    def parse_output(self, stdout: str, session_id: str | None) -> SeatReply:
        events = parse_json_lines(self.name, stdout)
        text_parts: list[str] = []
        total_cost = 0.0
        found_cost = False
        found_session_id = session_id
        for event in events:
            found_session_id = event.get("sessionID") or found_session_id
            event_type = event.get("type")
            part = event.get("part") or {}
            if event_type == "error":
                raise SeatError(self.name, self._error_message(event))
            if event_type == "text" and part.get("text"):
                text_parts.append(part["text"])
            elif event_type == "step_finish" and isinstance(part.get("cost"), int | float):
                total_cost += part["cost"]
                found_cost = True
        reply_text = "\n\n".join(text_parts).strip()
        if not reply_text:
            raise SeatError(self.name, "output has no text part")
        return SeatReply(
            text=reply_text,
            session_id=found_session_id,
            cost_usd=total_cost if found_cost else None,
            raw={"events": events},
        )

    @staticmethod
    def _error_message(event: dict[str, Any]) -> str:
        error = event.get("error")
        if isinstance(error, dict):
            data = error.get("data") or {}
            message = data.get("message") if isinstance(data, dict) else None
            return str(message or error.get("name") or error)
        return str(error or "opencode reported an error")
