"""Real `claude -p` round trip. Run by hand only: `uv run pytest -m integration`."""

from __future__ import annotations

import shutil

import pytest

from adda.seats.claude import ClaudeSeat

pytestmark = pytest.mark.integration

SYSTEM_PROMPT = "You are a terse test participant."


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude is not on PATH")
async def test_claude_remembers_word_across_resume(tmp_path):
    seat = ClaudeSeat("ada", model="haiku", working_directory=tmp_path)

    first = await seat.run(
        "Remember the word pineapple. Reply only OK.", SYSTEM_PROMPT, None, timeout_s=120
    )
    assert first.session_id

    second = await seat.run(
        "What word did I ask you to remember?", SYSTEM_PROMPT, first.session_id, timeout_s=120
    )
    assert "pineapple" in second.text.lower()
