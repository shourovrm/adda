import shutil
from datetime import datetime
from pathlib import Path

import pytest

from adda.models import MemorySource
from adda.personas import (
    MemoryNoteNotFoundError,
    Persona,
    PersonaError,
    delete_memory_note,
    list_memory_notes,
    list_personas,
    load_persona,
    load_personas,
    memory_index,
    read_memory_note,
    rebuild_memory_index,
    update_memory_note,
    write_memory_note,
)

FIXTURES = Path(__file__).parent / "fixtures" / "personas"
SHIPPED_PERSONAS = Path(__file__).parent.parent / "personas"


@pytest.fixture
def personas_directory(tmp_path):
    # Copied so tests that write memory notes never touch the committed fixtures.
    target = tmp_path / "personas"
    shutil.copytree(FIXTURES, target)
    return target


def test_load_persona_uses_slug_and_display_name(personas_directory):
    persona = load_persona(personas_directory, "alpha")
    assert persona.slug == "alpha"
    assert persona.name == "alpha"
    assert persona.color == "#D9480F"
    assert persona.role == "Architecture and trade-offs"
    assert persona.directory == personas_directory / "alpha"
    assert persona.body


def test_program_and_model_in_frontmatter_are_ignored(personas_directory):
    persona = load_persona(personas_directory, "alpha")  # fixture still has program: claude
    assert not hasattr(persona, "program")
    assert not hasattr(persona, "model")


def test_display_name_may_differ_from_slug(tmp_path):
    folder = tmp_path / "users-view"
    folder.mkdir()
    (folder / "persona.md").write_text("---\nname: Pi\ncolor: '#123456'\n---\nVoice.\n")
    persona = load_persona(tmp_path, "users-view")
    assert (persona.slug, persona.name, persona.body) == ("users-view", "Pi", "Voice.")


def test_name_defaults_to_slug(tmp_path):
    (tmp_path / "bare").mkdir()
    (tmp_path / "bare" / "persona.md").write_text("---\nrole: r\n---\nBody\n")
    assert load_persona(tmp_path, "bare").name == "bare"


def test_missing_and_malformed_personas_fail_loudly(tmp_path):
    with pytest.raises(PersonaError, match="not found"):
        load_persona(tmp_path, "ghost")
    (tmp_path / "nofront").mkdir()
    (tmp_path / "nofront" / "persona.md").write_text("no frontmatter")
    with pytest.raises(PersonaError, match="frontmatter"):
        load_persona(tmp_path, "nofront")


@pytest.mark.parametrize("slug", ["../etc", "a/b", "", ".hidden", "a b"])
def test_slugs_that_are_paths_are_rejected(tmp_path, slug):
    with pytest.raises(PersonaError, match="invalid persona slug"):
        load_persona(tmp_path, slug)


def test_list_personas_summaries_with_note_counts(personas_directory):
    summaries = list_personas(personas_directory)
    assert [summary.slug for summary in summaries] == ["alpha", "beta"]
    by_slug = {summary.slug: summary for summary in summaries}
    assert by_slug["alpha"].note_count == 1
    write_memory_note(load_persona(personas_directory, "beta"), "A fact", "Body.")
    counts_after = {s.slug: s.note_count for s in list_personas(personas_directory)}
    assert counts_after["beta"] == by_slug["beta"].note_count + 1


def test_missing_directory_means_no_personas(tmp_path):
    assert list_personas(tmp_path / "absent") == []
    assert load_personas(tmp_path / "absent") == {}


def test_write_memory_note_numbers_and_indexes(personas_directory):
    persona = load_persona(personas_directory, "alpha")
    note_path = write_memory_note(persona, "Prefers Short Answers!", "Keep it short.")
    assert note_path.name == "002-prefers-short-answers.md"
    assert note_path.read_text(encoding="utf-8") == "# Prefers Short Answers!\n\nKeep it short.\n"
    assert memory_index(persona).splitlines()[-1] == (
        "- [Prefers Short Answers!](002-prefers-short-answers.md)"
    )


def test_shipped_library_has_the_four_role_slugs():
    summaries = {summary.slug: summary for summary in list_personas(SHIPPED_PERSONAS)}
    assert {slug: summary.name for slug, summary in summaries.items()} == {
        "architecture": "Ravi",
        "implementation": "Tuli",
        "users-view": "Pi",
        "tests": "Codex",
    }
    assert all(summary.color.startswith("#") for summary in summaries.values())


SOURCE = MemorySource(
    room_id="room-1",
    room_title='Design: "the" room',
    thread_id="thread-1",
    thread_title="Why: colons & quotes",
)
INVALID_FILE_NAMES = ["../001-x.md", "index.md", "001-A.md", "1-x.md", "001-x.txt"]


@pytest.fixture
def alpha(personas_directory):
    return load_persona(personas_directory, "alpha")


def test_note_with_source_round_trips_frontmatter(alpha):
    title = 'Use "SQLite": always'
    note_path = write_memory_note(alpha, title, "Body text.", SOURCE)
    assert note_path.read_text(encoding="utf-8").startswith("---\nroom: room-1\n")
    note = list_memory_notes(alpha)[0]
    assert note.file == note_path.name
    assert (note.title, note.body) == (title, "Body text.")
    assert (note.room_id, note.room_title) == ("room-1", 'Design: "the" room')
    assert (note.thread_id, note.thread_title) == ("thread-1", "Why: colons & quotes")
    assert isinstance(note.written, datetime)


def test_legacy_note_lists_with_empty_source_fields(alpha):
    (note,) = list_memory_notes(alpha)
    assert (note.file, note.title) == ("001-first-fact.md", "First fact")
    assert note.body == "The owner likes short answers."
    assert note.room_id is None
    assert note.thread_title is None
    assert note.written is None


def test_notes_list_newest_first(alpha):
    write_memory_note(alpha, "Second", "b")
    write_memory_note(alpha, "Third", "c", SOURCE)
    assert [note.file for note in list_memory_notes(alpha)] == [
        "003-third.md",
        "002-second.md",
        "001-first-fact.md",
    ]


def test_missing_memory_directory_lists_nothing(tmp_path):
    persona = Persona("x", "x", "", "", "#000000", "", tmp_path / "x")
    assert list_memory_notes(persona) == []


def test_update_keeps_frontmatter_and_file_name_and_rewrites_index(alpha):
    note_path = write_memory_note(alpha, "Old title", "Old body.", SOURCE)
    before = read_memory_note(alpha, note_path.name)
    updated = update_memory_note(alpha, note_path.name, title="New: title", body="New body.")
    assert updated.file == note_path.name
    assert (updated.title, updated.body) == ("New: title", "New body.")
    assert updated.room_title == before.room_title
    assert updated.written == before.written
    index_lines = memory_index(alpha).splitlines()
    assert f"- [New: title]({note_path.name})" in index_lines
    assert not any("Old title" in line for line in index_lines)


def test_update_with_blank_title_or_body_is_rejected(alpha):
    with pytest.raises(PersonaError, match="title"):
        update_memory_note(alpha, "001-first-fact.md", title="   ")
    with pytest.raises(PersonaError, match="body"):
        update_memory_note(alpha, "001-first-fact.md", body="")
    assert read_memory_note(alpha, "001-first-fact.md").title == "First fact"


def test_delete_removes_file_and_index_line(alpha):
    note_path = write_memory_note(alpha, "Temporary", "x")
    delete_memory_note(alpha, note_path.name)
    assert not note_path.exists()
    assert memory_index(alpha) == "- [First fact](001-first-fact.md)"


def test_rebuild_index_repairs_a_corrupted_index(alpha):
    write_memory_note(alpha, "Second", "b")
    (alpha.memory_directory / "index.md").write_text("garbage\n", encoding="utf-8")
    rebuild_memory_index(alpha)
    assert memory_index(alpha).splitlines() == [
        "- [First fact](001-first-fact.md)",
        "- [Second](002-second.md)",
    ]


def test_rebuild_index_without_notes_is_empty(tmp_path):
    persona = Persona("x", "x", "", "", "#000000", "", tmp_path / "x")
    rebuild_memory_index(persona)
    assert memory_index(persona) == ""


@pytest.mark.parametrize("file_name", INVALID_FILE_NAMES)
def test_invalid_note_file_names_are_rejected(alpha, file_name):
    with pytest.raises(PersonaError, match="invalid memory note file name"):
        read_memory_note(alpha, file_name)
    with pytest.raises(PersonaError, match="invalid memory note file name"):
        update_memory_note(alpha, file_name, title="t")
    with pytest.raises(PersonaError, match="invalid memory note file name"):
        delete_memory_note(alpha, file_name)


def test_missing_valid_note_name_raises_not_found(alpha):
    with pytest.raises(MemoryNoteNotFoundError):
        read_memory_note(alpha, "099-ghost.md")
    with pytest.raises(MemoryNoteNotFoundError):
        update_memory_note(alpha, "099-ghost.md", title="t")
    with pytest.raises(MemoryNoteNotFoundError):
        delete_memory_note(alpha, "099-ghost.md")
