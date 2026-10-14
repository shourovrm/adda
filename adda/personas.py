"""Persona library: personas/<slug>/persona.md plus a memory/ folder of one-fact notes.

Memory belongs to the persona, so two seats on the same persona share it."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from adda.models import MemoryNote, MemorySource, PersonaSummary, utc_now

NOTE_FILE_PATTERN = re.compile(r"^(\d{3})-.*\.md$")
# Stricter than NOTE_FILE_PATTERN: this one guards file names that arrive from API payloads, so it
# must reject path separators, dots and upper case. Names produced by _slugify always match it.
MEMORY_NOTE_FILE_PATTERN = re.compile(r"^\d{3}-[a-z0-9-]+\.md$")
# A slug becomes a folder name and arrives from API payloads, so it must not be a path.
SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class PersonaError(ValueError):
    """A persona folder that cannot be loaded."""


class MemoryNoteNotFoundError(LookupError):
    """A valid memory note file name that does not exist in the persona's memory folder."""


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


def _frontmatter_text_for(source: MemorySource) -> str:
    frontmatter = {
        "room": source.room_id,
        "room_title": source.room_title,
        "thread": source.thread_id,
        "thread_title": source.thread_title,
        # Kept as a string so YAML never turns it into a timestamp type on a later rewrite.
        "written": utc_now().isoformat(),
    }
    return yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)


def write_memory_note(
    persona: Persona, title: str, body: str, source: MemorySource | None = None
) -> Path:
    """Write memory/NNN-slug.md with the next free number and append its line to index.md.

    With `source` the note starts with YAML frontmatter naming the room and thread it came from."""
    memory_directory = persona.memory_directory
    memory_directory.mkdir(parents=True, exist_ok=True)
    note_number = _next_note_number(memory_directory)
    note_name = f"{note_number:03d}-{_slugify(title)}.md"
    note_path = memory_directory / note_name
    frontmatter_block = f"---\n{_frontmatter_text_for(source)}---\n" if source else ""
    note_path.write_text(f"{frontmatter_block}# {title}\n\n{body.strip()}\n", encoding="utf-8")

    index_file = memory_directory / "index.md"
    existing_index = index_file.read_text(encoding="utf-8") if index_file.is_file() else ""
    if existing_index and not existing_index.endswith("\n"):
        existing_index += "\n"
    index_file.write_text(existing_index + f"- [{title}]({note_name})\n", encoding="utf-8")
    return note_path


def _validated_note_path(persona: Persona, file_name: str) -> Path:
    if not MEMORY_NOTE_FILE_PATTERN.match(file_name):
        raise PersonaError(f"invalid memory note file name {file_name!r}")
    return persona.memory_directory / file_name


def _existing_note_path(persona: Persona, file_name: str) -> Path:
    note_path = _validated_note_path(persona, file_name)
    if not note_path.is_file():
        raise MemoryNoteNotFoundError(f"memory note {file_name!r} not found for {persona.slug!r}")
    return note_path


def _split_note_text(text: str, source: Path) -> tuple[str, dict, str]:
    """Return (raw frontmatter block including both --- lines, parsed frontmatter, rest).

    A file that does not start with --- is a legacy note: empty block, no frontmatter."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", {}, text
    frontmatter, _ = _split_frontmatter(text, source)
    closing_index = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    raw_block = "".join(lines[: closing_index + 1])
    return raw_block, frontmatter, "".join(lines[closing_index + 1 :])


def _split_heading(content: str) -> tuple[str | None, str]:
    """Return (title, body) from the first `# ` heading line; title is None without a heading."""
    lines = content.splitlines()
    for heading_index, line in enumerate(lines):
        if line.startswith("# "):
            return line[2:].strip(), "\n".join(lines[heading_index + 1 :]).strip()
    return None, content.strip()


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _parse_written(value: object, source: Path) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):  # an unquoted timestamp in hand-edited YAML
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as error:
        raise PersonaError(f"{source}: invalid written timestamp {value!r}") from error


def _parse_note(note_path: Path) -> MemoryNote:
    text = note_path.read_text(encoding="utf-8")
    _, frontmatter, content = _split_note_text(text, note_path)
    title, body = _split_heading(content)
    return MemoryNote(
        file=note_path.name,
        title=title or note_path.stem,
        body=body,
        room_id=_optional_text(frontmatter.get("room")),
        room_title=_optional_text(frontmatter.get("room_title")),
        thread_id=_optional_text(frontmatter.get("thread")),
        thread_title=_optional_text(frontmatter.get("thread_title")),
        written=_parse_written(frontmatter.get("written"), note_path),
    )


def read_memory_note(persona: Persona, file_name: str) -> MemoryNote:
    return _parse_note(_existing_note_path(persona, file_name))


def _note_paths_in_order(persona: Persona) -> list[Path]:
    if not persona.memory_directory.is_dir():
        return []
    # The three-digit prefix makes name order the same as number order.
    return sorted(
        path
        for path in persona.memory_directory.iterdir()
        if MEMORY_NOTE_FILE_PATTERN.match(path.name)
    )


def list_memory_notes(persona: Persona) -> list[MemoryNote]:
    """Every note, newest (highest number) first."""
    return [_parse_note(path) for path in reversed(_note_paths_in_order(persona))]


def rebuild_memory_index(persona: Persona) -> Path:
    """Regenerate memory/index.md from the note files, so it cannot drift from them."""
    persona.memory_directory.mkdir(parents=True, exist_ok=True)
    index_lines = [
        f"- [{note.title}]({note.file})\n"
        for note in (_parse_note(path) for path in _note_paths_in_order(persona))
    ]
    index_file = persona.memory_directory / "index.md"
    index_file.write_text("".join(index_lines), encoding="utf-8")
    return index_file


def update_memory_note(
    persona: Persona, file_name: str, title: str | None = None, body: str | None = None
) -> MemoryNote:
    """Replace the title and/or body. The file name and frontmatter stay as they were."""
    note_path = _existing_note_path(persona, file_name)
    for field_name, value in (("title", title), ("body", body)):
        if value is not None and not value.strip():
            raise PersonaError(f"memory note {field_name} must not be blank")
    raw_frontmatter_block, _, _ = _split_note_text(note_path.read_text(encoding="utf-8"), note_path)
    current = _parse_note(note_path)
    # The title is the note's heading line, so it must stay on one line.
    new_title = " ".join(title.split()) if title is not None else current.title
    new_body = body.strip() if body is not None else current.body
    note_path.write_text(f"{raw_frontmatter_block}# {new_title}\n\n{new_body}\n", encoding="utf-8")
    rebuild_memory_index(persona)
    return _parse_note(note_path)


def delete_memory_note(persona: Persona, file_name: str) -> None:
    _existing_note_path(persona, file_name).unlink()
    rebuild_memory_index(persona)
