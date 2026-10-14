"""Opening a room folder in the desktop file manager (SPEC v0.5 F4)."""

from __future__ import annotations

import subprocess

import pytest

from adda.folders import (
    FolderError,
    FolderOpenError,
    open_in_file_manager,
    opener_command,
    prepare_room_folder,
)


@pytest.mark.parametrize(
    ("platform", "command"),
    [
        ("linux", "xdg-open"),
        ("darwin", "open"),
        ("win32", "explorer"),
        ("freebsd14", "xdg-open"),
    ],
)
def test_opener_command_by_platform(platform, command):
    assert opener_command(platform) == command


class FakePopen:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, command, **options):
        self.calls.append((command, options))
        return object()


def test_open_in_file_manager_starts_a_detached_child_and_does_not_wait(tmp_path):
    popen = FakePopen()
    open_in_file_manager(tmp_path, platform="darwin", popen=popen)
    command, options = popen.calls[0]
    assert command == ["open", str(tmp_path)]
    assert options["start_new_session"] is True
    assert options["stdin"] == options["stdout"] == options["stderr"] == subprocess.DEVNULL


def test_a_missing_opener_fails_loudly(tmp_path):
    def missing(command, **options):
        raise FileNotFoundError(command[0])

    with pytest.raises(FolderOpenError, match="xdg-open"):
        open_in_file_manager(tmp_path, platform="linux", popen=missing)


def test_prepare_room_folder_creates_it_inside_home(tmp_path):
    folder = tmp_path / "a" / "b"
    assert prepare_room_folder(str(folder), tmp_path) == folder.resolve()
    assert folder.is_dir()


@pytest.mark.parametrize("raw_path", ["", "/etc", "relative/path"])
def test_prepare_room_folder_refuses_bad_paths(tmp_path, raw_path):
    with pytest.raises(FolderError):
        prepare_room_folder(raw_path, tmp_path / "home")
