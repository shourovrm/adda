"""REST and WebSocket contract of the FastAPI app (SPEC section 6), with a fake seat adapter."""

from __future__ import annotations

import logging
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from adda.config import Settings
from adda.models import Message, SeatReply
from adda.personas import Persona
from adda.routers.heuristic import HeuristicRouter
from adda.server import create_app
from adda.store import Store

END_PROMPT_MARKER = "In at most 3 bullets"


class FakeAdapter:
    """Replies with a fixed sentence, or one memory bullet to the end-of-discussion question."""

    program = "claude"

    def __init__(self, name: str) -> None:
        self.name = name
        self.prompts: list[str] = []

    def available(self) -> bool:
        return True

    async def run(self, prompt, system_prompt, session_id, timeout_s) -> SeatReply:
        self.prompts.append(prompt)
        if END_PROMPT_MARKER in prompt:
            return SeatReply(text="- remember X", session_id="s1", cost_usd=0.01)
        return SeatReply(text=f"hello from {self.name}", session_id="s1", cost_usd=0.01)


def make_persona(root, name: str, program: str = "claude") -> Persona:
    directory = root / "personas" / name
    directory.mkdir(parents=True)
    return Persona(
        name=name,
        program=program,
        model="haiku" if name == "alpha" else None,
        color="#112233",
        role=f"role of {name}",
        stance="a stance",
        body=f"You are {name}.",
        directory=directory,
    )


@pytest.fixture
def static_directory(tmp_path):
    directory = tmp_path / "static"
    directory.mkdir()
    (directory / "index.html").write_text("<h1>adda test page</h1>", encoding="utf-8")
    (directory / "app.js").write_text("console.log('adda');", encoding="utf-8")
    return directory


@pytest.fixture
def adapters():
    return {"alpha": FakeAdapter("alpha"), "beta": FakeAdapter("beta")}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "adda.db")


@pytest.fixture
def client(tmp_path, static_directory, adapters, store):
    personas = {name: make_persona(tmp_path, name) for name in ("alpha", "beta")}
    settings = Settings(
        data_dir=tmp_path, personas_dir=tmp_path / "personas", router="heuristic", max_auto_turns=1
    )
    app = create_app(
        settings,
        store=store,
        personas=personas,
        adapters=adapters,
        router=HeuristicRouter(),
        static_directory=static_directory,
    )
    with TestClient(app) as test_client:
        yield test_client


def create_thread(client, title="a topic", **extra) -> dict:
    response = client.post("/api/threads", json={"title": title, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def receive_until(websocket, frame_type: str, limit: int = 20) -> list[dict]:
    """Collect frames up to and including the first one of `frame_type`."""
    frames = []
    for _ in range(limit):
        frame = websocket.receive_json()
        frames.append(frame)
        if frame["type"] == frame_type:
            return frames
    raise AssertionError(f"no {frame_type!r} frame in {frames}")


# Static files


def test_index_serves_html(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "adda test page" in response.text


def test_index_is_404_when_file_missing(tmp_path, adapters, store):
    empty_static = tmp_path / "empty-static"
    empty_static.mkdir()
    app = create_app(
        Settings(data_dir=tmp_path, personas_dir=tmp_path / "none", router="heuristic"),
        store=store,
        personas={},
        adapters={},
        router=HeuristicRouter(),
        static_directory=empty_static,
    )
    with TestClient(app) as test_client:
        assert test_client.get("/").status_code == 404


def test_static_file_is_served(client):
    response = client.get("/static/app.js")
    assert response.status_code == 200
    assert "console.log" in response.text


def test_missing_static_file_is_404(client):
    assert client.get("/static/nope.js").status_code == 404


# Seats


def test_seats_have_exactly_the_contract_fields(client):
    seats = client.get("/api/seats").json()
    assert [seat["name"] for seat in seats] == ["alpha", "beta"]
    for seat in seats:
        assert set(seat) == {"name", "program", "model", "color", "role", "available"}
    assert seats[0] == {
        "name": "alpha",
        "program": "claude",
        "model": "haiku",
        "color": "#112233",
        "role": "role of alpha",
        "available": True,
    }
    assert seats[1]["model"] is None


# Threads


def test_create_thread_defaults_to_bidding(client):
    thread = create_thread(client, "  padded title  ")
    assert thread["title"] == "padded title"
    assert thread["mode"] == "bidding"
    assert thread["ended_at"] is None
    assert len(thread["id"]) > 0


def test_create_thread_with_explicit_mode(client):
    assert create_thread(client, mode="roundrobin")["mode"] == "roundrobin"


@pytest.mark.parametrize("title", ["", "   "])
def test_create_thread_rejects_empty_title(client, title):
    assert client.post("/api/threads", json={"title": title}).status_code == 422


def test_create_thread_rejects_invalid_mode(client):
    response = client.post("/api/threads", json={"title": "x", "mode": "chaos"})
    assert response.status_code == 422


def test_create_thread_rejects_missing_title(client):
    assert client.post("/api/threads", json={}).status_code == 422


def test_list_threads_is_newest_first(client):
    first = create_thread(client, "first")
    second = create_thread(client, "second")
    third = create_thread(client, "third")
    listed = client.get("/api/threads").json()
    assert [thread["id"] for thread in listed] == [third["id"], second["id"], first["id"]]


def test_list_threads_is_empty_at_start(client):
    assert client.get("/api/threads").json() == []


def test_get_thread_returns_thread_and_messages(client, store):
    thread = create_thread(client)
    store.add_message(
        Message(thread_id=thread["id"], author="owner", role="owner", text="first words")
    )
    body = client.get(f"/api/threads/{thread['id']}").json()
    assert body["thread"] == thread
    assert [message["text"] for message in body["messages"]] == ["first words"]
    assert body["messages"][0]["role"] == "owner"


def test_get_unknown_thread_is_404(client):
    assert client.get("/api/threads/missing").status_code == 404


def test_datetimes_are_iso_strings_with_timezone(client, store):
    thread = create_thread(client)
    store.add_message(Message(thread_id=thread["id"], author="owner", role="owner", text="hi"))
    body = client.get(f"/api/threads/{thread['id']}").json()
    for value in (body["thread"]["created_at"], body["messages"][0]["created_at"]):
        assert isinstance(value, str)
        assert datetime.fromisoformat(value).tzinfo is not None


# Mode


def test_set_mode(client):
    thread = create_thread(client)
    response = client.post(f"/api/threads/{thread['id']}/mode", json={"mode": "manual"})
    assert response.status_code == 200
    assert response.json()["mode"] == "manual"
    assert client.get(f"/api/threads/{thread['id']}").json()["thread"]["mode"] == "manual"


def test_set_mode_rejects_invalid_mode(client):
    thread = create_thread(client)
    response = client.post(f"/api/threads/{thread['id']}/mode", json={"mode": "chaos"})
    assert response.status_code == 422


def test_set_mode_on_unknown_thread_is_404(client):
    assert client.post("/api/threads/missing/mode", json={"mode": "manual"}).status_code == 404


# End


def test_end_returns_notes_and_marks_thread_ended(client, store, tmp_path):
    thread = create_thread(client)
    store.add_message(
        Message(thread_id=thread["id"], author="alpha", role="seat", text="I spoke earlier")
    )
    response = client.post(f"/api/threads/{thread['id']}/end")
    assert response.status_code == 200
    body = response.json()
    assert body["notes"] == {"alpha": ["remember X"]}
    assert body["thread"]["ended_at"] is not None
    assert datetime.fromisoformat(body["thread"]["ended_at"]).tzinfo is not None
    index_text = (tmp_path / "personas" / "alpha" / "memory" / "index.md").read_text()
    assert "remember X" in index_text


def test_end_without_seat_messages_gives_empty_notes(client):
    thread = create_thread(client)
    body = client.post(f"/api/threads/{thread['id']}/end").json()
    assert body["notes"] == {}
    assert body["thread"]["ended_at"] is not None


def test_ending_twice_is_409(client):
    thread = create_thread(client)
    assert client.post(f"/api/threads/{thread['id']}/end").status_code == 200
    assert client.post(f"/api/threads/{thread['id']}/end").status_code == 409


def test_end_unknown_thread_is_404(client):
    assert client.post("/api/threads/missing/end").status_code == 404


# Search


def test_search_returns_message_title_and_snippet(client, store):
    thread = create_thread(client, "database talk")
    store.add_message(
        Message(thread_id=thread["id"], author="owner", role="owner", text="we should use sqlite")
    )
    hits = client.get("/api/search", params={"q": "sqlite"}).json()
    assert len(hits) == 1
    assert set(hits[0]) == {"message", "thread_title", "snippet"}
    assert hits[0]["thread_title"] == "database talk"
    assert hits[0]["message"]["text"] == "we should use sqlite"
    assert "sqlite" in hits[0]["snippet"]


def test_search_without_match_is_empty(client):
    assert client.get("/api/search", params={"q": "zebra"}).json() == []


def test_search_with_fts_syntax_characters_does_not_fail(client):
    assert client.get("/api/search", params={"q": 'a" OR (b:*'}).status_code == 200


# WebSocket


def test_websocket_hello_frame(client, store):
    thread = create_thread(client)
    store.add_message(Message(thread_id=thread["id"], author="owner", role="owner", text="earlier"))
    with client.websocket_connect(f"/ws/{thread['id']}") as websocket:
        hello = websocket.receive_json()
    assert hello["type"] == "hello"
    assert hello["thread"] == thread
    assert [seat["name"] for seat in hello["seats"]] == ["alpha", "beta"]
    assert [message["text"] for message in hello["messages"]] == ["earlier"]


def test_websocket_say_round_trip_with_mention(client, adapters):
    thread = create_thread(client)
    with client.websocket_connect(f"/ws/{thread['id']}") as websocket:
        assert websocket.receive_json()["type"] == "hello"
        websocket.send_json({"type": "say", "text": "hi @alpha"})
        frames = receive_until(websocket, "quiet")

    kinds = [frame["type"] for frame in frames]
    assert kinds == ["message", "typing", "typing", "message", "quiet"]
    assert frames[0]["message"]["author"] == "owner"
    assert frames[0]["message"]["mentions"] == ["alpha"]
    assert (frames[1]["seat"], frames[1]["state"]) == ("alpha", "start")
    assert (frames[2]["seat"], frames[2]["state"]) == ("alpha", "stop")
    assert frames[3]["message"]["author"] == "alpha"
    assert frames[3]["message"]["text"] == "hello from alpha"
    assert frames[3]["message"]["cost_usd"] == 0.01
    assert len(adapters["alpha"].prompts) == 1
    assert adapters["beta"].prompts == []


def test_websocket_say_persists_messages(client):
    thread = create_thread(client)
    with client.websocket_connect(f"/ws/{thread['id']}") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "say", "text": "hi @alpha"})
        receive_until(websocket, "quiet")
    messages = client.get(f"/api/threads/{thread['id']}").json()["messages"]
    assert [(message["author"], message["role"]) for message in messages] == [
        ("owner", "owner"),
        ("alpha", "seat"),
    ]


def test_websocket_frames_reach_every_client_on_the_thread(client):
    thread = create_thread(client)
    with client.websocket_connect(f"/ws/{thread['id']}") as first:
        with client.websocket_connect(f"/ws/{thread['id']}") as second:
            assert first.receive_json()["type"] == "hello"
            assert second.receive_json()["type"] == "hello"
            first.send_json({"type": "say", "text": "hi @alpha"})
            first_frames = receive_until(first, "quiet")
            second_frames = receive_until(second, "quiet")
    assert first_frames == second_frames
    assert first_frames[0]["message"]["text"] == "hi @alpha"


def test_websocket_frames_do_not_cross_threads(client):
    thread = create_thread(client, "one")
    other = create_thread(client, "two")
    with client.websocket_connect(f"/ws/{thread['id']}") as websocket:
        websocket.receive_json()
        with client.websocket_connect(f"/ws/{other['id']}") as other_websocket:
            other_websocket.receive_json()
            websocket.send_json({"type": "say", "text": "hi @alpha"})
            receive_until(websocket, "quiet")
            other_websocket.send_json({"type": "mode", "mode": "manual"})
            # The first frame the other thread sees is its own mode change, not thread one's talk.
            assert other_websocket.receive_json() == {"type": "mode", "mode": "manual"}


def test_websocket_unknown_frame_type_gives_error_frame(client):
    thread = create_thread(client)
    with client.websocket_connect(f"/ws/{thread['id']}") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "dance"})
        frame = websocket.receive_json()
    assert frame["type"] == "error"
    assert "dance" in frame["text"]


def test_websocket_non_object_frame_gives_error_frame(client):
    thread = create_thread(client)
    with client.websocket_connect(f"/ws/{thread['id']}") as websocket:
        websocket.receive_json()
        websocket.send_json(["say", "hi"])
        assert websocket.receive_json()["type"] == "error"


@pytest.mark.parametrize("text", ["", "   ", None])
def test_websocket_empty_say_gives_error_frame(client, text):
    thread = create_thread(client)
    with client.websocket_connect(f"/ws/{thread['id']}") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "say", "text": text})
        assert websocket.receive_json()["type"] == "error"
    assert client.get(f"/api/threads/{thread['id']}").json()["messages"] == []


def test_websocket_mode_frame_is_broadcast(client):
    thread = create_thread(client)
    with client.websocket_connect(f"/ws/{thread['id']}") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "mode", "mode": "roundrobin"})
        assert websocket.receive_json() == {"type": "mode", "mode": "roundrobin"}
    assert client.get(f"/api/threads/{thread['id']}").json()["thread"]["mode"] == "roundrobin"


def test_websocket_invalid_mode_frame_gives_error_frame(client):
    thread = create_thread(client)
    with client.websocket_connect(f"/ws/{thread['id']}") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "mode", "mode": "chaos"})
        assert websocket.receive_json()["type"] == "error"


def test_websocket_wake_runs_the_named_seat(client, adapters):
    thread = create_thread(client)
    with client.websocket_connect(f"/ws/{thread['id']}") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "wake", "seat": "beta"})
        frames = receive_until(websocket, "quiet")
    assert [frame["type"] for frame in frames] == ["typing", "typing", "message", "quiet"]
    assert frames[2]["message"]["author"] == "beta"
    assert len(adapters["beta"].prompts) == 1


def test_websocket_wake_unknown_seat_gives_error_frame(client):
    thread = create_thread(client)
    with client.websocket_connect(f"/ws/{thread['id']}") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "wake", "seat": "ghost"})
        assert websocket.receive_json()["type"] == "error"


def test_websocket_end_frame_broadcasts_ended(client):
    thread = create_thread(client)
    with client.websocket_connect(f"/ws/{thread['id']}") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "end"})
        frame = websocket.receive_json()
    assert frame == {"type": "ended", "notes": {}}


def test_websocket_say_after_end_gives_error_frame(client):
    thread = create_thread(client)
    client.post(f"/api/threads/{thread['id']}/end")
    with client.websocket_connect(f"/ws/{thread['id']}") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "say", "text": "too late"})
        assert websocket.receive_json()["type"] == "error"


def test_websocket_to_unknown_thread_gives_error_then_closes(client):
    with client.websocket_connect("/ws/missing") as websocket:
        assert websocket.receive_json()["type"] == "error"
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_json()
    assert closed.value.code == 4404


# App construction


def test_app_boots_without_personas_or_api_key(tmp_path, static_directory, caplog):
    settings = Settings(
        data_dir=tmp_path,
        personas_dir=tmp_path / "none",
        router="jev",
        jev_api_key=None,
    )
    with caplog.at_level(logging.WARNING):
        app = create_app(settings, static_directory=static_directory)
    with TestClient(app) as test_client:
        assert test_client.get("/api/seats").json() == []
        assert test_client.get("/").status_code == 200
    assert isinstance(app.state.room.router, HeuristicRouter)
    warnings = [record.getMessage() for record in caplog.records if record.levelno >= 30]
    assert any("heuristic" in warning for warning in warnings)
