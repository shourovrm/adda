"""No-network fallback router: mentioned seats score 10, else the longest-silent seat scores 6.

Bids are keyed by seat id; the transcript names seats by display name.
"""

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
        if not seats:
            return []
        lines = [line for line in transcript.splitlines() if line.strip()]
        last_line = lines[-1] if lines else ""
        mentioned = find_mentions(last_line.partition(":")[2], seats)
        if mentioned:
            return [
                Bid(seat=seat.id, score=MENTIONED_SCORE if seat.id in mentioned else 0.0)
                for seat in seats
            ]

        # A seat that never spoke in the window counts as silent since before the first line.
        last_spoke_at = {seat.id: -1 for seat in seats}
        seat_id_by_name = {seat.name: seat.id for seat in seats}
        for line_number, line in enumerate(lines):
            seat_id = seat_id_by_name.get(_author_of(line))
            if seat_id is not None:
                last_spoke_at[seat_id] = line_number
        longest_silent = min(seats, key=lambda seat: last_spoke_at[seat.id])
        return [
            Bid(seat=seat.id, score=LONGEST_SILENT_SCORE if seat is longest_silent else 0.0)
            for seat in seats
        ]
