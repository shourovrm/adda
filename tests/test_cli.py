"""`adda serve|rooms|search|harnesses` through adda.cli.main, with no server started."""

from __future__ import annotations

import pytest

from adda.cli import build_parser, main
from adda.models import Harness, Message
from adda.store import Store


@pytest.fixture
def cli_environment(tmp_path, monkeypatch):
    """Point every path the CLI reads at tmp_path so no real data, config or key is touched."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("ADDA_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADDA_PERSONAS_DIR", str(tmp_path / "personas"))
    monkeypatch.setenv("ADDA_CONFIG", str(tmp_path / "no-such-config.toml"))
    monkeypatch.setenv("ADDA_KEYS_FILE", str(tmp_path / "no-such-keys.env"))
    monkeypatch.setenv("ADDA_JEV_KEY_FILE", str(tmp_path / "no-such-key-file.env"))
    monkeypatch.setenv("ADDA_ROUTER", "heuristic")
    for variable in ("ADDA_HOST", "ADDA_PORT", "OPENROUTER_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(variable, raising=False)
    return data_dir


@pytest.fixture
def populated_store(cli_environment):
    store = Store(cli_environment / "adda.db")
    room = store.create_room("storage choices", "/tmp/rooms/storage", "roundrobin", 4, 2.5)
    store.add_seat(room.id, name="Ravi", program="claude")
    store.add_seat(room.id, name="Tuli", program="pi")
    store.add_message(
        Message(room_id=room.id, author="owner", role="owner", text="prefer sqlite always")
    )
    store.close()
    return room


def test_rooms_with_none(cli_environment, capsys):
    main(["rooms"])
    assert capsys.readouterr().out.strip() == "no rooms"


def test_rooms_lists_one_room(populated_store, capsys):
    main(["rooms"])
    line = capsys.readouterr().out.strip()
    assert populated_store.id in line
    assert "storage choices" in line
    assert "open" in line
    assert "roundrobin" in line
    assert "L4" in line
    assert "$0.00/$2.50" in line
    assert "[Ravi, Tuli]" in line


def test_rooms_shows_ended_status(populated_store, cli_environment, capsys):
    store = Store(cli_environment / "adda.db")
    store.end_room(populated_store.id)
    store.close()
    main(["rooms"])
    assert "ended" in capsys.readouterr().out


def test_search_hit(populated_store, capsys):
    main(["search", "sqlite"])
    output = capsys.readouterr().out
    assert populated_store.id in output
    assert "storage choices" in output
    assert "owner" in output
    assert "sqlite" in output


def test_search_no_hit(populated_store, capsys):
    main(["search", "zebra"])
    assert capsys.readouterr().out.strip() == "no matches"


def test_search_limit_option(cli_environment, capsys):
    store = Store(cli_environment / "adda.db")
    room = store.create_room("many")
    for number in range(3):
        store.add_message(
            Message(room_id=room.id, author="owner", role="owner", text=f"needle {number}")
        )
    store.close()
    main(["search", "needle", "--limit", "2"])
    assert len(capsys.readouterr().out.strip().splitlines()) == 2


def test_harnesses_prints_the_detection_table(cli_environment, monkeypatch, capsys):
    detected = [
        Harness(
            program="claude",
            found=True,
            path="/usr/bin/claude",
            version="2.1.299",
            default_model="sonnet",
            login_hint="Run claude.",
        ),
        Harness(program="codex", found=False, login_hint="Run `codex login`.", enabled=False),
    ]
    monkeypatch.setattr("adda.cli.detect_harnesses", lambda settings: detected)
    main(["harnesses"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].split()[:3] == ["program", "found", "version"]
    assert lines[1].split()[:4] == ["claude", "yes", "2.1.299", "sonnet"]
    assert "/usr/bin/claude" in lines[1]
    assert lines[2].split()[:2] == ["codex", "NO"]
    assert "codex login" in lines[2]
    assert "disabled in settings" in lines[2]


def test_serve_passes_port_option_to_uvicorn(cli_environment, monkeypatch):
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append((app, kwargs)))
    main(["serve", "--port", "8799"])
    assert len(calls) == 1
    application, keyword_arguments = calls[0]
    assert keyword_arguments == {"host": "127.0.0.1", "port": 8799}
    assert application.title == "Adda"


def test_serve_host_option_overrides_environment(cli_environment, monkeypatch):
    calls = []
    monkeypatch.setenv("ADDA_HOST", "0.0.0.0")
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append(kwargs))
    main(["serve", "--host", "127.0.0.2", "--port", "8799"])
    assert calls == [{"host": "127.0.0.2", "port": 8799}]


def test_serve_uses_environment_port_without_option(cli_environment, monkeypatch):
    calls = []
    monkeypatch.setenv("ADDA_PORT", "8798")
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append(kwargs))
    main(["serve"])
    assert calls == [{"host": "127.0.0.1", "port": 8798}]


def test_parser_serve_entry_parses_port():
    arguments = build_parser().parse_args(["serve", "--port", "8799"])
    assert arguments.port == 8799
    assert arguments.host is None


def test_parser_requires_a_command():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
