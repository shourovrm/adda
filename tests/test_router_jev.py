import json
import logging
from pathlib import Path

import httpx
import pytest

from adda.models import Seat
from adda.personas import Persona
from adda.routers.base import RouterError
from adda.routers.jev import OPENROUTER_URL, TYPESAFE_URL, JevRouter

TEST_KEY = "test-key-123"


def make_seat(name: str, role: str = "seat role") -> Seat:
    return Seat(name=name, program="claude", color="#000000", role=role, available=True)


def make_persona(name: str, role: str, stance: str) -> Persona:
    return Persona(
        name=name,
        program="claude",
        model=None,
        color="#000000",
        role=role,
        stance=stance,
        body="body",
        directory=Path("/nonexistent"),
    )


def answers_payload(**scores: tuple[float, float]) -> dict:
    return {
        "answers": {
            name: {"score": score, "confidence": confidence}
            for name, (score, confidence) in scores.items()
        },
        "usage": {"cost": 0.00002},
    }


def make_router(provider: str, handler, api_key: str | None = TEST_KEY) -> JevRouter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JevRouter(provider, "some-model", api_key, client=client)


SEATS = [make_seat("alpha", "fallback role"), make_seat("beta")]
PERSONAS = {"alpha": make_persona("alpha", "Architecture", "Keep it small.")}


async def test_openrouter_request_shape():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=answers_payload(alpha=(7.0, 0.9), beta=(2.0, 0.8)))

    router = make_router("openrouter", handler)
    bids = await router.bids("owner: hello", SEATS, PERSONAS)

    assert captured["url"] == OPENROUTER_URL
    assert captured["authorization"] == f"Bearer {TEST_KEY}"
    body = captured["body"]
    assert set(body) == {"model", "state", "questions", "provider"}
    assert body["model"] == "some-model"
    assert body["state"] == "owner: hello"
    assert body["provider"] == {"data_collection": "deny", "zdr": True}
    assert set(body["questions"]) == {"alpha", "beta"}
    assert [(bid.seat, bid.score, bid.confidence) for bid in bids] == [
        ("alpha", 7.0, 0.9),
        ("beta", 2.0, 0.8),
    ]
    assert router.last_usage == {"cost": 0.00002}


async def test_typesafe_request_shape_has_no_provider_block():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=answers_payload(alpha=(5.0, 1.0), beta=(5.0, 1.0)))

    await make_router("typesafe", handler).bids("t", SEATS, PERSONAS)

    assert captured["url"] == TYPESAFE_URL
    assert captured["authorization"] == f"Bearer {TEST_KEY}"
    assert set(captured["body"]) == {"model", "state", "questions"}


async def test_questions_are_score_questions_with_ten_criteria_and_persona_text():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=answers_payload(alpha=(1, 1), beta=(1, 1)))

    await make_router("openrouter", handler).bids("t", SEATS, PERSONAS)

    questions = captured["body"]["questions"]
    for question in questions.values():
        assert question["type"] == "score"
        assert len(question["criteria"]) == 10
        assert "nothing to add" in question["criteria"][0]
        assert "must answer now" in question["criteria"][-1]
    assert "Architecture" in questions["alpha"]["instructions"]
    assert "Keep it small." in questions["alpha"]["instructions"]
    # beta has no persona, so the Seat.role is used.
    assert "seat role" in questions["beta"]["instructions"]


async def test_scores_and_confidence_are_clamped():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=answers_payload(alpha=(12.5, 1.4), beta=(-3.0, -0.2)))

    bids = await make_router("openrouter", handler).bids("t", SEATS, PERSONAS)

    assert (bids[0].score, bids[0].confidence) == (10.0, 1.0)
    assert (bids[1].score, bids[1].confidence) == (0.0, 0.0)


async def test_low_confidence_is_logged_but_still_used(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=answers_payload(alpha=(6.0, 0.3), beta=(2.0, 0.9)))

    with caplog.at_level(logging.WARNING, logger="adda.routers.jev"):
        bids = await make_router("openrouter", handler).bids("t", SEATS, PERSONAS)

    assert bids[0].score == 6.0
    assert "alpha" in caplog.text
    assert "beta" not in caplog.text
    assert TEST_KEY not in caplog.text


async def test_server_error_raises_router_error_without_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream exploded")

    with pytest.raises(RouterError, match="500") as error_info:
        await make_router("openrouter", handler).bids("t", SEATS, PERSONAS)
    assert TEST_KEY not in str(error_info.value)


async def test_transport_error_raises_router_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(RouterError, match="ConnectError"):
        await make_router("typesafe", handler).bids("t", SEATS, PERSONAS)


async def test_malformed_json_raises_router_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json at all")

    with pytest.raises(RouterError, match="malformed"):
        await make_router("openrouter", handler).bids("t", SEATS, PERSONAS)


async def test_missing_answer_for_a_seat_raises_router_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=answers_payload(alpha=(5.0, 1.0)))

    with pytest.raises(RouterError, match="beta"):
        await make_router("openrouter", handler).bids("t", SEATS, PERSONAS)


async def test_missing_api_key_raises_before_any_request():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent without a key")

    with pytest.raises(RouterError, match="API key"):
        await make_router("openrouter", handler, api_key=None).bids("t", SEATS, PERSONAS)
