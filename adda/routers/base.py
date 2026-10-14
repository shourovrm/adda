"""Router protocol: score every seat's urge to speak next (SPEC section 5)."""

from __future__ import annotations

from typing import Protocol

from adda.models import Bid, Seat
from adda.personas import Persona

# What the router is asked: "should this seat answer the latest message?" or, when the room has
# been quiet for a while, "does this seat have an afterthought worth adding?".
MESSAGE_QUESTION = "message"
AFTERTHOUGHT_QUESTION = "afterthought"


class RouterError(RuntimeError):
    """The router could not produce bids (HTTP failure, bad response, missing key)."""


class Router(Protocol):
    name: str  # "jev" or "heuristic", shown in the `bids` frame

    async def bids(
        self,
        transcript: str,
        seats: list[Seat],
        personas: dict[str, Persona],
        question_kind: str = MESSAGE_QUESTION,
    ) -> list[Bid]: ...
