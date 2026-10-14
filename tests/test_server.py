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
def client(environment, static_directory, factory, store, tmp_path):
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
    for bad_patch in ({"liveliness": 11}, {"mode": "chaos"}, {"folder": "/x"}):
        assert client.patch(f"/api/rooms/{room['id']}", json=bad_patch).status_code == 400


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
    assert config["harness"]["claude"]["model"] == "opus"
    assert SECRET_KEY not in open(environment["ADDA_CONFIG"], encoding="utf-8").read()

    keys_path = environment["ADDA_KEYS_FILE"]
    assert f"OPENROUTER_API_KEY={SECRET_KEY}" in open(keys_path, encoding="utf-8").read()
    assert stat.S_IMODE(os.stat(keys_path).st_mode) == 0o600

    # The new owner name reaches a room opened afterwards, and new rooms use the new defaults.
    room = client.post("/api/rooms", json={"title": "after"}).json()
    assert room["liveliness"] == 9
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
    assert frames_one[5] == {"type": "quiet", "reason": "paused"}


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
        assert woken[-1] == {"type": "quiet", "reason": "paused"}

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
        assert frames[-1] == {"type": "quiet", "reason": "budget"}
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
