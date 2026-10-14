import shutil
from pathlib import Path

import pytest

from adda.personas import (
    PersonaError,
    list_personas,
    load_persona,
    load_personas,
    memory_index,
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
