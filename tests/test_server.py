"""REST and WebSocket contract of the FastAPI app (SPEC section 6), with fake seat adapters."""

from __future__ import annotations

import datetime
import os
import stat
import tomllib

import pytest
from fastapi.testclient import TestClient

from adda.config import load_settings
from adda.models import Harness, SeatReply
from adda.routers.heuristic import HeuristicRouter
from adda.server import create_app
from adda.store import Store
from tests.fakes import FakeAdapterFactory, FakeClock, make_settings

TODAY = datetime.date(2026, 10, 14)
SECRET_KEY = "sk-test-secret-value-1234"


@pytest.fixture
def environment(tmp_path):
    make_settings(tmp_path)  # copies the fixture personas into tmp_path/personas
    return {
        "ADDA_CONFIG": str(tmp_path / "config" / "config.toml"),
        "ADDA_KEYS_FILE": str(tmp_path / "config" / "keys.env"),
        "ADDA_JEV_KEY_FILE": str(tmp_path / "config" / "no-legacy.env"),
        "ADDA_DATA_DIR": str(tmp_path / "data"),
        "ADDA_ROOMS_DIR": str(tmp_path / "rooms"),
        "ADDA_PERSONAS_DIR": str(tmp_path / "personas"),
        "ADDA_ROUTER": "heuristic",
        "ADDA_COOLDOWN_S": "0",
    }


@pytest.fixture
def static_directory(tmp_path):
    directory = tmp_path / "static"
    directory.mkdir()
    for file_name, marker in (
        ("index.html", "rooms page"),
        ("room.html", "room page"),
        ("settings.html", "settings page"),
    ):
        (directory / file_name).write_text(f"<h1>{marker}</h1>", encoding="utf-8")
    (directory / "common.js").write_text("console.log('adda');", encoding="utf-8")
    return directory


@pytest.fixture
def factory():
    return FakeAdapterFactory(FakeClock())


def fake_harnesses(settings) -> list[Harness]:
    return [
        Harness(program="claude", found=True, path="/bin/claude", version="2.1.299"),
        Harness(program="opencode", found=True, path="/bin/opencode", version="1.17.7"),
        Harness(program="pi", found=False),
        Harness(program="codex", found=False),
    ]


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "adda.db")


@pytest.fixture
def opened_folders() -> list:
    """Folders the app asked the (fake) file manager to open."""
    return []


@pytest.fixture
def client(environment, static_directory, factory, store, tmp_path, opened_folders):
    app = create_app(
        load_settings(environment),
        store=store,
        router=HeuristicRouter(),
        adapter_builder=factory,
        detect=fake_harnesses,
        environment=environment,
        static_directory=static_directory,
        home=tmp_path,
        today=lambda: TODAY,
        open_folder=opened_folders.append,
    )
    with TestClient(app) as test_client:
        yield test_client


def make_room(client, title="a topic", seats=None, **extra) -> dict:
    if seats is None:
        seats = [
            {"name": "Ravi", "program": "claude", "persona": "alpha"},
            {"name": "Tuli", "program": "pi", "persona": "beta"},
            {"name": "Pi", "program": "opencode"},
        ]
    body = {"title": title, "seats": seats, "liveliness": 0, **extra}
    response = client.post("/api/rooms", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def seat_id(room: dict, name: str) -> str:
    return next(seat["id"] for seat in room["seats"] if seat["name"] == name)


def receive_until(websocket, frame_type: str, limit: int = 30) -> list[dict]:
    """Collect frames up to and including the first one of `frame_type`."""
    frames = []
    for _ in range(limit):
        frame = websocket.receive_json()
        frames.append(frame)
        if frame["type"] == frame_type:
            return frames
    raise AssertionError(f"no {frame_type!r} frame in {frames}")


# Pages ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "marker"),
    [("/", "rooms page"), ("/room/abc", "room page"), ("/settings", "settings page")],
)
def test_pages_are_served(client, path, marker):
    response = client.get(path)
    assert response.status_code == 200
    assert marker in response.text


def test_static_files_are_served(client):
    assert "adda" in client.get("/static/common.js").text


def test_page_is_404_when_file_is_missing(environment, tmp_path, factory, store):
    app = create_app(
        load_settings(environment),
        store=store,
        router=HeuristicRouter(),
        adapter_builder=factory,
        detect=fake_harnesses,
        environment=environment,
        static_directory=tmp_path / "empty-static",
    )
    with TestClient(app) as bare_client:
        response = bare_client.get("/settings")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


# Library ----------------------------------------------------------------------------------


def test_harnesses_come_from_detection(client):
    harnesses = client.get("/api/harnesses").json()
    assert [(h["program"], h["found"]) for h in harnesses] == [
        ("claude", True),
        ("opencode", True),
        ("pi", False),
        ("codex", False),
    ]


def test_personas_list_the_library(client):
    personas = {persona["slug"]: persona for persona in client.get("/api/personas").json()}
    assert set(personas) == {"alpha", "beta"}
    assert personas["alpha"]["note_count"] == 1
    assert personas["alpha"]["role"] == "Architecture and trade-offs"


# Rooms ------------------------------------------------------------------------------------


def test_starts_with_no_rooms(client):
    assert client.get("/api/rooms").json() == []
    assert client.get("/api/rooms/last-seats").json() == []


def test_create_room_expands_copies_and_creates_the_default_folder(client, tmp_path):
    room = make_room(
        client,
        "Design: the Queue!",
        seats=[
            {
                "name": "Scout",
                "program": "claude",
                "copies": 3,
                "persona": "alpha",
                "instruction": "Look for gaps.",
            },
            {"name": "Ravi", "program": "pi"},
        ],
    )
    assert [seat["name"] for seat in room["seats"]] == ["Scout1", "Scout2", "Scout3", "Ravi"]
    scouts = room["seats"][:3]
    assert {seat["persona"] for seat in scouts} == {"alpha"}
    assert {seat["instruction"] for seat in scouts} == {"Look for gaps."}
    assert {seat["color"] for seat in scouts} == {"#D9480F"}  # the persona's colour
    assert [seat["position"] for seat in room["seats"]] == [0, 1, 2, 3]

    expected_folder = tmp_path / "rooms" / f"{TODAY:%Y%m%d}-design-the-queue"
    assert room["folder"] == str(expected_folder)
    assert expected_folder.is_dir()
    assert (room["mode"], room["liveliness"], room["budget_usd"]) == ("bidding", 0, 1.0)
    assert room["max_concurrent"] == 2  # settings default


def test_create_room_uses_and_creates_an_explicit_folder(client, tmp_path):
    folder = tmp_path / "elsewhere" / "deep" / "room"
    room = make_room(client, folder=str(folder), budget_usd=0, mode="manual")
    assert room["folder"] == str(folder)
    assert folder.is_dir()
    assert (room["mode"], room["budget_usd"]) == ("manual", 0)


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"title": "  "}, "title"),
        ({"title": "x", "folder": "relative/path"}, "absolute"),
        ({"title": "x", "mode": "chaos"}, "mode"),
        ({"title": "x", "liveliness": 11}, "liveliness"),
        ({"title": "x", "max_concurrent": 0}, "max_concurrent"),
        ({"title": "x", "max_concurrent": 5}, "max_concurrent"),
        ({"title": "x", "access": "root"}, "access"),
        ({"title": "x", "seats": [{"name": "A", "program": "pi", "access": "root"}]}, "access"),
        ({"title": "x", "seats": [{"name": "A", "program": "emacs"}]}, "program"),
        ({"title": "x", "seats": [{"name": "A", "program": "pi", "persona": "nobody"}]}, "nobody"),
        ({"title": "x", "seats": [{"name": "A", "program": "pi", "copies": 0}]}, "copies"),
        ({"title": "x", "seats": [{"name": "", "program": "pi"}]}, "name"),
        (
            {
                "title": "x",
                "seats": [{"name": "Dup", "program": "pi"}, {"name": "dup", "program": "pi"}],
            },
            "already exists",
        ),
    ],
)
def test_invalid_room_creation_is_a_400_and_leaves_nothing_behind(client, body, fragment):
    response = client.post("/api/rooms", json=body)
    assert response.status_code == 400, response.text
    assert fragment in response.json()["detail"]
    assert client.get("/api/rooms").json() == []


def test_rooms_list_newest_first_with_live_seat_facts(client):
    client.get("/api/harnesses")  # fills the version cache
    first = make_room(client, "first")
    second = make_room(client, "second")
    rooms = client.get("/api/rooms").json()
    assert [room["id"] for room in rooms] == [second["id"], first["id"]]
    ravi = rooms[0]["seats"][0]
    assert (ravi["available"], ravi["harness_version"], ravi["handle"]) == (True, "2.1.299", "ravi")


def test_last_seats_returns_new_seat_shaped_dicts(client):
    make_room(client, "older", seats=[{"name": "Old", "program": "pi"}])
    make_room(
        client,
        "newest",
        seats=[
            {
                "name": "Ravi",
                "program": "claude",
                "model": "haiku",
                "thinking": "low",
                "persona": "alpha",
                "instruction": "Be brief.",
            }
        ],
    )
    assert client.get("/api/rooms/last-seats").json() == [
        {
            "name": "Ravi",
            "program": "claude",
            "model": "haiku",
            "thinking": "low",
            "persona": "alpha",
            "instruction": "Be brief.",
            "color": "#D9480F",
            "eagerness": 5,
            "access": None,
        }
    ]


def test_get_room_returns_room_and_messages(client):
    room = make_room(client)
    body = client.get(f"/api/rooms/{room['id']}").json()
    assert body["room"]["id"] == room["id"]
    assert body["messages"] == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/rooms/nope"),
        ("patch", "/api/rooms/nope"),
        ("post", "/api/rooms/nope/end"),
        ("delete", "/api/rooms/nope/seats/x"),
    ],
)
def test_unknown_room_is_404_with_detail(client, method, path):
    kwargs = {"json": {}} if method == "patch" else {}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 404
    assert "nope" in response.json()["detail"]


def test_patch_room(client):
    room = make_room(client)
    response = client.patch(
        f"/api/rooms/{room['id']}", json={"title": "Renamed", "liveliness": 8, "budget_usd": 3}
    )
    assert response.status_code == 200
    patched = response.json()
    assert (patched["title"], patched["liveliness"], patched["budget_usd"]) == ("Renamed", 8, 3.0)
    for bad_patch in (
        {"liveliness": 11},
        {"mode": "chaos"},
        {"folder": "/x"},
        {"max_concurrent": 0},
        {"max_concurrent": 5},
        {"max_concurrent": 1.5},
    ):
        assert client.patch(f"/api/rooms/{room['id']}", json=bad_patch).status_code == 400


def test_max_concurrent_is_set_on_create_and_patched(client):
    room = make_room(client, max_concurrent=3)
    assert room["max_concurrent"] == 3
    response = client.patch(f"/api/rooms/{room['id']}", json={"max_concurrent": 1})
    assert response.status_code == 200
    assert response.json()["max_concurrent"] == 1
    assert client.get(f"/api/rooms/{room['id']}").json()["room"]["max_concurrent"] == 1


def test_seat_add_patch_and_delete(client):
    room = make_room(client, seats=[{"name": "Ravi", "program": "claude"}])
    room_id = room["id"]

    added = client.post(
        f"/api/rooms/{room_id}/seats", json={"name": "Scout", "program": "pi", "copies": 2}
    )
    assert added.status_code == 200
    assert [seat["name"] for seat in added.json()["seats"]] == ["Ravi", "Scout1", "Scout2"]
    assert (
        client.post(
            f"/api/rooms/{room_id}/seats", json={"name": "ravi", "program": "pi"}
        ).status_code
        == 400
    )

    scout_id = seat_id(added.json(), "Scout1")
    patched = client.patch(
        f"/api/rooms/{room_id}/seats/{scout_id}",
        json={"name": "Scribe", "thinking": "high", "enabled": False, "color": "#123456"},
    )
    assert patched.status_code == 200
    scribe = next(seat for seat in patched.json()["seats"] if seat["id"] == scout_id)
    assert (scribe["name"], scribe["thinking"], scribe["enabled"]) == ("Scribe", "high", False)
    assert (
        client.patch(
            f"/api/rooms/{room_id}/seats/{scout_id}", json={"persona": "nobody"}
        ).status_code
        == 400
    )
    assert (
        client.patch(f"/api/rooms/{room_id}/seats/nope", json={"color": "#000"}).status_code == 404
    )

    deleted = client.delete(f"/api/rooms/{room_id}/seats/{scout_id}")
    assert [seat["name"] for seat in deleted.json()["seats"]] == ["Ravi", "Scout2"]
    assert client.delete(f"/api/rooms/{room_id}/seats/{scout_id}").status_code == 404


def test_end_writes_notes_only_for_seats_with_personas_and_second_end_is_409(client, factory):
    room = make_room(client)
    room_id = room["id"]
    with client.websocket_connect(f"/ws/{room_id}") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "say", "text": "@ravi @pi hello"})
        receive_until(websocket, "quiet")

    response = client.post(f"/api/rooms/{room_id}/end")
    assert response.status_code == 200
    body = response.json()
    assert body["room"]["ended_at"] is not None
    assert list(body["notes"]) == [seat_id(room, "Ravi")]  # Pi has no persona
    assert len(factory.by_name["Pi"].calls) == 1
    assert client.post(f"/api/rooms/{room_id}/end").status_code == 409


def test_search_finds_messages_with_room_title(client):
    room = make_room(client, "storage choices")
    with client.websocket_connect(f"/ws/{room['id']}") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "say", "text": "prefer sqlite always"})
        receive_until(websocket, "quiet")
    hits = client.get("/api/search", params={"q": "sqlite"}).json()
    assert len(hits) == 1
    assert hits[0]["room_title"] == "storage choices"
    assert hits[0]["message"]["text"] == "prefer sqlite always"
    assert "sqlite" in hits[0]["snippet"]
    assert client.get("/api/search", params={"q": ""}).json() == []


def test_removed_v01_routes_are_gone(client):
    assert client.get("/api/threads").status_code == 404
    assert client.get("/api/seats").status_code == 404


# Settings ---------------------------------------------------------------------------------


def test_settings_view_masks_keys(client):
    view = client.get("/api/settings").json()
    assert view["keys"] == {"openrouter": False, "typesafe": False, "deepseek": False}
    assert view["owner"]["name"] == "Owner"
    assert "router.kind" in view["env_overrides"]


def test_settings_put_round_trip_with_key_masking(client, environment, store):
    view = client.get("/api/settings").json()
    view["owner"] = {"name": "Riad", "line": "Likes short answers."}
    view["room_defaults"]["liveliness"] = 9
    view["room_defaults"].update(
        max_concurrent=3,
        self_reply_penalty=2.0,
        more_limit=1,
        afterthought_limit=4,
        afterthought_scale=0.5,
    )
    view["harness"]["claude"]["model"] = "opus"
    view["harness"]["claude"]["extra_models"] = ["opus-next"]
    view["keys"] = {"openrouter": SECRET_KEY, "typesafe": False, "deepseek": ""}

    response = client.put("/api/settings", json=view)
    assert response.status_code == 200, response.text
    assert SECRET_KEY not in response.text
    saved = response.json()
    assert saved["owner"] == {"name": "Riad", "line": "Likes short answers."}
    assert saved["keys"] == {"openrouter": True, "typesafe": False, "deepseek": False}

    assert SECRET_KEY not in client.get("/api/settings").text
    assert client.get("/api/settings").json() == saved

    config = tomllib.loads(open(environment["ADDA_CONFIG"], encoding="utf-8").read())
    assert config["owner"]["name"] == "Riad"
    assert config["room_defaults"]["liveliness"] == 9
    assert config["room_defaults"]["max_concurrent"] == 3
    assert config["room_defaults"]["afterthought_scale"] == 0.5
    assert config["harness"]["claude"]["model"] == "opus"
    assert SECRET_KEY not in open(environment["ADDA_CONFIG"], encoding="utf-8").read()

    keys_path = environment["ADDA_KEYS_FILE"]
    assert f"OPENROUTER_API_KEY={SECRET_KEY}" in open(keys_path, encoding="utf-8").read()
    assert stat.S_IMODE(os.stat(keys_path).st_mode) == 0o600

    # The new owner name reaches a room opened afterwards, and new rooms use the new defaults.
    room = client.post("/api/rooms", json={"title": "after"}).json()
    assert room["liveliness"] == 9
    assert room["max_concurrent"] == 3
    with client.websocket_connect(f"/ws/{room['id']}") as websocket:
        assert websocket.receive_json()["owner"] == {"name": "Riad"}


def test_settings_put_keeps_env_overridden_fields_out_of_the_file(client, environment):
    view = client.get("/api/settings").json()
    assert "room_defaults.cooldown_s" in view["env_overrides"]
    view["room_defaults"]["cooldown_s"] = 99
    view["owner"]["name"] = "Riad"
    assert client.put("/api/settings", json=view).status_code == 200
    config = tomllib.loads(open(environment["ADDA_CONFIG"], encoding="utf-8").read())
    assert config["room_defaults"]["cooldown_s"] == 0.0  # the file default, not the pushed 99


@pytest.mark.parametrize(
    "change",
    [
        lambda view: view["room_defaults"].update(liveliness=99),
        lambda view: view["room_defaults"].update(max_concurrent=5),
        lambda view: view["room_defaults"].update(access="root"),
        lambda view: view["room_defaults"].update(afterthought_scale=0),
        lambda view: view["room_defaults"].update(more_limit=-1),
        lambda view: view["router"].update(provider="carrier-pigeon"),
        lambda view: view["harness"]["pi"].update(thinking="extreme"),
        lambda view: view.update(keys={"made-up": "value"}),
    ],
)
def test_invalid_settings_are_a_400_and_write_nothing(client, environment, change):
    view = client.get("/api/settings").json()
    change(view)
    response = client.put("/api/settings", json=view)
    assert response.status_code == 400
    assert "detail" in response.json()
    assert not os.path.exists(environment["ADDA_CONFIG"])
    assert not os.path.exists(environment["ADDA_KEYS_FILE"])


# WebSocket --------------------------------------------------------------------------------


def test_hello_carries_room_messages_owner_and_router(client):
    room = make_room(client)
    with client.websocket_connect(f"/ws/{room['id']}") as websocket:
        hello = websocket.receive_json()
    assert hello["type"] == "hello"
    assert hello["room"]["id"] == room["id"]
    assert [seat["name"] for seat in hello["room"]["seats"]] == ["Ravi", "Tuli", "Pi"]
    assert hello["messages"] == []
    assert hello["owner"] == {"name": "Owner"}
    assert hello["router"] == "heuristic"


def test_unknown_room_socket_sends_error_and_closes(client):
    with client.websocket_connect("/ws/nope") as websocket:
        assert websocket.receive_json()["type"] == "error"
        with pytest.raises(Exception) as close_info:
            websocket.receive_json()
    assert getattr(close_info.value, "code", None) == 4404


def test_say_runs_the_mentioned_seat_and_every_tab_sees_it(client, factory):
    room = make_room(client)
    ravi = seat_id(room, "Ravi")
    with client.websocket_connect(f"/ws/{room['id']}") as tab_one:
        with client.websocket_connect(f"/ws/{room['id']}") as tab_two:
            tab_one.receive_json()
            tab_two.receive_json()
            factory.by_name["Ravi"].script(SeatReply(text="my view", cost_usd=0.25))
            tab_one.send_json({"type": "say", "text": "@ravi thoughts?"})
            frames_one = receive_until(tab_one, "quiet")
            frames_two = receive_until(tab_two, "quiet")

    assert frames_one == frames_two
    kinds = [frame["type"] for frame in frames_one]
    assert kinds == ["message", "typing", "typing", "message", "room", "quiet"]
    owner_message, seat_message = frames_one[0]["message"], frames_one[3]["message"]
    assert (owner_message["role"], owner_message["mentions"]) == ("owner", [ravi])
    assert (seat_message["author"], seat_message["seat_id"]) == ("Ravi", ravi)
    assert seat_message["meta"]["woken_by"] == "mention"
    assert frames_one[1] == {"type": "typing", "seat": ravi, "state": "start"}
    assert frames_one[4]["room"]["spent_usd"] == pytest.approx(0.25)
    assert frames_one[5] == {
        "type": "quiet",
        "reason": "paused",
        "next_tick_in_s": None,
        "ticks_left": 0,
    }


def test_room_and_seat_frames_patch_the_room_and_reach_every_tab(client):
    room = make_room(client)
    ravi = seat_id(room, "Ravi")
    with client.websocket_connect(f"/ws/{room['id']}") as tab_one:
        with client.websocket_connect(f"/ws/{room['id']}") as tab_two:
            tab_one.receive_json()
            tab_two.receive_json()

            tab_one.send_json({"type": "room", "patch": {"liveliness": 7, "title": "Hot"}})
            for tab in (tab_one, tab_two):
                frame = tab.receive_json()
                assert frame["type"] == "room"
                assert (frame["room"]["liveliness"], frame["room"]["title"]) == (7, "Hot")

            tab_two.send_json(
                {"type": "seat", "seat": ravi, "patch": {"name": "Rabindra", "thinking": "high"}}
            )
            for tab in (tab_one, tab_two):
                frame = tab.receive_json()
                renamed = next(seat for seat in frame["room"]["seats"] if seat["id"] == ravi)
                assert (renamed["name"], renamed["handle"], renamed["thinking"]) == (
                    "Rabindra",
                    "rabindra",
                    "high",
                )
    assert client.get(f"/api/rooms/{room['id']}").json()["room"]["liveliness"] == 7


@pytest.mark.parametrize(
    ("frame", "fragment"),
    [
        ({"type": "teleport"}, "unknown frame type"),
        ({"type": "say", "text": "   "}, "non-empty"),
        ({"type": "room", "patch": {"liveliness": 99}}, "liveliness"),
        ({"type": "room", "patch": {"max_concurrent": 9}}, "max_concurrent"),
        ({"type": "room", "patch": {"access": "root"}}, "access"),
        ({"type": "room"}, "patch"),
        ({"type": "seat", "seat": "nope", "patch": {"color": "#000"}}, "nope"),
        ({"type": "seat", "seat": "x", "patch": []}, "patch"),
        ({"type": "wake", "seat": "nope"}, "nope"),
    ],
)
def test_bad_frames_get_an_error_frame_and_the_socket_survives(client, frame, fragment):
    room = make_room(client)
    with client.websocket_connect(f"/ws/{room['id']}") as websocket:
        websocket.receive_json()
        websocket.send_json(frame)
        error = websocket.receive_json()
        assert error["type"] == "error"
        assert fragment in error["text"]
        websocket.send_json({"type": "room", "patch": {"title": "still alive"}})
        assert websocket.receive_json()["room"]["title"] == "still alive"


def test_non_json_text_gets_an_error_frame(client):
    room = make_room(client)
    with client.websocket_connect(f"/ws/{room['id']}") as websocket:
        websocket.receive_json()
        websocket.send_text("not json")
        assert websocket.receive_json()["type"] == "error"
        websocket.send_text("[1, 2]")
        assert "JSON object" in websocket.receive_json()["text"]


def test_wake_wake_all_and_stop_frames(client, factory):
    room = make_room(client)
    with client.websocket_connect(f"/ws/{room['id']}") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "wake", "seat": seat_id(room, "Tuli")})
        woken = receive_until(websocket, "quiet")
        assert [f["message"]["author"] for f in woken if f["type"] == "message"] == ["Tuli"]
        assert woken[-1] == {
            "type": "quiet",
            "reason": "paused",
            "next_tick_in_s": None,
            "ticks_left": 0,
        }

        websocket.send_json({"type": "wake_all"})
        everyone = receive_until(websocket, "quiet")
        authors = [f["message"]["author"] for f in everyone if f["type"] == "message"]
        assert authors == ["Ravi", "Tuli", "Pi"]

        websocket.send_json({"type": "stop"})  # nothing running: accepted silently
        websocket.send_json({"type": "room", "patch": {"title": "after stop"}})
        assert websocket.receive_json()["room"]["title"] == "after stop"


def test_budget_blocks_wake_with_an_error_frame(client, factory):
    room = make_room(client, budget_usd=0.1)
    factory.by_name["Ravi"].script(SeatReply(text="costly", cost_usd=0.5))
    with client.websocket_connect(f"/ws/{room['id']}") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "say", "text": "@ravi hi"})
        frames = receive_until(websocket, "quiet")
        assert frames[-1] == {
            "type": "quiet",
            "reason": "budget",
            "next_tick_in_s": None,
            "ticks_left": 0,
        }
        websocket.send_json({"type": "wake", "seat": seat_id(room, "Tuli")})
        error = websocket.receive_json()
    assert error["type"] == "error"
    assert error["text"] == "Budget of $0.10 reached; raise it to continue."


def test_end_frame_sends_ended_with_notes_and_blocks_further_input(client):
    room = make_room(client)
    ravi = seat_id(room, "Ravi")
    with client.websocket_connect(f"/ws/{room['id']}") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "say", "text": "@ravi @pi go"})
        receive_until(websocket, "quiet")
        websocket.send_json({"type": "end"})
        ended = receive_until(websocket, "ended")[-1]
        assert list(ended["notes"]) == [ravi]
        websocket.send_json({"type": "say", "text": "hello?"})
        error = receive_until(websocket, "error")[-1]
        assert "ended" in error["text"]


# Folders and eagerness (SPEC v0.3 D2, D3, D4) ---------------------------------------------


def test_folder_listing_defaults_to_rooms_dir_and_skips_hidden_folders(client, tmp_path):
    rooms = tmp_path / "rooms"
    (rooms / "beta").mkdir(parents=True)
    (rooms / "Alpha").mkdir()
    (rooms / ".hidden").mkdir()
    (rooms / "file.txt").write_text("x", encoding="utf-8")
    body = client.get("/api/folders").json()
    assert body["path"] == str(rooms)
    assert body["parent"] == str(tmp_path)
    assert body["folders"] == [
        {"name": "Alpha", "path": str(rooms / "Alpha")},
        {"name": "beta", "path": str(rooms / "beta")},
    ]


def test_folder_listing_has_no_parent_at_home_and_refuses_paths_outside(client, tmp_path):
    assert client.get("/api/folders", params={"path": str(tmp_path)}).json()["parent"] is None
    outside = client.get("/api/folders", params={"path": str(tmp_path.parent)})
    assert outside.status_code == 400
    assert client.get("/api/folders", params={"path": "/etc"}).status_code == 400
    assert client.get("/api/folders", params={"path": "relative"}).status_code == 400
    missing = client.get("/api/folders", params={"path": str(tmp_path / "nope")})
    assert missing.status_code == 400


def test_folder_listing_refuses_a_symlink_that_leaves_home(client, tmp_path):
    outside_target = tmp_path.parent / "outside-target"
    outside_target.mkdir(exist_ok=True)
    (tmp_path / "escape").symlink_to(outside_target)
    response = client.get("/api/folders", params={"path": str(tmp_path / "escape")})
    assert response.status_code == 400


def test_creating_a_folder_returns_the_entry_and_409_when_it_exists(client, tmp_path):
    response = client.post("/api/folders", json={"path": str(tmp_path), "name": "fresh"})
    assert response.status_code == 200
    assert response.json() == {"name": "fresh", "path": str(tmp_path / "fresh")}
    assert (tmp_path / "fresh").is_dir()
    again = client.post("/api/folders", json={"path": str(tmp_path), "name": "fresh"})
    assert again.status_code == 409


@pytest.mark.parametrize("name", ["", "  ", "..", "a/b"])
def test_creating_a_folder_rejects_bad_names(client, tmp_path, name):
    response = client.post("/api/folders", json={"path": str(tmp_path), "name": name})
    assert response.status_code == 400


def test_creating_a_folder_outside_home_is_refused(client, tmp_path):
    response = client.post("/api/folders", json={"path": str(tmp_path.parent), "name": "x"})
    assert response.status_code == 400


def test_default_room_folder_has_the_date_and_gains_a_numeric_suffix(client, tmp_path):
    first = make_room(client, "Design: the Queue!")
    second = make_room(client, "Design: the Queue!")
    third = make_room(client, "Design: the Queue!")
    base = tmp_path / "rooms" / "20261014-design-the-queue"
    assert [first["folder"], second["folder"], third["folder"]] == [
        str(base),
        f"{base}-2",
        f"{base}-3",
    ]


def test_folder_suggestion_matches_the_folder_a_room_would_get(client, tmp_path):
    suggestion = client.get("/api/rooms/folder-suggestion", params={"title": "Hello World"})
    expected = tmp_path / "rooms" / "20261014-hello-world"
    assert suggestion.json() == {"folder": str(expected)}
    assert make_room(client, "Hello World")["folder"] == str(expected)
    after = client.get("/api/rooms/folder-suggestion", params={"title": "Hello World"})
    assert after.json() == {"folder": f"{expected}-2"}


def test_seat_eagerness_defaults_to_five_and_can_be_set_and_patched(client):
    room = make_room(
        client,
        seats=[
            {"name": "Ravi", "program": "claude", "eagerness": 8},
            {"name": "Tuli", "program": "pi"},
        ],
    )
    assert [seat["eagerness"] for seat in room["seats"]] == [8, 5]
    ravi = seat_id(room, "Ravi")
    patched = client.patch(f"/api/rooms/{room['id']}/seats/{ravi}", json={"eagerness": 0})
    assert patched.status_code == 200
    assert patched.json()["seats"][0]["eagerness"] == 0


@pytest.mark.parametrize("value", [-1, 11, 2.5, "high", True])
def test_seat_patch_rejects_eagerness_outside_zero_to_ten(client, value):
    room = make_room(client)
    ravi = seat_id(room, "Ravi")
    response = client.patch(f"/api/rooms/{room['id']}/seats/{ravi}", json={"eagerness": value})
    assert response.status_code == 400


def test_new_seat_rejects_eagerness_outside_zero_to_ten(client):
    response = client.post(
        "/api/rooms",
        json={"title": "x", "seats": [{"name": "A", "program": "claude", "eagerness": 11}]},
    )
    assert response.status_code == 400


# Threads (SPEC v0.5 F2) ---------------------------------------------------------------------


def first_thread_id(room: dict) -> str:
    return room["threads"][0]["id"]


def test_new_room_has_a_first_thread_titled_like_the_room(client):
    room = make_room(client, "Billing")
    assert [thread["title"] for thread in room["threads"]] == ["Billing"]
    listed = client.get(f"/api/rooms/{room['id']}/threads").json()
    assert [thread["id"] for thread in listed] == [first_thread_id(room)]
    assert set(listed[0]) == {
        "id",
        "room_id",
        "title",
        "created_at",
        "ended_at",
        "message_count",
        "last_author",
        "last_line",
    }


def test_thread_crud_routes(client):
    room = make_room(client, "Billing")
    created = client.post(f"/api/rooms/{room['id']}/threads", json={"title": "Refunds"})
    assert created.status_code == 200, created.text
    thread = created.json()
    assert (thread["title"], thread["room_id"], thread["message_count"]) == (
        "Refunds",
        room["id"],
        0,
    )
    listed = client.get(f"/api/rooms/{room['id']}/threads").json()
    assert [entry["title"] for entry in listed] == ["Refunds", "Billing"]  # newest first

    fetched = client.get(f"/api/threads/{thread['id']}").json()
    assert set(fetched) == {"thread", "room", "messages"}
    assert fetched["thread"]["id"] == thread["id"]
    assert fetched["room"]["id"] == room["id"]
    assert fetched["messages"] == []

    renamed = client.patch(f"/api/threads/{thread['id']}", json={"title": "Refund flow"})
    assert renamed.json()["title"] == "Refund flow"
    assert client.patch(f"/api/threads/{thread['id']}", json={"title": " "}).status_code == 400

    ended = client.post(f"/api/threads/{thread['id']}/end")
    assert ended.status_code == 200
    assert ended.json()["thread"]["ended_at"] is not None
    assert ended.json()["notes"] == {}
    assert client.post(f"/api/threads/{thread['id']}/end").status_code == 409
    # The room stays open, and its page now opens the remaining open thread.
    room_body = client.get(f"/api/rooms/{room['id']}").json()
    assert room_body["room"]["ended_at"] is None
    assert room_body["thread"]["id"] == first_thread_id(room)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/threads/nope"),
        ("patch", "/api/threads/nope"),
        ("post", "/api/threads/nope/end"),
        ("get", "/api/rooms/nope/threads"),
        ("post", "/api/rooms/nope/threads"),
    ],
)
def test_unknown_thread_or_room_is_404(client, method, path):
    response = client.request(method, path, json={"title": "x"})
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_get_room_returns_messages_of_the_newest_open_thread(client):
    room = make_room(client, "Billing")
    with client.websocket_connect(f"/ws/threads/{first_thread_id(room)}") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "say", "text": "in the first thread"})
        receive_until(websocket, "quiet")
    second = client.post(f"/api/rooms/{room['id']}/threads", json={"title": "Second"}).json()
    body = client.get(f"/api/rooms/{room['id']}").json()
    assert body["thread"]["id"] == second["id"]
    assert body["messages"] == []
    first = client.get(f"/api/threads/{first_thread_id(room)}").json()
    assert [message["text"] for message in first["messages"]] == ["in the first thread"]
    assert first["thread"]["message_count"] == 1
    assert first["thread"]["last_line"] == "in the first thread"


def test_archived_room_refuses_new_threads(client):
    room = make_room(client, "Billing")
    assert client.post(f"/api/rooms/{room['id']}/end").status_code == 200
    archived = client.get(f"/api/rooms/{room['id']}").json()["room"]
    assert archived["ended_at"] is not None
    assert all(thread["ended_at"] is not None for thread in archived["threads"])
    response = client.post(f"/api/rooms/{room['id']}/threads", json={"title": "late"})
    assert response.status_code == 409


@pytest.mark.parametrize("path", ["/room/abc", "/room/abc/thread/def"])
def test_room_and_thread_pages_serve_the_room_page(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "room page" in response.text


def test_thread_socket_hello_carries_room_thread_messages_owner_and_router(client):
    room = make_room(client, "Billing")
    thread_id = first_thread_id(room)
    with client.websocket_connect(f"/ws/threads/{thread_id}") as websocket:
        hello = websocket.receive_json()
    assert hello["type"] == "hello"
    assert set(hello) == {"type", "room", "thread", "messages", "owner", "router"}
    assert hello["room"]["id"] == room["id"]
    assert hello["thread"]["id"] == thread_id
    assert hello["thread"]["title"] == "Billing"
    assert hello["messages"] == []
    assert set(hello["owner"]) == {"name"}
    assert hello["router"] == "heuristic"


def test_unknown_thread_socket_sends_error_and_closes(client):
    with client.websocket_connect("/ws/threads/nope") as websocket:
        frame = websocket.receive_json()
        assert frame["type"] == "error"
        assert "not found" in frame["text"]


def test_room_socket_alias_opens_the_newest_open_thread(client):
    room = make_room(client, "Billing")
    second = client.post(f"/api/rooms/{room['id']}/threads", json={"title": "Second"}).json()
    with client.websocket_connect(f"/ws/{room['id']}") as websocket:
        assert websocket.receive_json()["thread"]["id"] == second["id"]


def test_messages_stay_in_their_thread_and_room_frames_reach_every_thread(client):
    room = make_room(client, "Billing")
    first_id = first_thread_id(room)
    second = client.post(f"/api/rooms/{room['id']}/threads", json={"title": "Second"}).json()
    with client.websocket_connect(f"/ws/threads/{first_id}") as first_tab:
        first_tab.receive_json()
        with client.websocket_connect(f"/ws/threads/{second['id']}") as second_tab:
            second_tab.receive_json()
            second_tab.send_json({"type": "say", "text": "only in the second"})
            receive_until(second_tab, "quiet")

            # A room patch from the second thread's tab reaches the first thread's tab.
            second_tab.send_json({"type": "room", "patch": {"budget_usd": 3.0}})
            room_frame = receive_until(first_tab, "room")
            assert all(frame["type"] != "message" for frame in room_frame)
            assert room_frame[-1]["room"]["budget_usd"] == 3.0

            # A rename reaches every tab of the room as a thread frame.
            client.patch(f"/api/threads/{second['id']}", json={"title": "Renamed"})
            thread_frame = receive_until(first_tab, "thread")[-1]
            assert thread_frame["thread"]["id"] == second["id"]
            assert thread_frame["thread"]["title"] == "Renamed"
            assert receive_until(second_tab, "thread")[-1]["thread"]["title"] == "Renamed"

            # Ending over the socket ends only that thread.
            second_tab.send_json({"type": "end"})
            receive_until(second_tab, "ended")
            ended_frame = receive_until(second_tab, "thread")[-1]
            assert ended_frame["thread"]["ended_at"] is not None
    assert client.get(f"/api/threads/{first_id}").json()["messages"] == []
    assert client.get(f"/api/threads/{first_id}").json()["thread"]["ended_at"] is None


def test_new_thread_is_announced_to_open_tabs_with_a_room_frame(client):
    room = make_room(client, "Billing")
    with client.websocket_connect(f"/ws/threads/{first_thread_id(room)}") as websocket:
        websocket.receive_json()
        client.post(f"/api/rooms/{room['id']}/threads", json={"title": "Second"})
        room_frame = receive_until(websocket, "room")[-1]
    assert [thread["title"] for thread in room_frame["room"]["threads"]] == ["Second", "Billing"]


def test_search_hits_carry_thread_id_and_title(client):
    room = make_room(client, "storage choices")
    thread = client.post(f"/api/rooms/{room['id']}/threads", json={"title": "queues"}).json()
    with client.websocket_connect(f"/ws/threads/{thread['id']}") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "say", "text": "prefer a wombat queue"})
        receive_until(websocket, "quiet")
    hit = client.get("/api/search", params={"q": "wombat"}).json()[0]
    assert (hit["room_title"], hit["thread_id"], hit["thread_title"]) == (
        "storage choices",
        thread["id"],
        "queues",
    )


# Memory review (SPEC v0.5 F3) ---------------------------------------------------------------


def test_ending_a_thread_writes_notes_with_their_source(client, factory):
    room = make_room(client, "Billing")
    thread_id = first_thread_id(room)
    factory_reply_then_note(client, factory, thread_id, "- Refunds go through the ledger")

    notes = client.get("/api/personas/alpha/memory").json()
    newest = notes[0]
    assert newest["title"] == "Refunds go through the ledger"
    assert (newest["room_id"], newest["room_title"]) == (room["id"], "Billing")
    assert (newest["thread_id"], newest["thread_title"]) == (thread_id, "Billing")
    assert newest["written"] is not None
    assert notes[-1]["file"] == "001-first-fact.md"
    assert notes[-1]["room_id"] is None  # a legacy note

    grouped = client.get(f"/api/rooms/{room['id']}/memory").json()
    assert set(grouped) == {"alpha", "beta"}  # every persona a seat uses
    assert [note["file"] for note in grouped["alpha"]] == [newest["file"]]
    assert grouped["beta"] == []


def factory_reply_then_note(client, factory, thread_id: str, note_text: str) -> None:
    """Ravi (persona alpha) speaks once in the thread, then answers the end-of-thread question
    with `note_text`."""
    with client.websocket_connect(f"/ws/threads/{thread_id}") as websocket:
        websocket.receive_json()
        factory.by_name["Ravi"].script("my reply", note_text)
        websocket.send_json({"type": "say", "text": "@ravi thoughts?"})
        receive_until(websocket, "quiet")
    assert client.post(f"/api/threads/{thread_id}/end").status_code == 200


def test_memory_note_update_and_delete(client, tmp_path):
    memory_directory = tmp_path / "personas" / "alpha" / "memory"
    updated = client.put(
        "/api/personas/alpha/memory/001-first-fact.md",
        json={"title": "Short answers", "body": "The owner wants short answers."},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["title"] == "Short answers"
    assert "- [Short answers](001-first-fact.md)" in (memory_directory / "index.md").read_text()

    deleted = client.delete("/api/personas/alpha/memory/001-first-fact.md")
    assert deleted.json() == {"deleted": "001-first-fact.md"}
    assert client.get("/api/personas/alpha/memory").json() == []
    assert "001-first-fact.md" not in (memory_directory / "index.md").read_text()


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("get", "/api/personas/nobody/memory", 404),
        ("put", "/api/personas/nobody/memory/001-first-fact.md", 404),
        ("put", "/api/personas/alpha/memory/999-missing.md", 404),
        ("delete", "/api/personas/alpha/memory/999-missing.md", 404),
        ("put", "/api/personas/alpha/memory/index.md", 400),
        ("delete", "/api/personas/alpha/memory/001-First.md", 400),
        ("delete", "/api/personas/alpha/memory/001-a_b.md", 400),
        # An encoded slash never reaches the handler: no route has that many segments.
        ("delete", "/api/personas/alpha/memory/..%2Fpersona.md", 404),
        ("get", "/api/personas/bad..slug/memory", 400),
        ("get", "/api/rooms/nope/memory", 404),
    ],
)
def test_memory_route_errors(client, method, path, status):
    response = client.request(method, path, json={"title": "x"})
    assert response.status_code == status, response.text
    assert response.json()["detail"]


def test_memory_note_update_rejects_a_blank_title(client):
    response = client.put("/api/personas/alpha/memory/001-first-fact.md", json={"title": "  "})
    assert response.status_code == 400


# Open folder (SPEC v0.5 F4) -----------------------------------------------------------------


def test_open_folder_opens_and_creates_the_room_folder(client, tmp_path, opened_folders):
    folder = tmp_path / "projects" / "billing"
    room = make_room(client, "Billing", folder=str(folder))
    folder.rmdir()  # deleted by hand since the room was made
    response = client.post(f"/api/rooms/{room['id']}/open-folder")
    assert response.status_code == 200, response.text
    assert response.json() == {"opened": str(folder)}
    assert folder.is_dir()
    assert opened_folders == [folder]


def test_open_folder_refuses_a_folder_outside_home(client, store, opened_folders):
    room = make_room(client, "Billing")
    store.update_room(room["id"], folder="/etc")
    response = client.post(f"/api/rooms/{room['id']}/open-folder")
    assert response.status_code == 400
    assert "inside" in response.json()["detail"]
    assert opened_folders == []


def test_open_folder_of_an_unknown_room_is_404(client):
    assert client.post("/api/rooms/nope/open-folder").status_code == 404


# Access levels (SPEC v0.6 G1) -----------------------------------------------------------------


def test_a_new_room_gets_the_settings_default_access_and_seats_report_effective_access(client):
    room = make_room(client)
    assert room["access"] == "talk"
    assert [seat["access"] for seat in room["seats"]] == [None, None, None]
    assert [seat["effective_access"] for seat in room["seats"]] == ["talk"] * 3


def test_room_and_seat_access_are_accepted_when_creating_a_room(client):
    room = make_room(
        client,
        access="look",
        seats=[
            {"name": "Ravi", "program": "claude"},
            {"name": "Tuli", "program": "pi", "access": "work"},
        ],
    )
    assert room["access"] == "look"
    by_name = {seat["name"]: seat for seat in room["seats"]}
    assert (by_name["Ravi"]["access"], by_name["Ravi"]["effective_access"]) == (None, "look")
    assert (by_name["Tuli"]["access"], by_name["Tuli"]["effective_access"]) == ("work", "work")


def test_the_settings_default_access_is_used_when_the_request_names_none(
    environment, static_directory, factory, store, tmp_path
):
    environment["ADDA_ACCESS"] = "look"
    app = create_app(
        load_settings(environment),
        store=store,
        router=HeuristicRouter(),
        adapter_builder=factory,
        detect=fake_harnesses,
        environment=environment,
        static_directory=static_directory,
        home=tmp_path,
        today=lambda: TODAY,
    )
    with TestClient(app) as client_with_default:
        assert client_with_default.get("/api/settings").json()["room_defaults"]["access"] == "look"
        assert make_room(client_with_default)["access"] == "look"


def test_patching_access_over_rest_changes_effective_access_and_rebuilds_adapters(client, factory):
    room = make_room(client)
    ravi = seat_id(room, "Ravi")
    tuli = seat_id(room, "Tuli")
    response = client.patch(f"/api/rooms/{room['id']}", json={"access": "work"})
    assert response.status_code == 200, response.text
    assert {seat["effective_access"] for seat in response.json()["seats"]} == {"work"}
    assert factory.by_name["Ravi"].access == "work"

    response = client.patch(f"/api/rooms/{room['id']}/seats/{ravi}", json={"access": "look"})
    assert response.status_code == 200, response.text
    by_id = {seat["id"]: seat for seat in response.json()["seats"]}
    assert (by_id[ravi]["access"], by_id[ravi]["effective_access"]) == ("look", "look")
    assert by_id[tuli]["effective_access"] == "work"
    assert factory.by_name["Ravi"].access == "look"

    response = client.patch(f"/api/rooms/{room['id']}/seats/{ravi}", json={"access": None})
    by_id = {seat["id"]: seat for seat in response.json()["seats"]}
    assert (by_id[ravi]["access"], by_id[ravi]["effective_access"]) == (None, "work")


@pytest.mark.parametrize("bad_value", ["root", "", 3, None])
def test_an_invalid_room_access_patch_is_a_400(client, bad_value):
    room = make_room(client)
    response = client.patch(f"/api/rooms/{room['id']}", json={"access": bad_value})
    assert response.status_code == 400, response.text
    assert client.get(f"/api/rooms/{room['id']}").json()["room"]["access"] == "talk"


def test_an_invalid_seat_access_patch_is_a_400(client):
    room = make_room(client)
    ravi = seat_id(room, "Ravi")
    for bad_value in ("root", "", 3):
        response = client.patch(f"/api/rooms/{room['id']}/seats/{ravi}", json={"access": bad_value})
        assert response.status_code == 400, response.text


def test_new_seat_access_is_accepted_when_adding_a_seat(client):
    room = make_room(client, access="look")
    response = client.post(
        f"/api/rooms/{room['id']}/seats", json={"name": "Neo", "program": "pi", "access": "work"}
    )
    assert response.status_code == 200, response.text
    neo = next(seat for seat in response.json()["seats"] if seat["name"] == "Neo")
    assert (neo["access"], neo["effective_access"]) == ("work", "work")


def test_access_patches_over_the_websocket_reach_every_tab(client):
    room = make_room(client)
    ravi = seat_id(room, "Ravi")
    with client.websocket_connect(f"/ws/{room['id']}") as tab:
        tab.receive_json()
        tab.send_json({"type": "room", "patch": {"access": "look"}})
        frame = tab.receive_json()
        assert frame["room"]["access"] == "look"
        tab.send_json({"type": "seat", "seat": ravi, "patch": {"access": "work"}})
        frame = tab.receive_json()
        changed = next(seat for seat in frame["room"]["seats"] if seat["id"] == ravi)
        assert (changed["access"], changed["effective_access"]) == ("work", "work")
