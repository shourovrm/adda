import shutil
from pathlib import Path

import pytest

from adda.personas import PersonaError, load_personas, memory_index, write_memory_note

FIXTURES = Path(__file__).parent / "fixtures" / "personas"


@pytest.fixture
def personas_directory(tmp_path) -> Path:
    # Copied so tests that write memory notes never touch the committed fixtures.
    copy = tmp_path / "personas"
    shutil.copytree(FIXTURES, copy)
    return copy


def write_persona(directory: Path, folder: str, frontmatter: str) -> None:
    persona_folder = directory / folder
    persona_folder.mkdir(parents=True)
    (persona_folder / "persona.md").write_text(f"---\n{frontmatter}\n---\nBody.\n")


def test_load_fixture_set_in_order(personas_directory):
    personas = load_personas(personas_directory)
    assert list(personas) == ["alpha", "beta"]
    alpha = personas["alpha"]
    assert alpha.program == "claude"
    assert alpha.model == "haiku"
    assert alpha.color == "#D9480F"
    assert alpha.role == "Architecture and trade-offs"
    assert alpha.stance.startswith("Prefers the smallest design")
    assert alpha.body == "You are Alpha, a seat in Adda. Argue for simple designs."
    assert alpha.directory == personas_directory / "alpha"
    assert personas["beta"].program == "pi"
    assert personas["beta"].model is None


def test_missing_directory_gives_empty_dict(tmp_path):
    assert load_personas(tmp_path / "nope") == {}


def test_name_mismatch_raises(tmp_path):
    write_persona(tmp_path, "gamma", "name: delta\nprogram: claude")
    with pytest.raises(PersonaError, match="folder name"):
        load_personas(tmp_path)


def test_bad_program_raises(tmp_path):
    write_persona(tmp_path, "gamma", "name: gamma\nprogram: emacs")
    with pytest.raises(PersonaError, match="program"):
        load_personas(tmp_path)


def test_missing_frontmatter_raises(tmp_path):
    (tmp_path / "gamma").mkdir()
    (tmp_path / "gamma" / "persona.md").write_text("no frontmatter here")
    with pytest.raises(PersonaError, match="frontmatter"):
        load_personas(tmp_path)


def test_memory_index_returns_index_text(personas_directory):
    alpha = load_personas(personas_directory)["alpha"]
    assert memory_index(alpha) == "- [First fact](001-first-fact.md)"


def test_memory_index_empty_when_no_file(tmp_path):
    write_persona(tmp_path, "gamma", "name: gamma\nprogram: claude")
    assert memory_index(load_personas(tmp_path)["gamma"]) == ""


def test_write_memory_note_numbers_and_indexes(personas_directory):
    alpha = load_personas(personas_directory)["alpha"]
    note_path = write_memory_note(alpha, "Owner prefers SQLite!", "  Use stdlib sqlite3.  ")
    assert note_path.name == "002-owner-prefers-sqlite.md"
    assert note_path.read_text() == "# Owner prefers SQLite!\n\nUse stdlib sqlite3.\n"
    assert memory_index(alpha).splitlines() == [
        "- [First fact](001-first-fact.md)",
        "- [Owner prefers SQLite!](002-owner-prefers-sqlite.md)",
    ]
    third = write_memory_note(alpha, "Another", "text")
    assert third.name == "003-another.md"


def test_write_memory_note_creates_missing_memory_directory(tmp_path):
    write_persona(tmp_path, "gamma", "name: gamma\nprogram: claude")
    gamma = load_personas(tmp_path)["gamma"]
    note_path = write_memory_note(gamma, "First", "body")
    assert note_path.name == "001-first.md"
    assert memory_index(gamma) == "- [First](001-first.md)"
