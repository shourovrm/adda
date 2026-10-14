"""`adda threads|search|seats|serve` through adda.cli.main, with no server started."""

from __future__ import annotations

from pathlib import Path

import pytest

from adda.cli import build_parser, main
from adda.models import Message
from adda.store import Store

FIXTURE_PERSONAS = Path(__file__).parent / "fixtures" / "personas"


@pytest.fixture
def cli_environment(tmp_path, monkeypatch):
    """Point every path the CLI reads at tmp_path so no real data or key file is touched."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("ADDA_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADDA_PERSONAS_DIR", str(tmp_path / "personas"))
    monkeypatch.setenv("ADDA_JEV_KEY_FILE", str(tmp_path / "no-such-key-file.env"))
    monkeypatch.setenv("ADDA_ROUTER", "heuristic")
    for variable in ("ADDA_HOST", "ADDA_PORT", "OPENROUTER_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(variable, raising=False)
    return data_dir


@pytest.fixture
def populated_store(cli_environment):
    store = Store(cli_environment / "adda.db")
    thread = store.create_thread("storage choices", "roundrobin")
    store.add_message(
        Message(thread_id=thread.id, author="owner", role="owner", text="prefer sqlite always")
    )
    store.close()
    return thread


def test_threads_with_none(cli_environment, capsys):
    main(["threads"])
    assert capsys.readouterr().out.strip() == "no threads"


def test_threads_lists_one_thread(populated_store, capsys):
    main(["threads"])
    line = capsys.readouterr().out.strip()
    assert populated_store.id in line
    assert "storage choices" in line
    assert "open" in line
    assert "roundrobin" in line


def test_threads_shows_ended_status(populated_store, cli_environment, capsys):
    store = Store(cli_environment / "adda.db")
    store.end_thread(populated_store.id)
    store.close()
    main(["threads"])
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
    thread = store.create_thread("many")
    for number in range(3):
        store.add_message(
            Message(thread_id=thread.id, author="owner", role="owner", text=f"needle {number}")
        )
    store.close()
    main(["search", "needle", "--limit", "2"])
    assert len(capsys.readouterr().out.strip().splitlines()) == 2


def test_seats_with_no_personas(cli_environment, capsys):
    main(["seats"])
    assert capsys.readouterr().out.startswith("no personas in")


def test_seats_lists_fixture_personas(cli_environment, monkeypatch, capsys):
    monkeypatch.setenv("ADDA_PERSONAS_DIR", str(FIXTURE_PERSONAS))
    main(["seats"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    alpha_columns = lines[0].split()
    beta_columns = lines[1].split()
    assert alpha_columns[:3] == ["alpha", "claude", "haiku"]
    assert beta_columns[:2] == ["beta", "pi"]
    assert "default model" in lines[1]
    assert "Architecture and trade-offs" in lines[0]
    assert "Testing and edge cases" in lines[1]
    for line in lines:
        assert "available" in line or "NOT FOUND" in line


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
