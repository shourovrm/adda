"""Pi seat: `pi -p --mode json`.

Verified 2026-10-14 from `pi --help` and the installed docs (docs/json.md, message-types.md of
@earendil-works/pi-coding-agent); no prompt was run. `--system-prompt` replaces the default and
is passed every turn; `--session-id <id>` takes an exact id and creates it if missing, so it
serves for resume. Tools, MCP, context files and skills are off; extensions stay on because they
may provide model providers. The prompt goes last after "--". Output is JSONL: a first
{"type":"session","id":...} header, then events; `message_end` with message.role "assistant"
carries content blocks, stopReason, errorMessage and usage.cost.total. Pi exits 0 even when the
model call failed, so stopReason is checked here. Samples: tests/fixtures/seat_outputs/pi_*.jsonl.

Access levels, verified 2026-10-14 from `pi --help` and docs/cli.md (example
`pi --tools read,grep,find,ls --print ...`; no prompt run): --tools is a comma-separated
allowlist, --no-tools disables all tools, --no-mcp disables MCP.
- talk: --no-tools --no-mcp --no-context-files --no-skills
- look: --tools read,grep,find,ls --no-mcp
- work: --no-mcp (the built-in read, bash, edit, write)
Look and work leave context files and skills on so the seat sees the project's AGENTS.md.
Pi has no sandbox or folder confinement: look and work rely on the tool set alone.
"""

from __future__ import annotations

from adda.models import Access, SeatReply
from adda.seats.base import CommandSeat, SeatError, parse_json_lines

FAILED_STOP_REASONS = ("error", "aborted")


class PiSeat(CommandSeat):
    program = "pi"
    binary = "pi"

    def build_command(
        self, prompt: str, system_prompt: str, session_id: str | None, access: Access
    ) -> list[str]:
        command = [
            self.binary,
            "-p",
            "--mode",
            "json",
            *self._access_arguments(access),
            "--system-prompt",
            system_prompt,
        ]
        if self.model:
            command += ["--model", self.model]
        command += self.thinking_arguments  # `--thinking <level>`, before the "--" separator
        if session_id:
            command += ["--session-id", session_id]
        return command + ["--", prompt]

    @staticmethod
    def _access_arguments(access: Access) -> list[str]:
        if access == "look":
            return ["--tools", "read,grep,find,ls", "--no-mcp"]
        if access == "work":
            return ["--no-mcp"]
        return ["--no-tools", "--no-mcp", "--no-context-files", "--no-skills"]

    def parse_output(self, stdout: str, session_id: str | None) -> SeatReply:
        events = parse_json_lines(self.name, stdout)
        found_session_id = session_id
        assistant_messages = []
        total_cost = 0.0
        found_cost = False
        for event in events:
            if event.get("type") == "session" and event.get("id"):
                found_session_id = event["id"]
            if event.get("type") != "message_end":
                continue
            message = event.get("message") or {}
            if message.get("role") != "assistant":
                continue
            assistant_messages.append(message)
            cost = (message.get("usage") or {}).get("cost", {}).get("total")
            if isinstance(cost, int | float):
                total_cost += cost
                found_cost = True
        if not assistant_messages:
            raise SeatError(self.name, "output has no assistant message")

        last_message = assistant_messages[-1]
        if last_message.get("stopReason") in FAILED_STOP_REASONS:
            detail = last_message.get("errorMessage") or last_message["stopReason"]
            raise SeatError(self.name, str(detail))
        text_blocks = [
            block["text"]
            for block in last_message.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
        ]
        reply_text = "\n\n".join(text_blocks).strip()
        if not reply_text:
            raise SeatError(self.name, "last assistant message has no text")
        return SeatReply(
            text=reply_text,
            session_id=found_session_id,
            cost_usd=total_cost if found_cost else None,
            raw={"events": events},
        )
