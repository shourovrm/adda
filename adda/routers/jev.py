"""Jev decision router: one request scores every seat's urge to speak (SPEC section 5).

Question shape, taken from the `use-jev` skill (docs.typesafe.ai): a `score` question is
`{"type": "score", "instructions": str, "criteria": [lowest level, ..., highest level]}` with
2 to 10 ordered levels. The answer holds `score` (probability-weighted level number, 1..10),
`confidence` (0..1) and the response holds `usage`. ASSUMPTION for the main session to verify
with one real call: the `provider` block is sent only to OpenRouter, and TypeSafe's
`/v1/systemone` accepts the same question shape and answer fields as OpenRouter.
"""

from __future__ import annotations

import logging

import httpx

from adda.models import Bid, Seat
from adda.personas import Persona
from adda.routers.base import RouterError

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/alpha/decisions"
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
PROVIDER_URLS = {"openrouter": OPENROUTER_URL, "typesafe": TYPESAFE_URL}
LOW_CONFIDENCE = 0.6

# Level 1 is the lowest, level 10 the highest; Jev returns a level number in 1..10.
SCORE_CRITERIA = [
    "1: nothing to add",
    "2: a remark would be redundant",
    "3: a minor aside at best",
    "4: could add a small detail",
    "5: has a relevant point, but others may cover it",
    "6: has a useful point worth hearing now",
    "7: has a clearly valuable point",
    "8: should answer soon (asked, or a mistake it can fix)",
    "9: very likely needed (directly relevant question or an error to correct)",
    "10: must answer now (addressed, corrected, or has the decisive point)",
]


def _question_for_seat(seat: Seat, personas: dict[str, Persona]) -> dict:
    """`personas` is the library by slug; the seat points at one with `seat.persona`."""
    persona = personas.get(seat.persona) if seat.persona else None
    role = persona.role if persona and persona.role else "a participant"
    stance = persona.stance if persona else ""
    instructions = (
        f"The state is a chat transcript. Rate how strongly the participant @{seat.handle} "
        "should speak next, given the last message.\n"
        f"Role of @{seat.handle}: {role}"
    )
    if stance:
        instructions += f"\nStance of @{seat.handle}: {stance}"
    if seat.instruction and seat.instruction.strip():
        instructions += f"\nInstruction given to @{seat.handle}: {seat.instruction.strip()[:300]}"
    return {"type": "score", "instructions": instructions, "criteria": SCORE_CRITERIA}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class JevRouter:
    name = "jev"

    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str | None,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 20.0,
    ) -> None:
        if provider not in PROVIDER_URLS:
            raise ValueError(f"provider must be one of {sorted(PROVIDER_URLS)}, got {provider!r}")
        self.provider = provider
        self.model = model
        self._api_key = api_key
        self._client = client
        self._timeout_s = timeout_s
        self.last_usage: dict = {}

    def __repr__(self) -> str:
        # The key is deliberately left out.
        return f"JevRouter(provider={self.provider!r}, model={self.model!r})"

    def _request_body(self, transcript: str, seats: list[Seat], personas: dict[str, Persona]):
        body: dict = {
            "model": self.model,
            "state": transcript,
            "questions": {seat.id: _question_for_seat(seat, personas) for seat in seats},
        }
        if self.provider == "openrouter":
            body["provider"] = {"data_collection": "deny", "zdr": True}
        return body

    async def _post(self, body: dict) -> httpx.Response:
        url = PROVIDER_URLS[self.provider]
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            if self._client is not None:
                return await self._client.post(
                    url, json=body, headers=headers, timeout=self._timeout_s
                )
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                return await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as error:
            raise RouterError(
                f"jev ({self.provider}) request failed: {type(error).__name__}: {error}"
            ) from error

    async def bids(
        self, transcript: str, seats: list[Seat], personas: dict[str, Persona]
    ) -> list[Bid]:
        if not self._api_key:
            raise RouterError(f"jev ({self.provider}) has no API key configured")
        if not seats:
            return []

        response = await self._post(self._request_body(transcript, seats, personas))
        if not response.is_success:
            body_start = response.text[:300]
            raise RouterError(
                f"jev ({self.provider}) returned HTTP {response.status_code}: {body_start}"
            )
        try:
            payload = response.json()
            answers = payload["answers"]
        except (ValueError, KeyError, TypeError) as error:
            raise RouterError(
                f"jev ({self.provider}) returned a malformed body: {error!r}"
            ) from error
        usage = payload.get("usage") if isinstance(payload, dict) else None
        self.last_usage = usage if isinstance(usage, dict) else {}

        return [self._bid_from_answer(seat.id, answers) for seat in seats]

    def _bid_from_answer(self, seat_id: str, answers: object) -> Bid:
        try:
            answer = answers[seat_id]
            score = float(answer["score"])
            confidence = float(answer["confidence"])
        except (KeyError, TypeError, ValueError) as error:
            raise RouterError(
                f"jev ({self.provider}) answer for seat {seat_id!r} is missing or malformed:"
                f" {error!r}"
            ) from error
        if confidence < LOW_CONFIDENCE:
            logger.warning("jev low confidence %.2f for seat %s", confidence, seat_id)
        return Bid(
            seat=seat_id,
            score=_clamp(score, 0.0, 10.0),
            confidence=_clamp(confidence, 0.0, 1.0),
        )
