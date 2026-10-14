"""Claude seat: `claude -p` under the owner's Max login.

Verified 2026-10-14 from `claude --help` (not by running a prompt): -p/--print,
--output-format json, --model, -r/--resume, --append-system-prompt, --tools, --strict-mcp-config.
`--tools` is variadic, so the prompt positional must come right after -p and before --tools;
`--tools ""` disables every tool. Output is one JSON object with type "result", is_error,
result, session_id and total_cost_usd. Sample: tests/fixtures/seat_outputs/claude_*.json.
"""

from __future__ import annotations

import json

from adda.models import SeatReply
from adda.seats.base import CommandSeat, SeatError


class ClaudeSeat(CommandSeat):
    program = "claude"
    binary = "claude"

    def build_command(self, prompt: str, system_prompt: str, session_id: str | None) -> list[str]:
        command = [
            self.binary,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--tools",
            "",
            # Without this, MCP servers from the owner's config would start for every turn.
            "--strict-mcp-config",
            "--append-system-prompt",
            system_prompt,
        ]
        if self.model:
            command += ["--model", self.model]
        if session_id:
            command += ["--resume", session_id]
        return command

    def parse_output(self, stdout: str, session_id: str | None) -> SeatReply:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise SeatError(self.name, f"output is not JSON: {stdout[:200]!r}") from error
        if not isinstance(payload, dict):
            raise SeatError(self.name, f"output is not a JSON object: {stdout[:200]!r}")
        if payload.get("is_error"):
            raise SeatError(self.name, str(payload.get("result") or "claude reported an error"))
        result_text = payload.get("result")
        if not isinstance(result_text, str):
            raise SeatError(self.name, "output has no string 'result' field")
        return SeatReply(
            text=result_text,
            session_id=payload.get("session_id") or session_id,
            cost_usd=payload.get("total_cost_usd"),
            raw=payload,
        )
