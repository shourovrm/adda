"""Folder browsing for the room-folder picker, and default room folder names (SPEC v0.3 D3, D4).

The browser only shows folders inside the owner's home directory, because the UI is a folder
picker for a personal tool and has no reason to list system directories. Paths are resolved
(symlinks followed) before the check so a link inside home cannot lead outside it.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any


class FolderError(ValueError):
    """The path is not allowed, does not exist, or the name is not a plain folder name."""


class FolderExistsError(FolderError):
    """A folder with that name already exists."""


def slugify_title(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-")
    return slug or "room"


def default_room_folder(rooms_dir: Path, title: str, today: date) -> Path:
    """`<rooms_dir>/<YYYYMMDD>-<slug>`, with `-2`, `-3`, ... added while that folder exists."""
    base_name = f"{today:%Y%m%d}-{slugify_title(title)}"
    candidate = rooms_dir / base_name
    attempt = 2
    while candidate.exists():
        candidate = rooms_dir / f"{base_name}-{attempt}"
        attempt += 1
    return candidate


def _resolve_inside_home(raw_path: str, home: Path) -> Path:
    home = home.resolve()
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise FolderError(f"path must be absolute, got {raw_path!r}")
    resolved = path.resolve()
    if resolved != home and home not in resolved.parents:
        raise FolderError(f"path must be inside {home}, got {str(resolved)!r}")
    return resolved


def _entry(path: Path) -> dict[str, str]:
    return {"name": path.name, "path": str(path)}


def list_folders(raw_path: str, home: Path, rooms_dir: Path) -> dict[str, Any]:
    """Subfolders of `raw_path` (empty means rooms_dir, or home when rooms_dir is outside it)."""
    home = home.resolve()
    if raw_path.strip():
        path = _resolve_inside_home(raw_path.strip(), home)
    else:
        try:
            path = _resolve_inside_home(str(rooms_dir), home)
        except FolderError:
            path = home
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise FolderError(f"{str(path)!r} is not a folder")
    try:
        children = [
            child for child in path.iterdir() if not child.name.startswith(".") and child.is_dir()
        ]
    except OSError as error:
        raise FolderError(f"cannot read {path}: {error}") from error
    children.sort(key=lambda child: child.name.lower())
    return {
        "path": str(path),
        "parent": None if path == home else str(path.parent),
        "folders": [_entry(child) for child in children],
    }


def create_folder(raw_path: str, name: str, home: Path) -> dict[str, str]:
    """Create `name` inside `raw_path`; FolderExistsError if it is already there."""
    clean_name = name.strip()
    if not clean_name or clean_name in (".", "..") or "/" in clean_name or "\0" in clean_name:
        raise FolderError(f"{name!r} is not a plain folder name")
    parent = _resolve_inside_home(raw_path.strip(), home)
    if not parent.is_dir():
        raise FolderError(f"{str(parent)!r} is not a folder")
    new_folder = parent / clean_name
    try:
        new_folder.mkdir()
    except FileExistsError as error:
        raise FolderExistsError(f"{str(new_folder)!r} already exists") from error
    except OSError as error:
        raise FolderError(f"cannot create {new_folder}: {error}") from error
    return _entry(new_folder)
