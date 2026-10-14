"""Subprocess runner behaviour, with asyncio.create_subprocess_exec replaced by a fake."""

from __future__ import annotations

import asyncio

import pytest

from adda.seats import base
from adda.seats.base import SeatError, SeatTimeoutError, is_pass, run_command


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, hang=False):
        self._stdout = stdout
        self._stderr = stderr
        self._exit_code = returncode
        self._hang = hang
        self.returncode = None if hang else returncode
        self.kill_calls = 0

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(3600)
        return self._stdout, self._stderr

    async def wait(self):
        return self.returncode

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9


@pytest.fixture
def fake_spawn(monkeypatch):
    """Install a fake spawner; returns a dict with 'calls' and the 'process' to hand out."""
    state = {"calls": [], "process": FakeProcess(stdout=b"hello")}

    async def fake_create_subprocess_exec(*argv, **kwargs):
        state["calls"].append((argv, kwargs))
        return state["process"]

    monkeypatch.setattr(base.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    return state


async def test_prompt_is_one_argv_element_and_no_shell(fake_spawn):
    prompt = "rm -rf / ; echo $HOME"
    output = await run_command("seat", ["tool", "-p", prompt], timeout_s=5)

    assert output == "hello"
    argv, kwargs = fake_spawn["calls"][0]
    assert argv == ("tool", "-p", prompt)
    assert "shell" not in kwargs


async def test_api_key_is_stripped_and_other_variables_survive(fake_spawn, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-not-a-key")
    monkeypatch.setenv("ADDA_TEST_MARKER", "kept")

    await run_command("seat", ["tool"], timeout_s=5)

    environment = fake_spawn["calls"][0][1]["env"]
    assert "ANTHROPIC_API_KEY" not in environment
    assert environment["ADDA_TEST_MARKER"] == "kept"


async def test_nonzero_exit_raises_with_stderr_tail(fake_spawn):
    fake_spawn["process"] = FakeProcess(stderr=b"boom: bad flag", returncode=2)

    with pytest.raises(SeatError) as caught:
        await run_command("seat", ["tool"], timeout_s=5)

    assert "boom: bad flag" in str(caught.value)
    assert "exit code 2" in str(caught.value)


async def test_timeout_kills_process(fake_spawn):
    process = FakeProcess(hang=True)
    fake_spawn["process"] = process

    with pytest.raises(SeatTimeoutError):
        await run_command("seat", ["tool"], timeout_s=0.05)

    assert process.kill_calls == 1


async def test_cancellation_kills_process_and_propagates(fake_spawn):
    process = FakeProcess(hang=True)
    fake_spawn["process"] = process

    task = asyncio.create_task(run_command("seat", ["tool"], timeout_s=60))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.kill_calls == 1


def test_is_pass():
    assert is_pass("  PASS \n") is True
    assert is_pass("PASS.") is False
    assert is_pass("pass") is False
