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


def make_seat(name: str, persona: str | None = None, instruction: str | None = None) -> Seat:
    return Seat(
        id=f"id-{name}",
        room_id="room1",
        name=name,
        program="claude",
        persona=persona,
        instruction=instruction,
        handle=name,
        available=True,
    )


def make_persona(slug: str, role: str, stance: str) -> Persona:
    return Persona(
        slug=slug,
        name=slug,
        color="#000000",
        role=role,
        stance=stance,
        body="body",
        directory=Path("/nonexistent"),
    )


def answers_payload(**scores: tuple[float, float]) -> dict:
    """Answers are keyed by seat id, which the tests build as "id-<name>"."""
    return {
        "answers": {
            f"id-{name}": {"score": score, "confidence": confidence}
            for name, (score, confidence) in scores.items()
        },
        "usage": {"cost": 0.00002},
    }


def make_router(provider: str, handler, api_key: str | None = TEST_KEY) -> JevRouter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JevRouter(provider, "some-model", api_key, client=client)


SEATS = [make_seat("alpha", persona="arch"), make_seat("beta", instruction="Play the sceptic.")]
PERSONAS = {"arch": make_persona("arch", "Architecture", "Keep it small.")}


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
    assert set(body["questions"]) == {"id-alpha", "id-beta"}
    assert [(bid.seat, bid.score, bid.confidence) for bid in bids] == [
        ("id-alpha", 7.0, 0.9),
        ("id-beta", 2.0, 0.8),
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
    assert "Architecture" in questions["id-alpha"]["instructions"]
    assert "Keep it small." in questions["id-alpha"]["instructions"]
    # beta has no persona: the question says "a participant" and carries the custom instruction.
    assert "a participant" in questions["id-beta"]["instructions"]
    assert "Play the sceptic." in questions["id-beta"]["instructions"]


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
    assert "id-alpha" in caplog.text
    assert "id-beta" not in caplog.text
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

    with pytest.raises(RouterError, match="id-beta"):
        await make_router("openrouter", handler).bids("t", SEATS, PERSONAS)


async def test_missing_api_key_raises_before_any_request():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent without a key")

    with pytest.raises(RouterError, match="API key"):
        await make_router("openrouter", handler, api_key=None).bids("t", SEATS, PERSONAS)


async def test_afterthought_question_asks_for_something_new_to_add():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=answers_payload(alpha=(1, 1), beta=(1, 1)))

    await make_router("openrouter", handler).bids("t", SEATS, PERSONAS, "afterthought")

    questions = captured["body"]["questions"]
    for seat_name in ("alpha", "beta"):
        instructions = questions[f"id-{seat_name}"]["instructions"]
        assert f"Does @{seat_name} have something worth adding that nobody has said" in instructions
        assert "afterthought" in instructions
        assert "should speak next" not in instructions
        assert len(questions[f"id-{seat_name}"]["criteria"]) == 10
    assert "Architecture" in questions["id-alpha"]["instructions"]


async def test_message_question_is_the_default():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=answers_payload(alpha=(1, 1), beta=(1, 1)))

    await make_router("typesafe", handler).bids("t", SEATS, PERSONAS)
    assert "should speak next" in captured["body"]["questions"]["id-alpha"]["instructions"]
