"""Router protocol: score every seat's urge to speak next (SPEC section 5)."""

from __future__ import annotations

from typing import Protocol

from adda.models import Bid, Seat
from adda.personas import Persona


class RouterError(RuntimeError):
    """The router could not produce bids (HTTP failure, bad response, missing key)."""


class Router(Protocol):
    name: str  # "jev" or "heuristic", shown in the `bids` frame

    async def bids(
        self, transcript: str, seats: list[Seat], personas: dict[str, Persona]
    ) -> list[Bid]: ...
