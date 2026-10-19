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
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from adda.models import Access, SeatReply
from adda.seats.base import CommandSeat, SeatError, SeatRateLimitedError, SeatTimeoutError

READ_ONLY_TOOLS = "Read,Grep,Glob,LS"
WORK_TOOLS = "Read,Grep,Glob,LS,Edit,Write,MultiEdit,Bash"


# "3pm", "3:30 pm" (the am/pm form), or a 24-hour "14:00". A bare number such as "5 hours" is
# not a time, so the clock form needs either am/pm or minutes.
_CLOCK_WITH_MERIDIEM = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)
_CLOCK_24_HOUR = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_ZONE_IN_PARENTHESES = re.compile(r"\(([A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+)*)\)")


def is_rate_limit_text(text: str) -> bool:
    """True when claude's message says the plan refused the request.

    Seen: "You've hit your limit · resets 3pm (Asia/Dhaka)" and "Claude usage limit reached.
    Your limit will reset at 2pm (UTC)." So "limit" with "reset", or the phrases "usage limit"
    and "rate limit".
    """
    lowered = text.lower()
    if "usage limit" in lowered or "rate limit" in lowered:
        return True
    return "limit" in lowered and "reset" in lowered


def parse_reset_time(text: str, now: datetime | None = None) -> datetime | None:
    """When the limit lifts, as an aware UTC datetime, or None when the text gives no time.

    The time is read in the "(Zone/Name)" zone after it, else in local time. A time that has
    already passed today means tomorrow.
    """
    now = now or datetime.now(UTC)
    # Only text after "reset" counts, so a clock time elsewhere in the message is ignored.
    reset_position = text.lower().find("reset")
    if reset_position < 0:
        return None
    tail_text = text[reset_position:]

    meridiem_match = _CLOCK_WITH_MERIDIEM.search(tail_text)
    if meridiem_match:
        hour = int(meridiem_match.group(1))
        minute = int(meridiem_match.group(2) or 0)
        is_pm = meridiem_match.group(3).lower() == "pm"
        if not 1 <= hour <= 12 or minute > 59:
            return None
        hour = hour % 12 + (12 if is_pm else 0)
        match_end = meridiem_match.end()
    else:
        clock_match = _CLOCK_24_HOUR.search(tail_text)
        if clock_match is None:
            return None
        hour, minute = int(clock_match.group(1)), int(clock_match.group(2))
        match_end = clock_match.end()

    zone_match = _ZONE_IN_PARENTHESES.search(tail_text, match_end)
    if zone_match:
        try:
            zone = ZoneInfo(zone_match.group(1))
        except (ZoneInfoNotFoundError, ValueError):
            return None
    else:
        zone = now.astimezone().tzinfo

    now_in_zone = now.astimezone(zone)
    reset = now_in_zone.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset <= now_in_zone:
        reset = (reset.replace(tzinfo=None) + timedelta(days=1)).replace(tzinfo=zone)
    return reset.astimezone(UTC)


def _usage_counts(payload: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    """(input, cached input, output) tokens from the result's `usage`, or Nones without it.

    Anthropic's `input_tokens` leaves out both cache parts, so the total input adds them back.
    """
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None, None, None

    def count(field_name: str) -> int:
        value = usage.get(field_name)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    cache_read = count("cache_read_input_tokens")
    total_input = count("input_tokens") + count("cache_creation_input_tokens") + cache_read
    return total_input, cache_read, count("output_tokens")


class ClaudeSeat(CommandSeat):
    program = "claude"
    binary = "claude"

    def __init__(
        self,
        name: str,
        model: str | None = None,
        working_directory: Path | None = None,
        thinking_arguments: list[str] | None = None,
        access: Access = "talk",
        data_dir: Path | None = None,
        base_url: str = "",
        auth_token: str = "",
    ):
        super().__init__(name, model, working_directory, thinking_arguments, access, data_dir)
        # A custom Anthropic-compatible endpoint; empty means the owner's Max login.
        self.base_url = base_url
        self.auth_token = auth_token

    def extra_environment(self, access: Access) -> dict[str, str]:
        if not self.base_url:
            return {}
        if not self.auth_token:
            raise SeatError(self.name, f"claude needs ANTHROPIC_AUTH_TOKEN for {self.base_url}")
        return {"ANTHROPIC_BASE_URL": self.base_url, "ANTHROPIC_AUTH_TOKEN": self.auth_token}

    async def run(
        self, prompt: str, system_prompt: str, session_id: str | None, timeout_s: float
    ) -> SeatReply:
        try:
            return await super().run(prompt, system_prompt, session_id, timeout_s)
        except (SeatTimeoutError, SeatRateLimitedError):
            raise
        except SeatError as error:
            # A non-zero exit carries the limit message in its detail (stderr or stdout).
            if is_rate_limit_text(error.detail):
                raise SeatRateLimitedError(
                    self.name, error.detail, parse_reset_time(error.detail)
                ) from error
            raise

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
            message = str(payload.get("result") or "claude reported an error")
            if is_rate_limit_text(message):
                raise SeatRateLimitedError(self.name, message, parse_reset_time(message))
            raise SeatError(self.name, message)
        result_text = payload.get("result")
        if not isinstance(result_text, str):
            raise SeatError(self.name, "output has no string 'result' field")
        input_tokens, cached_input_tokens, output_tokens = _usage_counts(payload)
        return SeatReply(
            text=result_text,
            session_id=payload.get("session_id") or session_id,
            cost_usd=payload.get("total_cost_usd"),
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            raw=payload,
        )
