"""Shared test doubles: a hand-advanced clock, scripted seat adapters and routers."""

from __future__ import annotations

import asyncio
import dataclasses
import shutil
from collections import deque
from pathlib import Path

from adda.config import Settings
from adda.models import Bid, SeatReply

FIXTURE_PERSONAS = Path(__file__).parent / "fixtures" / "personas"


class FakeClock:
    """A clock the test advances by hand, so cooldowns and latencies are exact."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSleeper:
    """Stands in for asyncio.sleep in the scheduler: records every delay it was asked for.

    By default a sleep returns at once (after yielding to the event loop once). With
    `hold=True` it blocks until `release()` so a test can look at the room while it waits.
    """

    def __init__(self, hold: bool = False) -> None:
        self.hold = hold
        self.delays: list[float] = []
        self._release = asyncio.Event()

    def release(self) -> None:
        self._release.set()

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        if self.hold and not self._release.is_set():
            await self._release.wait()
        else:
            await asyncio.sleep(0)


class FakeAdapter:
    """Returns scripted replies (SeatReply or an exception to raise) and records every call."""

    def __init__(self, name: str, program: str, clock: FakeClock, available: bool = True) -> None:
        self.name = name
        self.program = program
        self.is_available = available
        self.clock = clock
        self.scripted: deque[SeatReply | Exception] = deque()
        self.default_text = f"default reply from {name}"
        self.calls: list[dict] = []
        self.seconds_per_run = 0.0
        # When set, run() waits on this event so a test can interrupt a seat mid-turn.
        self.block_until: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.was_cancelled = False

    def available(self) -> bool:
        return self.is_available

    def script(self, *replies: SeatReply | Exception | str) -> None:
        for reply in replies:
            self.scripted.append(SeatReply(text=reply) if isinstance(reply, str) else reply)

    async def run(
        self, prompt: str, system_prompt: str, session_id: str | None, timeout_s: float
    ) -> SeatReply:
        self.calls.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "session_id": session_id,
                "timeout_s": timeout_s,
            }
        )
        self.started.set()
        if self.block_until is not None:
            try:
                await self.block_until.wait()
            except asyncio.CancelledError:
                self.was_cancelled = True
                raise
        self.clock.advance(self.seconds_per_run)
        if not self.scripted:
            return SeatReply(text=self.default_text)
        reply = self.scripted.popleft()
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeAdapterFactory:
    """Stands in for registry.build_adapter: one FakeAdapter per build, remembered by seat name.

    A rebuild (program, model or thinking changed) replaces the adapter under the same name and
    is counted in `builds`. Replies scripted on the old adapter do not carry over.
    """

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.by_name: dict[str, FakeAdapter] = {}
        self.builds: list[tuple[str, str, str | None, str]] = []
        self.unavailable_names: set[str] = set()

    def __call__(self, config, harness_settings, working_directory) -> FakeAdapter:
        adapter = FakeAdapter(
            config.name,
            config.program,
            self.clock,
            available=config.name not in self.unavailable_names,
        )
        previous = self.by_name.get(config.name)
        if previous is not None:
            adapter.scripted = previous.scripted
        adapter.working_directory = working_directory
        adapter.model = config.model
        adapter.thinking = config.thinking
        self.by_name[config.name] = adapter
        self.builds.append((config.name, config.program, config.model, config.thinking))
        return adapter

    def build_count(self, name: str) -> int:
        return sum(1 for build in self.builds if build[0] == name)


class FakeRouter:
    """Returns one scripted {seat name: score} per call; once the script is empty all score 0."""

    def __init__(self, name: str = "jev") -> None:
        self.name = name
        self.scripted: deque[dict[str, float] | Exception] = deque()
        self.transcripts: list[str] = []
        self.question_kinds: list[str] = []

    def script(self, *entries: dict[str, float] | Exception) -> None:
        self.scripted.extend(entries)

    @property
    def call_count(self) -> int:
        return len(self.transcripts)

    async def bids(self, transcript, seats, personas, question_kind="message"):
        self.transcripts.append(transcript)
        self.question_kinds.append(question_kind)
        entry = self.scripted.popleft() if self.scripted else {}
        if isinstance(entry, Exception):
            raise entry
        return [Bid(seat=seat.id, score=entry.get(seat.name, 0.0)) for seat in seats]


def make_settings(tmp_path: Path, **overrides) -> Settings:
    """Settings that touch only tmp_path; personas are a writable copy of the fixtures."""
    personas_dir = tmp_path / "personas"
    if not personas_dir.exists():
        shutil.copytree(FIXTURE_PERSONAS, personas_dir)
    defaults = {
        "data_dir": tmp_path / "data",
        "rooms_dir": tmp_path / "rooms",
        "personas_dir": personas_dir,
        "owner_name": "Riad",
        "owner_line": "Owner of Adda. Prefers short answers.",
        "cooldown_s": 0.0,
        "router_kind": "heuristic",
    }
    defaults.update(overrides)
    return dataclasses.replace(Settings(), **defaults)
