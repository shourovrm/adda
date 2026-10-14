"""SQLite storage: rooms, their seats and threads, messages (with FTS5 search) and sessions.

Public API (all methods are thread-safe; ids are 12-char hex strings):

    Rooms    create_room(title, folder="", mode="bidding", liveliness=6, budget_usd=0.0,
                         max_concurrent=2) -> Room   (with a first thread titled like the room)
             get_room(room_id) -> Room            (seats in position order, threads newest first)
             list_rooms() -> list[Room]           (newest first, seats and threads included)
             update_room(room_id, **fields) -> Room   (title, folder, mode, liveliness, budget_usd)
             end_room(room_id) -> Room            (archives the room; threads are not touched)
             add_spend(room_id, usd) -> Room      (atomic increment of spent_usd)
    Threads  create_thread(room_id, title) -> Thread
             get_thread(thread_id) -> Thread
             thread_summary(thread_id) -> ThreadSummary
             list_threads(room_id) -> list[ThreadSummary]   (newest first)
             newest_thread(room_id) -> Thread     (newest open thread, else the newest thread)
             rename_thread(thread_id, title) -> Thread
             end_thread(thread_id) -> Thread
    Seats    add_seat(room_id, name=..., program=..., model=None, thinking="default",
                      persona=None, instruction=None, color="#888888", enabled=True,
                      position=None) -> Seat
             get_seat(seat_id) / list_seats(room_id) -> Seat / list[Seat]
             update_seat(seat_id, **fields) -> Seat   (name, program, model, thinking, persona,
                      instruction, color, enabled, position)
             rename_seat(seat_id, new_name) -> Seat   (also rewrites messages.author)
             remove_seat(seat_id) -> None             (messages stay, session row is deleted)
             last_room_seats() -> list[SeatConfig]    (seats of the newest room that has any)
    Messages add_message(message) -> Message   (message.thread_id must belong to message.room_id)
             list_messages(room_id, last=None) -> list[Message]          (all threads of a room)
             list_thread_messages(thread_id, last=None) -> list[Message]
             search(raw_query, limit=50) -> list[SearchHit]   (room and thread titles, snippet)
    Sessions get_session(thread_id, seat_id) -> str | None   (a tool session lasts one thread)
             set_session(thread_id, seat_id, session_id) -> None

`Seat.available` and `Seat.harness_version` are not stored; the store returns the defaults
(False, None) and the server fills them in from harness detection.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from adda.models import (
    MODES,
    THINKING_LEVELS,
    Message,
    Room,
    Seat,
    SeatConfig,
    Thread,
    ThreadSummary,
    new_id,
    seat_handle,
    utc_now,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    ended_at TEXT,
    mode TEXT NOT NULL,
    folder TEXT NOT NULL DEFAULT '',
    liveliness INTEGER NOT NULL DEFAULT 6,
    budget_usd REAL NOT NULL DEFAULT 0,
    spent_usd REAL NOT NULL DEFAULT 0,
    max_concurrent INTEGER NOT NULL DEFAULT 2
);
CREATE TABLE IF NOT EXISTS room_seats (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(id),
    name TEXT NOT NULL,
    program TEXT NOT NULL,
    model TEXT,
    thinking TEXT NOT NULL DEFAULT 'default',
    persona TEXT,
    instruction TEXT,
    color TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0,
    eagerness INTEGER NOT NULL DEFAULT 5
);
CREATE INDEX IF NOT EXISTS room_seats_by_room ON room_seats(room_id, position);
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(id),
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE INDEX IF NOT EXISTS threads_by_room ON threads(room_id, created_at);
CREATE TABLE IF NOT EXISTS messages (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    room_id TEXT NOT NULL REFERENCES rooms(id),
    seat_id TEXT,
    author TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    mentions TEXT NOT NULL,
    cost_usd REAL,
    meta TEXT NOT NULL,
    thread_id TEXT REFERENCES threads(id)
);
CREATE INDEX IF NOT EXISTS messages_by_room ON messages(room_id, rowid);
CREATE INDEX IF NOT EXISTS messages_by_thread ON messages(thread_id, rowid);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, author, content='messages', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS messages_after_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text, author) VALUES (new.rowid, new.text, new.author);
END;
CREATE TABLE IF NOT EXISTS seat_sessions (
    thread_id TEXT NOT NULL REFERENCES threads(id),
    seat_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    PRIMARY KEY (thread_id, seat_id)
);
"""

# The threads table as v0.5 created it; also used by the upgrade from v0.4.
THREADS_TABLE = """
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(id),
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    ended_at TEXT
)
"""
# The thread list shows the newest message's first line, cut to this many characters.
LAST_LINE_LENGTH = 140

ROOM_UPDATABLE_FIELDS = ("title", "folder", "mode", "liveliness", "budget_usd", "max_concurrent")
SEAT_UPDATABLE_FIELDS = (
    "name",
    "program",
    "model",
    "thinking",
    "persona",
    "instruction",
    "color",
    "enabled",
    "position",
    "eagerness",
)
PROGRAM_NAMES = ("claude", "opencode", "pi", "codex")


class NotFoundError(LookupError):
    """A room, thread or seat id that does not exist in the store."""


@dataclass(frozen=True)
class SearchHit:
    message: Message
    room_title: str
    thread_title: str
    snippet: str


def _fts_query(raw_query: str) -> str:
    """Quote every whitespace-separated term so FTS5 syntax characters in user input
    (quotes, colons, NEAR, *) are searched literally instead of raising a syntax error."""
    terms = raw_query.split()
    quoted_terms = ['"' + term.replace('"', '""') + '"' for term in terms]
    return " ".join(quoted_terms)


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
    ).fetchone()
    return row is not None


def _column_names(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")}


def _upgrade_from_version_one(connection: sqlite3.Connection) -> None:
    """Turn a v0.1 database (threads, thread_id columns) into the v0.2 layout, in place.

    The tables are renamed rather than copied so message rowids, and with them the FTS5
    index, stay valid. Old seat sessions are dropped: they were keyed by a v0.1 seat name
    and no v0.2 seat can resume them.
    """
    # Explicit BEGIN: Python's sqlite3 does not open a transaction before DDL on its own,
    # and a half-done upgrade would leave a database that is neither v0.1 nor v0.2.
    connection.execute("BEGIN")
    connection.execute("ALTER TABLE threads RENAME TO rooms")
    connection.execute("ALTER TABLE rooms ADD COLUMN folder TEXT NOT NULL DEFAULT ''")
    connection.execute("ALTER TABLE rooms ADD COLUMN liveliness INTEGER NOT NULL DEFAULT 6")
    connection.execute("ALTER TABLE rooms ADD COLUMN budget_usd REAL NOT NULL DEFAULT 0")
    connection.execute("ALTER TABLE rooms ADD COLUMN spent_usd REAL NOT NULL DEFAULT 0")
    connection.execute("DROP INDEX IF EXISTS messages_by_thread")
    connection.execute("ALTER TABLE messages RENAME COLUMN thread_id TO room_id")
    connection.execute("ALTER TABLE messages ADD COLUMN seat_id TEXT")
    connection.execute("DROP TABLE seat_sessions")


def _upgrade_to_threads(connection: sqlite3.Connection) -> None:
    """Give a v0.4 database threads (SPEC v0.5 F2), in place.

    Every room gets one thread titled after the room, and all its messages move into it. A
    room that had ended in v0.4 had finished its one discussion, so its thread takes the end
    time and the room itself is reopened: in v0.5 an ended room means archived, and the owner
    should still be able to start a new thread there. Seat sessions move to the room's thread
    so seats keep resuming the conversation their tool session already holds.
    """
    connection.execute("BEGIN")
    connection.execute(THREADS_TABLE)
    connection.execute("ALTER TABLE messages ADD COLUMN thread_id TEXT REFERENCES threads(id)")
    rooms = connection.execute(
        "SELECT id, title, created_at, ended_at FROM rooms ORDER BY created_at, rowid"
    ).fetchall()
    for room_id, title, created_at, ended_at in rooms:
        thread_id = new_id()
        connection.execute(
            "INSERT INTO threads (id, room_id, title, created_at, ended_at) VALUES (?, ?, ?, ?, ?)",
            (thread_id, room_id, title, created_at, ended_at),
        )
        connection.execute(
            "UPDATE messages SET thread_id = ? WHERE room_id = ?", (thread_id, room_id)
        )
    connection.execute("UPDATE rooms SET ended_at = NULL")
    if _table_exists(connection, "seat_sessions"):
        connection.execute(
            "CREATE TABLE seat_sessions_by_thread ("
            " thread_id TEXT NOT NULL REFERENCES threads(id), seat_id TEXT NOT NULL,"
            " session_id TEXT NOT NULL, PRIMARY KEY (thread_id, seat_id))"
        )
        connection.execute(
            "INSERT INTO seat_sessions_by_thread (thread_id, seat_id, session_id)"
            " SELECT threads.id, seat_sessions.seat_id, seat_sessions.session_id"
            " FROM seat_sessions JOIN threads ON threads.room_id = seat_sessions.room_id"
        )
        connection.execute("DROP TABLE seat_sessions")
        connection.execute("ALTER TABLE seat_sessions_by_thread RENAME TO seat_sessions")


class Store:
    def __init__(self, database_path: Path | str) -> None:
        if str(database_path) != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        # The FastAPI server and its test client call the store from more than one thread;
        # the lock serialises access to the single connection.
        self._connection = sqlite3.connect(str(database_path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            # v0.1 had a `threads` table and no `rooms`; v0.5 has both.
            is_version_one = _table_exists(self._connection, "threads") and not _table_exists(
                self._connection, "rooms"
            )
            if is_version_one:
                with self._connection:
                    _upgrade_from_version_one(self._connection)
            # A database from before v0.5: rooms and messages exist, but not messages.thread_id.
            # Runs before SCHEMA, whose messages_by_thread index needs that column.
            if _table_exists(self._connection, "messages") and "thread_id" not in _column_names(
                self._connection, "messages"
            ):
                with self._connection:
                    _upgrade_to_threads(self._connection)
            self._connection.executescript(SCHEMA)
            self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """Columns added after v0.2 shipped; CREATE TABLE IF NOT EXISTS leaves old tables as is."""
        room_columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(rooms)")}
        if "max_concurrent" not in room_columns:
            with self._connection:
                self._connection.execute(
                    "ALTER TABLE rooms ADD COLUMN max_concurrent INTEGER NOT NULL DEFAULT 2"
                )
        seat_columns = {
            row["name"] for row in self._connection.execute("PRAGMA table_info(room_seats)")
        }
        if "eagerness" not in seat_columns:
            with self._connection:
                self._connection.execute(
                    "ALTER TABLE room_seats ADD COLUMN eagerness INTEGER NOT NULL DEFAULT 5"
                )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # Rooms

    def create_room(
        self,
        title: str,
        folder: str = "",
        mode: str = "bidding",
        liveliness: int = 6,
        budget_usd: float = 0.0,
        max_concurrent: int = 2,
    ) -> Room:
        _check_room_fields(
            mode=mode, liveliness=liveliness, budget_usd=budget_usd, max_concurrent=max_concurrent
        )
        room = Room(
            title=title,
            folder=folder,
            mode=mode,
            liveliness=liveliness,
            budget_usd=budget_usd,
            max_concurrent=max_concurrent,
        )
        first_thread = Thread(room_id=room.id, title=title, created_at=room.created_at)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO rooms (id, title, created_at, ended_at, mode, folder, liveliness,"
                " budget_usd, spent_usd, max_concurrent) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, 0, ?)",
                (
                    room.id,
                    room.title,
                    room.created_at.isoformat(),
                    room.mode,
                    room.folder,
                    room.liveliness,
                    room.budget_usd,
                    room.max_concurrent,
                ),
            )
            # Every room has at least one thread, so a room page always has one to open.
            self._insert_thread(first_thread)
        return self.get_room(room.id)

    def get_room(self, room_id: str) -> Room:
        with self._lock:
            return self._load_room(room_id)

    def list_rooms(self) -> list[Room]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM rooms ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
            return [self._room_with_seats(row) for row in rows]

    def update_room(self, room_id: str, **fields: Any) -> Room:
        unknown_fields = set(fields) - set(ROOM_UPDATABLE_FIELDS)
        if unknown_fields:
            raise ValueError(f"cannot update room fields {sorted(unknown_fields)}")
        _check_room_fields(**fields)
        self.get_room(room_id)
        if fields:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            with self._lock, self._connection:
                self._connection.execute(
                    f"UPDATE rooms SET {assignments} WHERE id = ?", (*fields.values(), room_id)
                )
        return self.get_room(room_id)

    def end_room(self, room_id: str) -> Room:
        self.get_room(room_id)
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE rooms SET ended_at = ? WHERE id = ?", (utc_now().isoformat(), room_id)
            )
        return self.get_room(room_id)

    def add_spend(self, room_id: str, usd: float) -> Room:
        """Add to `spent_usd` in one SQL statement so concurrent turns never lose an update."""
        self.get_room(room_id)
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE rooms SET spent_usd = spent_usd + ? WHERE id = ?", (usd, room_id)
            )
        return self.get_room(room_id)

    def _load_room(self, room_id: str) -> Room:
        row = self._connection.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"room {room_id!r} not found")
        return self._room_with_seats(row)

    def _room_with_seats(self, room_row: sqlite3.Row) -> Room:
        seat_rows = self._connection.execute(
            "SELECT * FROM room_seats WHERE room_id = ? ORDER BY position, rowid",
            (room_row["id"],),
        ).fetchall()
        ended_at = room_row["ended_at"]
        return Room(
            id=room_row["id"],
            title=room_row["title"],
            folder=room_row["folder"],
            mode=room_row["mode"],
            liveliness=room_row["liveliness"],
            budget_usd=room_row["budget_usd"],
            spent_usd=room_row["spent_usd"],
            max_concurrent=room_row["max_concurrent"],
            created_at=datetime.fromisoformat(room_row["created_at"]),
            ended_at=None if ended_at is None else datetime.fromisoformat(ended_at),
            seats=[_seat_from_row(seat_row) for seat_row in seat_rows],
            threads=self._thread_summaries(room_row["id"]),
        )

    # Threads

    def create_thread(self, room_id: str, title: str) -> Thread:
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("thread title must not be empty")
        with self._lock, self._connection:
            self._load_room(room_id)
            thread = Thread(room_id=room_id, title=clean_title)
            self._insert_thread(thread)
        return thread

    def get_thread(self, thread_id: str) -> Thread:
        with self._lock:
            return _thread_from_row(self._load_thread_row(thread_id))

    def thread_summary(self, thread_id: str) -> ThreadSummary:
        with self._lock:
            self._load_thread_row(thread_id)
            return self._summaries("threads.id = ?", (thread_id,))[0]

    def list_threads(self, room_id: str) -> list[ThreadSummary]:
        with self._lock:
            self._load_room(room_id)
            return self._thread_summaries(room_id)

    def newest_thread(self, room_id: str) -> Thread:
        """The thread a room page opens: the newest open one, else the newest one."""
        with self._lock:
            self._load_room(room_id)
            row = self._connection.execute(
                "SELECT * FROM threads WHERE room_id = ?"
                " ORDER BY ended_at IS NOT NULL, created_at DESC, rowid DESC LIMIT 1",
                (room_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"room {room_id!r} has no thread")
        return _thread_from_row(row)

    def rename_thread(self, thread_id: str, title: str) -> Thread:
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("thread title must not be empty")
        with self._lock, self._connection:
            self._load_thread_row(thread_id)
            self._connection.execute(
                "UPDATE threads SET title = ? WHERE id = ?", (clean_title, thread_id)
            )
        return self.get_thread(thread_id)

    def end_thread(self, thread_id: str) -> Thread:
        with self._lock, self._connection:
            self._load_thread_row(thread_id)
            self._connection.execute(
                "UPDATE threads SET ended_at = ? WHERE id = ?", (utc_now().isoformat(), thread_id)
            )
        return self.get_thread(thread_id)

    def _insert_thread(self, thread: Thread) -> None:
        """Caller holds the lock and an open transaction."""
        self._connection.execute(
            "INSERT INTO threads (id, room_id, title, created_at, ended_at)"
            " VALUES (?, ?, ?, ?, NULL)",
            (thread.id, thread.room_id, thread.title, thread.created_at.isoformat()),
        )

    def _load_thread_row(self, thread_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM threads WHERE id = ?", (thread_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"thread {thread_id!r} not found")
        return row

    def _thread_summaries(self, room_id: str) -> list[ThreadSummary]:
        return self._summaries("threads.room_id = ?", (room_id,))

    def _summaries(self, condition: str, parameters: tuple) -> list[ThreadSummary]:
        """Thread summaries, newest first. System messages are neither counted nor shown: the
        thread list is about what the owner and the seats said."""
        rows = self._connection.execute(
            "SELECT threads.*,"
            " (SELECT COUNT(*) FROM messages WHERE messages.thread_id = threads.id"
            "   AND messages.role != 'system') AS message_count,"
            " last.author AS last_author, last.text AS last_text"
            " FROM threads LEFT JOIN messages AS last ON last.rowid = ("
            "   SELECT MAX(rowid) FROM messages WHERE messages.thread_id = threads.id"
            "   AND messages.role != 'system')"
            f" WHERE {condition} ORDER BY threads.created_at DESC, threads.rowid DESC",
            parameters,
        ).fetchall()
        return [
            ThreadSummary(
                **_thread_from_row(row).model_dump(),
                message_count=row["message_count"],
                last_author=row["last_author"],
                last_line=_first_line(row["last_text"]),
            )
            for row in rows
        ]

    # Seats

    def add_seat(
        self,
        room_id: str,
        *,
        name: str,
        program: str,
        model: str | None = None,
        thinking: str = "default",
        persona: str | None = None,
        instruction: str | None = None,
        color: str = "#888888",
        enabled: bool = True,
        position: int | None = None,
        eagerness: int = 5,
    ) -> Seat:
        _check_seat_fields(program=program, thinking=thinking, eagerness=eagerness)
        with self._lock:
            self._load_room(room_id)
            self._check_name_free(room_id, name, ignoring_seat_id=None)
            if position is None:
                highest = self._connection.execute(
                    "SELECT MAX(position) FROM room_seats WHERE room_id = ?", (room_id,)
                ).fetchone()[0]
                position = 0 if highest is None else highest + 1
            config = SeatConfig(
                room_id=room_id,
                name=name,
                program=program,
                model=model,
                thinking=thinking,
                persona=persona,
                instruction=instruction,
                color=color,
                enabled=enabled,
                position=position,
                eagerness=eagerness,
            )
            with self._connection:
                self._connection.execute(
                    "INSERT INTO room_seats (id, room_id, name, program, model, thinking, persona,"
                    " instruction, color, enabled, position, eagerness)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        config.id,
                        config.room_id,
                        config.name,
                        config.program,
                        config.model,
                        config.thinking,
                        config.persona,
                        config.instruction,
                        config.color,
                        int(config.enabled),
                        config.position,
                        config.eagerness,
                    ),
                )
        return Seat.from_config(config)

    def get_seat(self, seat_id: str) -> Seat:
        with self._lock:
            return self._load_seat(seat_id)

    def list_seats(self, room_id: str) -> list[Seat]:
        return self.get_room(room_id).seats

    def update_seat(self, seat_id: str, **fields: Any) -> Seat:
        """Change seat fields. A new `name` also rewrites message authors (see rename_seat);
        a new `program` deletes the seat's session because another program cannot resume it."""
        unknown_fields = set(fields) - set(SEAT_UPDATABLE_FIELDS)
        if unknown_fields:
            raise ValueError(f"cannot update seat fields {sorted(unknown_fields)}")
        _check_seat_fields(**fields)
        with self._lock:
            current = self._load_seat(seat_id)
            with self._connection:
                if "name" in fields:
                    self._rename_locked(current, fields["name"])
                program_changed = "program" in fields and fields["program"] != current.program
                other_fields = {name: value for name, value in fields.items() if name != "name"}
                if other_fields:
                    if "enabled" in other_fields:
                        other_fields["enabled"] = int(other_fields["enabled"])
                    assignments = ", ".join(f"{name} = ?" for name in other_fields)
                    self._connection.execute(
                        f"UPDATE room_seats SET {assignments} WHERE id = ?",
                        (*other_fields.values(), seat_id),
                    )
                if program_changed:
                    self._connection.execute(
                        "DELETE FROM seat_sessions WHERE seat_id = ?", (seat_id,)
                    )
            return self._load_seat(seat_id)

    def rename_seat(self, seat_id: str, new_name: str) -> Seat:
        """Rename a seat and rewrite `messages.author` for its messages in its room.
        Sessions are keyed by seat id, so resume continues."""
        with self._lock:
            current = self._load_seat(seat_id)
            with self._connection:
                self._rename_locked(current, new_name)
            return self._load_seat(seat_id)

    def remove_seat(self, seat_id: str) -> None:
        """Delete the seat and its session. Its messages stay (seat_id keeps pointing at it)."""
        with self._lock:
            self._load_seat(seat_id)
            with self._connection:
                self._connection.execute("DELETE FROM seat_sessions WHERE seat_id = ?", (seat_id,))
                self._connection.execute("DELETE FROM room_seats WHERE id = ?", (seat_id,))

    def last_room_seats(self) -> list[SeatConfig]:
        """Seat configs of the newest room that has seats, for "same as last room"."""
        with self._lock:
            room_row = self._connection.execute(
                "SELECT rooms.id FROM rooms WHERE EXISTS"
                " (SELECT 1 FROM room_seats WHERE room_seats.room_id = rooms.id)"
                " ORDER BY created_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
            if room_row is None:
                return []
            seat_rows = self._connection.execute(
                "SELECT * FROM room_seats WHERE room_id = ? ORDER BY position, rowid",
                (room_row["id"],),
            ).fetchall()
        return [SeatConfig(**_seat_fields(row)) for row in seat_rows]

    def _load_seat(self, seat_id: str) -> Seat:
        row = self._connection.execute(
            "SELECT * FROM room_seats WHERE id = ?", (seat_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"seat {seat_id!r} not found")
        return _seat_from_row(row)

    def _check_name_free(self, room_id: str, name: str, ignoring_seat_id: str | None) -> None:
        """Mentions use the handle, so two seats whose handles match would be ambiguous."""
        if not name.strip():
            raise ValueError("seat name must not be empty")
        new_handle = seat_handle(name)
        rows = self._connection.execute(
            "SELECT id, name FROM room_seats WHERE room_id = ?", (room_id,)
        ).fetchall()
        for row in rows:
            if row["id"] != ignoring_seat_id and seat_handle(row["name"]) == new_handle:
                raise ValueError(f"a seat named {row['name']!r} already exists in this room")

    def _rename_locked(self, seat: Seat, new_name: str) -> None:
        """Caller holds the lock and an open transaction."""
        self._check_name_free(seat.room_id, new_name, ignoring_seat_id=seat.id)
        self._connection.execute("UPDATE room_seats SET name = ? WHERE id = ?", (new_name, seat.id))
        self._connection.execute(
            "UPDATE messages SET author = ? WHERE room_id = ? AND seat_id = ?",
            (new_name, seat.room_id, seat.id),
        )

    # Messages

    def add_message(self, message: Message) -> Message:
        with self._lock, self._connection:
            thread_row = self._load_thread_row(message.thread_id)
            if thread_row["room_id"] != message.room_id:
                raise ValueError(
                    f"thread {message.thread_id!r} belongs to room {thread_row['room_id']!r},"
                    f" not {message.room_id!r}"
                )
            self._connection.execute(
                "INSERT INTO messages (id, room_id, thread_id, seat_id, author, role, text,"
                " created_at, mentions, cost_usd, meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message.id,
                    message.room_id,
                    message.thread_id,
                    message.seat_id,
                    message.author,
                    message.role,
                    message.text,
                    message.created_at.isoformat(),
                    json.dumps(message.mentions),
                    message.cost_usd,
                    json.dumps(message.meta),
                ),
            )
        return message

    def list_messages(self, room_id: str, last: int | None = None) -> list[Message]:
        """Messages of a room in chronological order; `last` keeps only the newest N."""
        with self._lock:
            if last is None:
                rows = self._connection.execute(
                    "SELECT * FROM messages WHERE room_id = ? ORDER BY rowid", (room_id,)
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM (SELECT * FROM messages WHERE room_id = ?"
                    " ORDER BY rowid DESC LIMIT ?) ORDER BY rowid",
                    (room_id, last),
                ).fetchall()
        return [_message_from_row(row) for row in rows]

    def list_thread_messages(self, thread_id: str, last: int | None = None) -> list[Message]:
        """Messages of one thread in chronological order; `last` keeps only the newest N."""
        with self._lock:
            self._load_thread_row(thread_id)
            if last is None:
                rows = self._connection.execute(
                    "SELECT * FROM messages WHERE thread_id = ? ORDER BY rowid", (thread_id,)
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM (SELECT * FROM messages WHERE thread_id = ?"
                    " ORDER BY rowid DESC LIMIT ?) ORDER BY rowid",
                    (thread_id, last),
                ).fetchall()
        return [_message_from_row(row) for row in rows]

    def search(self, raw_query: str, limit: int = 50) -> list[SearchHit]:
        match_expression = _fts_query(raw_query)
        if not match_expression:
            return []
        with self._lock:
            rows = self._connection.execute(
                "SELECT messages.*, rooms.title AS room_title, threads.title AS thread_title,"
                " snippet(messages_fts, 0, '', '', '…', 12) AS snippet"
                " FROM messages_fts"
                " JOIN messages ON messages.rowid = messages_fts.rowid"
                " JOIN rooms ON rooms.id = messages.room_id"
                " JOIN threads ON threads.id = messages.thread_id"
                " WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?",
                (match_expression, limit),
            ).fetchall()
        return [
            SearchHit(
                message=_message_from_row(row),
                room_title=row["room_title"],
                thread_title=row["thread_title"],
                snippet=row["snippet"],
            )
            for row in rows
        ]

    # Seat sessions

    def get_session(self, thread_id: str, seat_id: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT session_id FROM seat_sessions WHERE thread_id = ? AND seat_id = ?",
                (thread_id, seat_id),
            ).fetchone()
        return None if row is None else row["session_id"]

    def set_session(self, thread_id: str, seat_id: str, session_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO seat_sessions (thread_id, seat_id, session_id) VALUES (?, ?, ?)"
                " ON CONFLICT (thread_id, seat_id) DO UPDATE SET session_id = excluded.session_id",
                (thread_id, seat_id, session_id),
            )


def _check_room_fields(**fields: Any) -> None:
    if "mode" in fields and fields["mode"] not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {fields['mode']!r}")
    if "liveliness" in fields and not 0 <= fields["liveliness"] <= 10:
        raise ValueError(f"liveliness must be 0..10, got {fields['liveliness']!r}")
    if "budget_usd" in fields and fields["budget_usd"] < 0:
        raise ValueError(f"budget_usd must not be negative, got {fields['budget_usd']!r}")
    if "max_concurrent" in fields:
        max_concurrent = fields["max_concurrent"]
        if (
            isinstance(max_concurrent, bool)
            or not isinstance(max_concurrent, int)
            or not 1 <= max_concurrent <= 4
        ):
            raise ValueError(f"max_concurrent must be 1..4, got {max_concurrent!r}")


def _check_seat_fields(**fields: Any) -> None:
    if "program" in fields and fields["program"] not in PROGRAM_NAMES:
        raise ValueError(f"program must be one of {PROGRAM_NAMES}, got {fields['program']!r}")
    if "thinking" in fields and fields["thinking"] not in THINKING_LEVELS:
        raise ValueError(f"thinking must be one of {THINKING_LEVELS}, got {fields['thinking']!r}")
    if "eagerness" in fields:
        eagerness = fields["eagerness"]
        if (
            isinstance(eagerness, bool)
            or not isinstance(eagerness, int)
            or not 0 <= eagerness <= 10
        ):
            raise ValueError(f"eagerness must be a whole number 0..10, got {eagerness!r}")


def _seat_fields(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "room_id": row["room_id"],
        "name": row["name"],
        "program": row["program"],
        "model": row["model"],
        "thinking": row["thinking"],
        "persona": row["persona"],
        "instruction": row["instruction"],
        "color": row["color"],
        "enabled": bool(row["enabled"]),
        "position": row["position"],
        "eagerness": row["eagerness"],
    }


def _seat_from_row(row: sqlite3.Row) -> Seat:
    return Seat.from_config(SeatConfig(**_seat_fields(row)))


def _thread_from_row(row: sqlite3.Row) -> Thread:
    ended_at = row["ended_at"]
    return Thread(
        id=row["id"],
        room_id=row["room_id"],
        title=row["title"],
        created_at=datetime.fromisoformat(row["created_at"]),
        ended_at=None if ended_at is None else datetime.fromisoformat(ended_at),
    )


def _first_line(text: str | None) -> str | None:
    if text is None:
        return None
    for line in text.splitlines():
        if line.strip():
            line = line.strip()
            if len(line) > LAST_LINE_LENGTH:
                return line[: LAST_LINE_LENGTH - 1].rstrip() + "…"
            return line
    return None


def _message_from_row(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"],
        room_id=row["room_id"],
        thread_id=row["thread_id"],
        seat_id=row["seat_id"],
        author=row["author"],
        role=row["role"],
        text=row["text"],
        created_at=datetime.fromisoformat(row["created_at"]),
        mentions=json.loads(row["mentions"]),
        cost_usd=row["cost_usd"],
        meta=json.loads(row["meta"]),
    )
