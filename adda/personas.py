"""Persona library: personas/<slug>/persona.md plus a memory/ folder of one-fact notes.

Memory belongs to the persona, so two seats on the same persona share it."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from adda.models import PersonaSummary

NOTE_FILE_PATTERN = re.compile(r"^(\d{3})-.*\.md$")
# A slug becomes a folder name and arrives from API payloads, so it must not be a path.
SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class PersonaError(ValueError):
    """A persona folder that cannot be loaded."""


@dataclass(frozen=True)
class Persona:
    """A library entry (SPEC section 3). `slug` is the folder name seats refer to; `name` is the
    display name from the frontmatter. `program` and `model` in old files are ignored."""

    slug: str
    name: str
    role: str
    stance: str
    color: str
    body: str
    directory: Path

    @property
    def memory_directory(self) -> Path:
        return self.directory / "memory"


def _split_frontmatter(text: str, source: Path) -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise PersonaError(f"{source}: missing YAML frontmatter")
    for closing_index in range(1, len(lines)):
        if lines[closing_index].strip() == "---":
            frontmatter_text = "\n".join(lines[1:closing_index])
            body = "\n".join(lines[closing_index + 1 :]).strip()
            try:
                frontmatter = yaml.safe_load(frontmatter_text) or {}
            except yaml.YAMLError as error:
                raise PersonaError(f"{source}: invalid YAML frontmatter: {error}") from error
            if not isinstance(frontmatter, dict):
                raise PersonaError(f"{source}: frontmatter must be a mapping")
            return frontmatter, body
    raise PersonaError(f"{source}: frontmatter is not closed with ---")


def load_persona(personas_directory: Path, slug: str) -> Persona:
    """Load `<personas_directory>/<slug>/persona.md`. Raises PersonaError if it is missing."""
    if not SLUG_PATTERN.match(slug):
        raise PersonaError(f"invalid persona slug {slug!r}")
    directory = personas_directory / slug
    persona_file = directory / "persona.md"
    if not persona_file.is_file():
        raise PersonaError(f"persona {slug!r} not found in {personas_directory}")
    frontmatter, body = _split_frontmatter(persona_file.read_text(encoding="utf-8"), persona_file)
    return Persona(
        slug=slug,
        name=str(frontmatter.get("name") or slug),
        role=str(frontmatter.get("role") or ""),
        stance=str(frontmatter.get("stance") or ""),
        color=str(frontmatter.get("color") or "#888888"),
        body=body,
        directory=directory,
    )


def _persona_slugs(personas_directory: Path) -> list[str]:
    if not personas_directory.is_dir():
        return []
    return sorted(
        directory.name
        for directory in personas_directory.iterdir()
        if directory.is_dir() and (directory / "persona.md").is_file()
    )


def load_personas(personas_directory: Path) -> dict[str, Persona]:
    """Every persona by slug, in slug order. A missing directory means none."""
    return {
        slug: load_persona(personas_directory, slug) for slug in _persona_slugs(personas_directory)
    }


def count_notes(persona: Persona) -> int:
    if not persona.memory_directory.is_dir():
        return 0
    return sum(
        1 for path in persona.memory_directory.iterdir() if NOTE_FILE_PATTERN.match(path.name)
    )


def list_personas(personas_directory: Path) -> list[PersonaSummary]:
    """Summaries for the library API, in slug order."""
    summaries: list[PersonaSummary] = []
    for persona in load_personas(personas_directory).values():
        summaries.append(
            PersonaSummary(
                slug=persona.slug,
                name=persona.name,
                role=persona.role,
                stance=persona.stance,
                color=persona.color,
                note_count=count_notes(persona),
            )
        )
    return summaries


def memory_index(persona: Persona) -> str:
    index_file = persona.memory_directory / "index.md"
    if not index_file.is_file():
        return ""
    return index_file.read_text(encoding="utf-8").strip()


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:48].rstrip("-") or "note"


def _next_note_number(memory_directory: Path) -> int:
    highest = 0
    for path in memory_directory.iterdir():
        match = NOTE_FILE_PATTERN.match(path.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def write_memory_note(persona: Persona, title: str, body: str) -> Path:
    """Write memory/NNN-slug.md with the next free number and append its line to index.md."""
    memory_directory = persona.memory_directory
    memory_directory.mkdir(parents=True, exist_ok=True)
    note_number = _next_note_number(memory_directory)
    note_name = f"{note_number:03d}-{_slugify(title)}.md"
    note_path = memory_directory / note_name
    note_path.write_text(f"# {title}\n\n{body.strip()}\n", encoding="utf-8")

    index_file = memory_directory / "index.md"
    existing_index = index_file.read_text(encoding="utf-8") if index_file.is_file() else ""
    if existing_index and not existing_index.endswith("\n"):
        existing_index += "\n"
    index_file.write_text(existing_index + f"- [{title}]({note_name})\n", encoding="utf-8")
    return note_path
