"""Wire and storage models (SPEC section 2). Field names are the JSON contract."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Mode = Literal["bidding", "roundrobin", "manual"]
MODES: tuple[str, ...] = ("bidding", "roundrobin", "manual")
Thinking = Literal["default", "low", "medium", "high"]
THINKING_LEVELS: tuple[str, ...] = ("default", "low", "medium", "high")
MessageRole = Literal["owner", "seat", "system"]
Program = Literal["claude", "opencode", "pi", "codex"]
# What a seat may do in the room folder: talk = no tools, look = read-only tools,
# work = read, edit, write and run commands (no approval prompts in v0.6).
Access = Literal["talk", "look", "work"]
ACCESS_LEVELS: tuple[str, ...] = ("talk", "look", "work")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def utc_now() -> datetime:
    return datetime.now(UTC)


def seat_handle(name: str) -> str:
    """Mention handle: the name with all whitespace removed, lowercased ("Scout 2" -> "scout2")."""
    return "".join(name.split()).lower()


class SeatConfig(BaseModel):
    """One seat in one room, as stored in `room_seats`."""

    id: str = Field(default_factory=new_id)  # stable even if the seat is renamed
    room_id: str
    name: str  # unique within the room
    program: Program
    model: str | None = None  # None = the harness default from settings
    thinking: Thinking = "default"
    persona: str | None = None  # persona slug
    instruction: str | None = None
    color: str = "#888888"
    enabled: bool = True
    position: int = 0
    # 0..10; shifts every bid by 0.4 * (eagerness - 5); 0 means the seat never bids.
    eagerness: int = Field(default=5, ge=0, le=10)
    access: Access | None = None  # None = the room's level


class Seat(SeatConfig):
    """What the UI gets: the config plus live facts about the harness."""

    handle: str = ""
    available: bool = False
    harness_version: str | None = None
    # The seat's own level, else the room's. Filled in by the store, which knows the room.
    effective_access: Access = "talk"

    @classmethod
    def from_config(
        cls,
        config: SeatConfig,
        available: bool = False,
        harness_version: str | None = None,
        room_access: Access = "talk",
    ) -> Seat:
        return cls(
            **config.model_dump(),
            effective_access=config.access or room_access,
            handle=seat_handle(config.name),
            available=available,
            harness_version=harness_version,
        )


class Thread(BaseModel):
    """One discussion inside a room. A seat's tool session lasts one thread."""

    id: str = Field(default_factory=new_id)
    room_id: str
    title: str
    created_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None  # set when the thread ends and memory notes are written


class ThreadSummary(Thread):
    """A thread with the facts the thread list shows. System messages are not counted."""

    message_count: int = 0
    last_author: str | None = None  # "owner" for the owner, else the seat's display name
    last_line: str | None = None  # first non-empty line of the newest message, shortened


class Room(BaseModel):
    id: str = Field(default_factory=new_id)
    title: str
    folder: str = ""  # absolute path; "" for rooms migrated from v0.1
    mode: Mode = "bidding"
    liveliness: int = Field(default=6, ge=0, le=10)
    budget_usd: float = Field(default=0.0, ge=0)  # 0 = unlimited
    spent_usd: float = 0.0
    # How many seats may compose a reply at the same time (1 reproduces strict turn-taking).
    max_concurrent: int = Field(default=2, ge=1, le=4)
    # Seconds a seat is left out of bidding after it posts a reply; 0 = off. Paces the room so
    # the owner can keep up. Mentions and wakes ignore it; an owner message clears it.
    cooldown_s: float = Field(default=0.0, ge=0)
    access: Access = "talk"  # the level of every seat that has none of its own
    created_at: datetime = Field(default_factory=utc_now)
    # Set when the room is archived. Threads end one by one; the room stays open meanwhile.
    ended_at: datetime | None = None
    seats: list[Seat] = Field(default_factory=list)
    threads: list[ThreadSummary] = Field(default_factory=list)  # newest first


class Message(BaseModel):
    id: str = Field(default_factory=new_id)
    room_id: str
    thread_id: str
    seat_id: str | None = None  # None for owner and system messages
    author: str  # display name at the time of writing; "owner" for the owner
    role: MessageRole
    text: str
    created_at: datetime = Field(default_factory=utc_now)
    mentions: list[str] = Field(default_factory=list)  # seat ids
    cost_usd: float | None = None
    # bid_score, model, latency_ms, woken_by
    # ("mention"|"all"|"wake"|"bid"|"roundrobin"|"more"|"afterthought")
    meta: dict[str, Any] = Field(default_factory=dict)


class Bid(BaseModel):
    seat: str  # seat id
    score: float = Field(ge=0, le=10)
    confidence: float = Field(default=1.0, ge=0, le=1)
    # The router's own number, before eagerness and cooldown change `score`. None until the
    # scheduler adjusts the bid.
    raw_score: float | None = Field(default=None, ge=0, le=10)


class SeatReply(BaseModel):
    text: str
    session_id: str | None = None
    cost_usd: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class Harness(BaseModel):
    """One agent program on this machine (see harnesses.detect_harnesses)."""

    program: Program
    found: bool
    path: str | None = None
    version: str | None = None
    supports_thinking: bool = False
    model_suggestions: list[str] = Field(default_factory=list)
    login_hint: str = ""
    default_model: str | None = None
    default_thinking: Thinking = "default"
    enabled: bool = True


class PersonaSummary(BaseModel):
    slug: str
    name: str
    role: str
    stance: str
    color: str
    note_count: int


class MemoryNote(BaseModel):
    """One file in a persona's memory folder. The source fields are None for notes written
    before v0.5, which had no frontmatter."""

    file: str  # e.g. "004-prefer-sqlite.md"
    title: str
    body: str
    room_id: str | None = None
    room_title: str | None = None
    thread_id: str | None = None
    thread_title: str | None = None
    written: datetime | None = None


class MemorySource(BaseModel):
    """Where a memory note came from; written into the note's frontmatter."""

    room_id: str
    room_title: str
    thread_id: str
    thread_title: str
