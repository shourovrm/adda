"""OpenCode seat: `opencode run --format json`.

Verified 2026-10-14 against opencode 1.17.7 from `opencode run --help` and strings in the
binary (no prompt was run): `-s/--session <id>`, `-m/--model provider/model`, `--format json`.
There is no system-prompt flag, so the system prompt is prepended to the prompt on the first
turn only; the session keeps it afterwards. Options come first and the message last after "--"
so a message starting with "-" is not read as a flag; the binary joins
`[...message, ...argv["--"]]`, so text after "--" is the message. Output is JSONL, one object
per line with type step_start/step_finish/text/tool_use/reasoning/error and a sessionID. Samples:
tests/fixtures/seat_outputs/opencode_*.jsonl.

Access levels, verified 2026-10-14 against opencode 1.17.7 from the config skill text embedded
in the binary (no prompt run): the environment variable OPENCODE_CONFIG names an extra config
file that is merged over the global and project configs; its `permission` block maps a tool key
to "allow", "ask" or "deny" (keys read, edit, glob, grep, list, bash, task, external_directory,
webfetch, websearch, lsp, skill...). "ask" cannot be answered headless, so every key is set
explicitly. Each level has one file, `<data_dir>/opencode-config/<access>.json`, written before
the run. `opencode run --help` has no tool or permission flag; `--dangerously-skip-permissions`
is never used. MCP servers from the owner's own config are NOT switched off (no verified
switch), and a project opencode.json in the room folder may still override these rules.
- talk: every key denied
- look: read, glob, grep, list allowed; the rest denied
- work: read, glob, grep, list, edit, bash allowed; webfetch, websearch, external_directory denied
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from adda.models import Access, SeatReply
from adda.seats.base import CommandSeat, SeatError, parse_json_lines

DEFAULT_DATA_DIR = Path("~/.adda")
CONFIG_VARIABLE = "OPENCODE_CONFIG"
READ_KEYS = ("read", "glob", "grep", "list")
EDIT_KEYS = ("edit", "bash")
DENIED_ALWAYS_KEYS = ("webfetch", "websearch", "external_directory", "task", "lsp", "skill")


def permission_rules(access: Access) -> dict[str, str]:
    """The `permission` block for one level. Unknown levels get the talk rules."""
    allowed: tuple[str, ...] = ()
    if access == "look":
        allowed = READ_KEYS
    elif access == "work":
        allowed = READ_KEYS + EDIT_KEYS
    every_key = READ_KEYS + EDIT_KEYS + DENIED_ALWAYS_KEYS
    return {key: "allow" if key in allowed else "deny" for key in every_key}


class OpenCodeSeat(CommandSeat):
    program = "opencode"
    binary = "opencode"

    def config_path(self, access: Access) -> Path:
        data_dir = (self.data_dir or DEFAULT_DATA_DIR).expanduser()
        return data_dir / "opencode-config" / f"{access}.json"

    def extra_environment(self, access: Access) -> dict[str, str]:
        """Write the level's config file (same content every time) and point opencode at it."""
        config_path = self.config_path(access)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config = {
            "$schema": "https://opencode.ai/config.json",
            "permission": permission_rules(access),
        }
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        return {CONFIG_VARIABLE: str(config_path)}

    def build_command(
        self, prompt: str, system_prompt: str, session_id: str | None, access: Access
    ) -> list[str]:
        # `access` reaches opencode through the config file named by extra_environment, not
        # through a flag: `opencode run` has none.
        command = [self.binary, "run", "--format", "json"]
        if self.model:
            command += ["--model", self.model]
        command += self.thinking_arguments  # `--variant <level>`, before the "--" separator
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
