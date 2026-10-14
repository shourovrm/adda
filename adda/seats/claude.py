"""Claude seat: `claude -p` under the owner's Max login.

Verified 2026-10-14 from `claude --help` (not by running a prompt): -p/--print,
--output-format json, --model, -r/--resume, --append-system-prompt, --tools, --strict-mcp-config.
`--effort <low|medium|high|xhigh|max>` selects thinking (see harnesses.py).
`--tools` is variadic, so the prompt positional must come right after -p and before --tools;
`--tools ""` disables every tool. Output is one JSON object with type "result", is_error,
result, session_id and total_cost_usd. Sample: tests/fixtures/seat_outputs/claude_*.json.

Access levels, verified 2026-10-14 against claude 2.1.299 from `claude --help` (no prompt run):
--tools <tools...> takes a comma list (or "" for none), --allowedTools takes a comma or space
list, --add-dir <directories...> grants file-tool access to a folder, --permission-mode accepts
acceptEdits, --permission-prompts none denies whatever would prompt (nobody can answer
headless). Tool names Read, Grep, Glob, LS, Edit, Write, MultiEdit and Bash appear in the
binary but were not exercised, since that needs a prompt. All three variadic flags are followed
by another flag so they never swallow the next argument.
- talk: --tools ""
- look: --tools Read,Grep,Glob,LS --add-dir <folder> --permission-prompts none
- work: look's flags plus Edit,Write,MultiEdit,Bash in --tools and --allowedTools, and
  --permission-mode acceptEdits. Restricting --tools too (SPEC lists only --allowedTools) keeps
  WebFetch, Task and the rest unavailable. Bash is not confined to the folder: work is full
  trust.
"""

from __future__ import annotations

import json

from adda.models import Access, SeatReply
from adda.seats.base import CommandSeat, SeatError

READ_ONLY_TOOLS = "Read,Grep,Glob,LS"
WORK_TOOLS = "Read,Grep,Glob,LS,Edit,Write,MultiEdit,Bash"


class ClaudeSeat(CommandSeat):
    program = "claude"
    binary = "claude"

    def build_command(
        self, prompt: str, system_prompt: str, session_id: str | None, access: Access
    ) -> list[str]:
        command = [
            self.binary,
            "-p",
            prompt,
            "--output-format",
            "json",
            *self._access_arguments(access),
            # Without this, MCP servers from the owner's config would start for every turn.
            "--strict-mcp-config",
            "--append-system-prompt",
            system_prompt,
        ]
        if self.model:
            command += ["--model", self.model]
        command += self.thinking_arguments
        if session_id:
            command += ["--resume", session_id]
        return command

    def _access_arguments(self, access: Access) -> list[str]:
        if access == "talk":
            return ["--tools", ""]
        arguments = ["--permission-prompts", "none"]
        if self.working_directory is not None:
            arguments += ["--add-dir", str(self.working_directory)]
        if access == "look":
            return ["--tools", READ_ONLY_TOOLS, *arguments]
        return [
            "--tools",
            WORK_TOOLS,
            "--allowedTools",
            WORK_TOOLS,
            "--permission-mode",
            "acceptEdits",
            *arguments,
        ]

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
