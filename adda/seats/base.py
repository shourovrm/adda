"""SeatAdapter protocol and the subprocess runner every adapter shares (SPEC section 4)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Protocol

from adda.models import SeatReply

# Headless `claude -p` prefers this variable over the owner's Max login and would bill the API.
BILLING_VARIABLES_TO_STRIP = ("ANTHROPIC_API_KEY",)
STDERR_TAIL_CHARACTERS = 2000
PASS_REPLY = "PASS"


class SeatError(RuntimeError):
    """A seat run failed: non-zero exit, unparseable output, or a timeout."""

    def __init__(self, seat: str, detail: str) -> None:
        super().__init__(f"seat {seat} failed: {detail}")
        self.seat = seat
        self.detail = detail


class SeatTimeoutError(SeatError):
    pass


class SeatAdapter(Protocol):
    name: str
    program: str

    async def run(
        self, prompt: str, system_prompt: str, session_id: str | None, timeout_s: float
    ) -> SeatReply: ...

    def available(self) -> bool: ...


def is_pass(reply_text: str) -> bool:
    """True when the seat chose not to speak: the trimmed reply is exactly PASS."""
    return reply_text.strip() == PASS_REPLY


def seat_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for variable in BILLING_VARIABLES_TO_STRIP:
        environment.pop(variable, None)
    return environment


def tail(text: str, characters: int = STDERR_TAIL_CHARACTERS) -> str:
    return text[-characters:].strip()


def parse_json_lines(seat: str, stdout: str) -> list[dict[str, Any]]:
    """Parse JSONL output; non-JSON lines (progress noise) are skipped, none at all is an error."""
    events: list[dict[str, Any]] = []
    # Split on LF only: str.splitlines() also breaks on U+2028/U+2029, which are legal inside
    # JSON strings (pi's JSON mode docs warn about exactly this).
    for line in stdout.split("\n"):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    if not events:
        raise SeatError(seat, f"no JSON lines in output: {stdout[:200]!r}")
    return events


async def run_command(
    seat: str, command: list[str], timeout_s: float, working_directory: Path | None = None
) -> str:
    """Run one seat command without a shell and return its stdout.

    Raises SeatTimeoutError after `timeout_s`, SeatError on a non-zero exit. On cancellation
    (the owner interrupted) the child is killed before CancelledError propagates.
    """
    if working_directory is not None:
        working_directory.mkdir(parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=seat_environment(),
        cwd=None if working_directory is None else str(working_directory),
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout_s)
    except TimeoutError as error:
        await _kill(process)
        raise SeatTimeoutError(seat, f"no reply within {timeout_s:g} s, process killed") from error
    except asyncio.CancelledError:
        await _kill(process)
        raise

    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    if process.returncode != 0:
        detail = describe_failure(stdout_text, stderr_text)
        raise SeatError(seat, f"exit code {process.returncode}: {detail}")
    return stdout_text


def describe_failure(stdout_text: str, stderr_text: str) -> str:
    """Pick the shortest useful explanation of a failed run.

    Tools that emit JSON (opencode, pi) put the real reason inside an error object on stdout
    and exit non-zero; the raw line is hundreds of characters of headers. Prefer that nested
    message, then the stderr tail, then the stdout tail.
    """
    for line in reversed(stdout_text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = _nested_error_message(event)
        if message:
            return message
    return tail(stderr_text) or tail(stdout_text) or "(no output)"


def _nested_error_message(event: object) -> str | None:
    """Find a human message in shapes like {"error": {"data": {"message": ...}}}."""
    if not isinstance(event, dict):
        return None
    error = event.get("error", event)
    if isinstance(error, str):
        return error
    if not isinstance(error, dict):
        return None
    for container in (error.get("data"), error):
        if isinstance(container, dict):
            for key in ("message", "errorMessage"):
                value = container.get(key)
                if isinstance(value, str) and value:
                    return value
    return None


async def _kill(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            # The child exited between the check and the kill; nothing left to stop.
            pass
    await process.wait()


class CommandSeat(ABC):
    """An adapter that runs one CLI invocation per turn and parses its stdout."""

    program: str
    binary: str

    def __init__(self, name: str, model: str | None = None, working_directory: Path | None = None):
        self.name = name
        self.model = model
        self.working_directory = working_directory

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    @abstractmethod
    def build_command(
        self, prompt: str, system_prompt: str, session_id: str | None
    ) -> list[str]: ...

    @abstractmethod
    def parse_output(self, stdout: str, session_id: str | None) -> SeatReply:
        """Parse stdout into a reply; raise SeatError if it cannot be understood."""

    async def run(
        self, prompt: str, system_prompt: str, session_id: str | None, timeout_s: float
    ) -> SeatReply:
        command = self.build_command(prompt, system_prompt, session_id)
        stdout = await run_command(self.name, command, timeout_s, self.working_directory)
        return self.parse_output(stdout, session_id)
