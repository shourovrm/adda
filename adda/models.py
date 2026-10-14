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

# v0.1 names, kept so modules not yet adapted still import.
ThreadMode = Mode
THREAD_MODES = MODES


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


class Seat(SeatConfig):
    """What the UI gets: the config plus live facts about the harness."""

    handle: str = ""
    available: bool = False
    harness_version: str | None = None

    @classmethod
    def from_config(
        cls, config: SeatConfig, available: bool = False, harness_version: str | None = None
    ) -> Seat:
        return cls(
            **config.model_dump(),
            handle=seat_handle(config.name),
            available=available,
            harness_version=harness_version,
        )


class Room(BaseModel):
    id: str = Field(default_factory=new_id)
    title: str
    folder: str = ""  # absolute path; "" for rooms migrated from v0.1
    mode: Mode = "bidding"
    liveliness: int = Field(default=6, ge=0, le=10)
    budget_usd: float = Field(default=0.0, ge=0)  # 0 = unlimited
    spent_usd: float = 0.0
    created_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    seats: list[Seat] = Field(default_factory=list)


# v0.1 name; untouched modules still import it.
Thread = Room


class Message(BaseModel):
    id: str = Field(default_factory=new_id)
    room_id: str
    seat_id: str | None = None  # None for owner and system messages
    author: str  # display name at the time of writing; "owner" for the owner
    role: MessageRole
    text: str
    created_at: datetime = Field(default_factory=utc_now)
    mentions: list[str] = Field(default_factory=list)  # seat ids
    cost_usd: float | None = None
    # bid_score, model, latency_ms, woken_by ("mention"|"all"|"wake"|"bid"|"roundrobin")
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def thread_id(self) -> str:
        """v0.1 name for `room_id`."""
        return self.room_id


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
