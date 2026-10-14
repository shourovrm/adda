"""No-network fallback router: mentioned seats score 10, else the longest-silent seat scores 6."""

from __future__ import annotations

from adda.models import Bid, Seat
from adda.personas import Persona
from adda.prompts import find_mentions

MENTIONED_SCORE = 10.0
LONGEST_SILENT_SCORE = 6.0


def _author_of(line: str) -> str:
    author, separator, _ = line.partition(":")
    return author.strip() if separator else ""


class HeuristicRouter:
    name = "heuristic"

    async def bids(
        self, transcript: str, seats: list[Seat], personas: dict[str, Persona]
    ) -> list[Bid]:
        seat_names = [seat.name for seat in seats]
        lines = [line for line in transcript.splitlines() if line.strip()]
        last_line = lines[-1] if lines else ""
        mentioned = find_mentions(last_line, seat_names)
        if mentioned:
            return [
                Bid(seat=name, score=MENTIONED_SCORE if name in mentioned else 0.0)
                for name in seat_names
            ]

        # A seat that never spoke in the window counts as silent since before the first line.
        last_spoke_at = {name: -1 for name in seat_names}
        for line_number, line in enumerate(lines):
            author = _author_of(line)
            if author in last_spoke_at:
                last_spoke_at[author] = line_number
        if not seat_names:
            return []
        longest_silent = min(seat_names, key=lambda name: last_spoke_at[name])
        return [
            Bid(seat=name, score=LONGEST_SILENT_SCORE if name == longest_silent else 0.0)
            for name in seat_names
        ]
