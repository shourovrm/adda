"""SQLite storage: threads, messages (with FTS5 search) and per-thread seat sessions."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from adda.models import THREAD_MODES, Message, Thread, utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    ended_at TEXT,
    mode TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL REFERENCES threads(id),
    author TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    mentions TEXT NOT NULL,
    cost_usd REAL,
    meta TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_by_thread ON messages(thread_id, rowid);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, author, content='messages', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS messages_after_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text, author) VALUES (new.rowid, new.text, new.author);
END;
CREATE TABLE IF NOT EXISTS seat_sessions (
    thread_id TEXT NOT NULL REFERENCES threads(id),
    seat TEXT NOT NULL,
    session_id TEXT NOT NULL,
    PRIMARY KEY (thread_id, seat)
);
"""


class NotFoundError(LookupError):
    """A thread id that does not exist in the store."""


@dataclass(frozen=True)
class SearchHit:
    message: Message
    thread_title: str
    snippet: str


def _fts_query(raw_query: str) -> str:
    """Quote every whitespace-separated term so FTS5 syntax characters in user input
    (quotes, colons, NEAR, *) are searched literally instead of raising a syntax error."""
    terms = raw_query.split()
    quoted_terms = ['"' + term.replace('"', '""') + '"' for term in terms]
    return " ".join(quoted_terms)


class Store:
    def __init__(self, database_path: Path | str) -> None:
        if str(database_path) != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        # The FastAPI server and its test client call the store from more than one thread;
        # the lock serialises access to the single connection.
        self._connection = sqlite3.connect(str(database_path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._connection:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # Threads

    def create_thread(self, title: str, mode: str = "bidding") -> Thread:
        if mode not in THREAD_MODES:
            raise ValueError(f"mode must be one of {THREAD_MODES}, got {mode!r}")
        thread = Thread(title=title, mode=mode)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO threads (id, title, created_at, ended_at, mode)"
                " VALUES (?, ?, ?, ?, ?)",
                (thread.id, thread.title, thread.created_at.isoformat(), None, thread.mode),
            )
        return thread

    def get_thread(self, thread_id: str) -> Thread:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"thread {thread_id!r} not found")
        return _thread_from_row(row)

    def list_threads(self) -> list[Thread]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM threads ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        return [_thread_from_row(row) for row in rows]

    def set_mode(self, thread_id: str, mode: str) -> Thread:
        if mode not in THREAD_MODES:
            raise ValueError(f"mode must be one of {THREAD_MODES}, got {mode!r}")
        thread = self.get_thread(thread_id)
        updated = thread.model_copy(update={"mode": mode})
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE threads SET mode = ? WHERE id = ?", (updated.mode, thread_id)
            )
        return updated

    def end_thread(self, thread_id: str) -> Thread:
        thread = self.get_thread(thread_id)
        ended_at = utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE threads SET ended_at = ? WHERE id = ?", (ended_at.isoformat(), thread_id)
            )
        return thread.model_copy(update={"ended_at": ended_at})

    # Messages

    def add_message(self, message: Message) -> Message:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO messages (id, thread_id, author, role, text, created_at, mentions,"
                " cost_usd, meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message.id,
                    message.thread_id,
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

    def list_messages(self, thread_id: str, last: int | None = None) -> list[Message]:
        """Messages of a thread in chronological order; `last` keeps only the newest N."""
        with self._lock:
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
                "SELECT messages.*, threads.title AS thread_title,"
                " snippet(messages_fts, 0, '', '', '…', 12) AS snippet"
                " FROM messages_fts"
                " JOIN messages ON messages.rowid = messages_fts.rowid"
                " JOIN threads ON threads.id = messages.thread_id"
                " WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?",
                (match_expression, limit),
            ).fetchall()
        return [
            SearchHit(
                message=_message_from_row(row),
                thread_title=row["thread_title"],
                snippet=row["snippet"],
            )
            for row in rows
        ]

    # Seat sessions

    def get_session(self, thread_id: str, seat: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT session_id FROM seat_sessions WHERE thread_id = ? AND seat = ?",
                (thread_id, seat),
            ).fetchone()
        return None if row is None else row["session_id"]

    def set_session(self, thread_id: str, seat: str, session_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO seat_sessions (thread_id, seat, session_id) VALUES (?, ?, ?)"
                " ON CONFLICT (thread_id, seat) DO UPDATE SET session_id = excluded.session_id",
                (thread_id, seat, session_id),
            )


def _thread_from_row(row: sqlite3.Row) -> Thread:
    ended_at = row["ended_at"]
    return Thread(
        id=row["id"],
        title=row["title"],
        created_at=datetime.fromisoformat(row["created_at"]),
        ended_at=None if ended_at is None else datetime.fromisoformat(ended_at),
        mode=row["mode"],
    )


def _message_from_row(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"],
        thread_id=row["thread_id"],
        author=row["author"],
        role=row["role"],
        text=row["text"],
        created_at=datetime.fromisoformat(row["created_at"]),
        mentions=json.loads(row["mentions"]),
        cost_usd=row["cost_usd"],
        meta=json.loads(row["meta"]),
    )
