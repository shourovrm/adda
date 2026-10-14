"""Wire and storage models (SPEC section 2). Field names are the JSON contract."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ThreadMode = Literal["bidding", "roundrobin", "manual"]
THREAD_MODES: tuple[str, ...] = ("bidding", "roundrobin", "manual")
MessageRole = Literal["owner", "seat", "system"]
Program = Literal["claude", "opencode", "pi", "codex"]


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def utc_now() -> datetime:
    return datetime.now(UTC)


class Thread(BaseModel):
    id: str = Field(default_factory=new_id)
    title: str
    created_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    mode: ThreadMode = "bidding"


class Message(BaseModel):
    id: str = Field(default_factory=new_id)
    thread_id: str
    author: str
    role: MessageRole
    text: str
    created_at: datetime = Field(default_factory=utc_now)
    mentions: list[str] = Field(default_factory=list)
    cost_usd: float | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class Seat(BaseModel):
    name: str
    program: Program
    model: str | None = None
    color: str
    role: str
    available: bool


class Bid(BaseModel):
    seat: str
    score: float = Field(ge=0, le=10)
    confidence: float = Field(default=1.0, ge=0, le=1)


class SeatReply(BaseModel):
    text: str
    session_id: str | None = None
    cost_usd: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
