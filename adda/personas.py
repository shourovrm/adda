"""Persona folders: personas/<name>/persona.md plus a memory/ folder of one-fact notes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

PROGRAMS = ("claude", "opencode", "pi", "codex")
NOTE_FILE_PATTERN = re.compile(r"^(\d{3})-.*\.md$")


class PersonaError(ValueError):
    """A persona folder that cannot be loaded."""


@dataclass(frozen=True)
class Persona:
    name: str
    program: str
    model: str | None
    color: str
    role: str
    stance: str
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


def load_persona(directory: Path) -> Persona:
    persona_file = directory / "persona.md"
    frontmatter, body = _split_frontmatter(persona_file.read_text(encoding="utf-8"), persona_file)
    name = str(frontmatter.get("name") or "")
    if name != directory.name:
        raise PersonaError(
            f"{persona_file}: name {name!r} must equal the folder name {directory.name!r}"
        )
    program = frontmatter.get("program")
    if program not in PROGRAMS:
        raise PersonaError(f"{persona_file}: program must be one of {PROGRAMS}, got {program!r}")
    model = frontmatter.get("model")
    return Persona(
        name=name,
        program=program,
        model=None if model is None else str(model),
        color=str(frontmatter.get("color") or "#888888"),
        role=str(frontmatter.get("role") or ""),
        stance=str(frontmatter.get("stance") or ""),
        body=body,
        directory=directory,
    )


def load_personas(personas_directory: Path) -> dict[str, Persona]:
    """Load every `<name>/persona.md`, in folder-name order. A missing directory means none."""
    if not personas_directory.is_dir():
        return {}
    personas: dict[str, Persona] = {}
    for directory in sorted(personas_directory.iterdir()):
        if directory.is_dir() and (directory / "persona.md").is_file():
            persona = load_persona(directory)
            personas[persona.name] = persona
    return personas


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
    index_file.write_text(existing_index + f"- {note_name}: {title}\n", encoding="utf-8")
    return note_path
