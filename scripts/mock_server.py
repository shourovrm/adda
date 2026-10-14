"""Fake Adda server for UI work (SPEC section 6 and the v0.3, v0.4 and v0.5 deltas).

Run with `uv run python scripts/mock_server.py`; it listens on 127.0.0.1:8766 and keeps
everything in memory. It serves the real static pages, so the front end can be built and
checked without the real backend, without any harness binary and without spending money.
"""

from __future__ import annotations

import asyncio
import random
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

MOCK_HOST = "127.0.0.1"
MOCK_PORT = 8766
REAL_ADDA_PORT = 8765
STATIC_DIRECTORY = Path(__file__).resolve().parent.parent / "static"

OWNER_NAME = "Riad"
REPLY_DELAY_SECONDS = 1.5
BID_DELAY_SECONDS = 0.6
COST_PER_REPLY_USD = 0.031

# The folder browser works on a throwaway directory so the mock never creates folders in the
# owner's home. It stands in for the home directory the real server confines browsing to.
SANDBOX_ROOT = Path(tempfile.mkdtemp(prefix="adda-mock-home-"))
MOCK_ROOMS_DIR = SANDBOX_ROOT / "Adda-Rooms"
for sample_folder in ("Adda-Rooms", "repos/adda", "repos/notes", "Documents", "Downloads"):
    (SANDBOX_ROOT / sample_folder).mkdir(parents=True, exist_ok=True)

EAGERNESS_BID_PER_STEP = 0.4
NEUTRAL_EAGERNESS = 5
PASS_PROBABILITY = 0.4
THRESHOLD_FLOOR = 2.5
MORE_PROBABILITY = 0.3
AFTERTHOUGHT_DELAY_SECONDS = 25
AFTERTHOUGHT_TICKS = 3

SEAT_COLORS = ["#CF5A26", "#2A8F5E", "#D6409F", "#3E7FD6", "#8A6BD1", "#B08900"]


def now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.isoformat()


def make_handle(name: str) -> str:
    return re.sub(r"\s+", "", name).lower()


def make_id() -> str:
    return f"{random.getrandbits(48):012x}"


# ---------------------------------------------------------------------------
# Canned library data
# ---------------------------------------------------------------------------

HARNESSES: list[dict[str, Any]] = [
    {
        "program": "claude",
        "found": True,
        "path": "/home/riad/.local/bin/claude",
        "version": "2.1.296",
        "supports_thinking": True,
        "model_suggestions": [
            "fable",
            "opus",
            "sonnet",
            "haiku",
            "claude-fable-5-1",
            "claude-opus-5-5",
            "claude-sonnet-5-5",
            "claude-haiku-5-5",
        ],
        "login_hint": "Max login; never an API key",
        "default_model": "sonnet",
        "default_thinking": "medium",
        "enabled": True,
    },
    {
        "program": "opencode",
        "found": True,
        "path": "/home/riad/.opencode/bin/opencode",
        "version": "1.17.7",
        "supports_thinking": True,
        "model_suggestions": [
            "deepseek/deepseek-flash",
            "deepseek/deepseek-v4-pro",
            "opencode-go/glm-5.3-flash",
            "openrouter/z-ai/glm-5.3-flash",
            "openrouter/moonshotai/kimi-k3",
            "deepseek/deepseek-v4-flash",
            "opencode-go/kimi-k3",
            "opencode-go/deepseek-v4-pro",
        ],
        "login_hint": "DeepSeek key from the shell",
        "default_model": "deepseek/deepseek-flash",
        "default_thinking": "default",
        "enabled": True,
    },
    {
        "program": "pi",
        "found": True,
        "path": "/home/riad/.npm-global/bin/pi",
        "version": "1.1.0",
        "supports_thinking": True,
        "model_suggestions": [
            "openrouter/z-ai/glm-5.3-flash",
            "openrouter/deepseek/deepseek-v4-flash",
            "openrouter/moonshotai/kimi-k3",
            "z-ai/glm-5.3-flash",
        ],
        "login_hint": "OpenRouter key",
        "default_model": "z-ai/glm-5.3-flash",
        "default_thinking": "default",
        "enabled": True,
    },
    {
        "program": "codex",
        "found": False,
        "path": None,
        "version": None,
        "supports_thinking": False,
        "model_suggestions": ["gpt-5-codex"],
        "login_hint": "ChatGPT plan or API key",
        "default_model": "gpt-5-codex",
        "default_thinking": "default",
        "enabled": False,
    },
]

PERSONAS: list[dict[str, Any]] = [
    {
        "slug": "architecture",
        "name": "Ravi",
        "role": "architecture",
        "stance": "Store first",
        "color": "#CF5A26",
        "note_count": 4,
    },
    {
        "slug": "implementation",
        "name": "Tuli",
        "role": "implementation",
        "stance": "Smallest diff",
        "color": "#2A8F5E",
        "note_count": 4,
    },
    {
        "slug": "users-view",
        "name": "Pi",
        "role": "user's view",
        "stance": "Ask what breaks",
        "color": "#D6409F",
        "note_count": 4,
    },
    {
        "slug": "tests",
        "name": "Codex",
        "role": "tests",
        "stance": "Prove it",
        "color": "#3E7FD6",
        "note_count": 1,
    },
]

SETTINGS: dict[str, Any] = {
    "owner": {
        "name": OWNER_NAME,
        "line": "Owner of Adda. Prefers short answers and a decision at the end.",
    },
    "folders": {
        "rooms_dir": str(MOCK_ROOMS_DIR),
        "data_dir": "~/.adda",
        "personas_dir": "~/repos/adda/personas",
    },
    "router": {
        "kind": "jev",
        "provider": "openrouter",
        "model": "",
        "threshold_floor": THRESHOLD_FLOOR,
        "window": 20,
    },
    "room_defaults": {
        "liveliness": 6,
        "budget_usd": 1.0,
        "mode": "bidding",
        "max_concurrent": 2,
        "cooldown_s": 0,
        "seat_timeout_s": 180,
        "max_auto_turns": 40,
    },
    "harness": {
        "claude": {"model": "sonnet", "thinking": "medium", "enabled": True, "extra_models": []},
        "opencode": {
            "model": "deepseek/deepseek-flash",
            "thinking": "default",
            "enabled": True,
            "extra_models": [],
        },
        "pi": {"model": "", "thinking": "default", "enabled": True, "extra_models": []},
        "codex": {
            "model": "gpt-5-codex",
            "thinking": "default",
            "enabled": False,
            "extra_models": [],
        },
    },
    "keys": {"openrouter": True, "typesafe": False, "deepseek": True},
    "env_overrides": ["folders.data_dir"],
}

CANNED_MORE = [
    "One more thing on that: whatever we pick, write the decision down before we move on.",
    "Continuing: the cheapest check is a ten-line script, so run it before we argue further.",
]
CANNED_REPLIES = [
    "I agree with the direction, and the cost is lower than it looks. "
    "The thread is already in SQLite, "
    "so a crash loses only the note step.\n\n"
    "### What each option costs later\n\n"
    "| Option | Crash cost | Code it adds | Verdict |\n"
    "| :--- | :---: | --- | ---: |\n"
    "| Notes early | none | revise and delete rules | no |\n"
    "| `closed` flag | one startup check | one column | yes |\n\n"
    "{other}, does the flag fit the current schema?",
    "The flag fits, but a plain boolean breaks two ways:\n\n"
    "1. `closed=0` also matches a *live* room, so a restart fires the prompt early.\n"
    "2. A crash after notes land replays the prompt and writes a second set.\n\n"
    "Make it a three-state column and the note write idempotent:\n\n"
    "```python\n"
    "async def recover_unfinished(self) -> None:\n"
    "    for room in self.store.list_rooms(closed=False):\n"
    "        notes = await self._collect_memory_notes(room.id)\n"
    "        self.store.mark_closed(room.id)\n"
    "```\n",
    "Short version:\n\n- keep the store as the source of truth\n- sessions are scratch space\n"
    "- write notes at the end, **once**\n\n{other} may disagree; see [the docs](https://example.com/docs).",
    "Nothing to add. I would ship it as it is.",
    "One risk: two seats on the same persona share its memory, so a bad note spreads. "
    "Worth a review step before notes are written.",
]

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

rooms: dict[str, dict[str, Any]] = {}
threads: dict[str, dict[str, Any]] = {}
messages_by_thread: dict[str, list[dict[str, Any]]] = {}
sockets_by_thread: dict[str, set[WebSocket]] = {}
turn_tasks: dict[str, set[asyncio.Task]] = {}
afterthought_tasks: dict[str, asyncio.Task] = {}
memory_by_persona: dict[str, list[dict[str, Any]]] = {}
message_counter = 0

# The real server validates memory file names with this pattern before touching the disk.
MEMORY_FILE_PATTERN = re.compile(r"^\d{3}-[a-z0-9-]+\.md$")
LAST_LINE_LENGTH = 160


def build_seat(room_id: str, position: int, spec: dict[str, Any]) -> dict[str, Any]:
    harness = next((h for h in HARNESSES if h["program"] == spec["program"]), None)
    if harness is None:
        raise HTTPException(422, f"unknown program {spec['program']}")
    name = spec["name"]
    return {
        "id": make_id(),
        "room_id": room_id,
        "name": name,
        "program": spec["program"],
        "model": spec.get("model") or None,
        "thinking": spec.get("thinking") or "default",
        "persona": spec.get("persona") or None,
        "instruction": spec.get("instruction") or None,
        "color": spec.get("color") or SEAT_COLORS[position % len(SEAT_COLORS)],
        "eagerness": int(spec.get("eagerness", NEUTRAL_EAGERNESS)),
        "enabled": spec.get("enabled", True),
        "position": position,
        "handle": make_handle(name),
        "available": harness["found"],
        "harness_version": harness["version"],
    }


def expand_new_seats(
    room_id: str, specs: list[dict[str, Any]], first_position: int
) -> list[dict[str, Any]]:
    seats: list[dict[str, Any]] = []
    for spec in specs:
        copies = int(spec.get("copies") or 1)
        names = (
            [spec["name"]] if copies <= 1 else [f"{spec['name']}{n}" for n in range(1, copies + 1)]
        )
        for name in names:
            seats.append(build_seat(room_id, first_position + len(seats), {**spec, "name": name}))
    return seats


def add_message(thread: dict[str, Any], role: str, author: str, text: str, **extra: Any) -> dict:
    global message_counter
    message_counter += 1
    message = {
        "id": message_counter,
        "room_id": thread["room_id"],
        "thread_id": thread["id"],
        "seat_id": extra.pop("seat_id", None),
        "author": author,
        "role": role,
        "text": text,
        "created_at": iso(extra.pop("created_at", now())),
        "mentions": extra.pop("mentions", []),
        "cost_usd": extra.pop("cost_usd", None),
        "meta": extra.pop("meta", {}),
    }
    messages_by_thread[thread["id"]].append(message)
    return message


def make_thread(room_id: str, title: str, **fields: Any) -> dict[str, Any]:
    thread = {
        "id": make_id(),
        "room_id": room_id,
        "title": title,
        "created_at": iso(fields.get("created_at", now())),
        "ended_at": fields.get("ended_at"),
    }
    threads[thread["id"]] = thread
    messages_by_thread[thread["id"]] = []
    return thread


def flatten_line(text: str) -> str:
    flat = re.sub(r"[#*`>|]", "", re.sub(r"\s+", " ", text)).strip()
    return flat if len(flat) <= LAST_LINE_LENGTH else flat[:LAST_LINE_LENGTH] + "…"


def thread_summary(thread: dict[str, Any]) -> dict[str, Any]:
    """ThreadSummary of SPEC v0.5 F2: the thread plus its count and last spoken line."""
    thread_messages = messages_by_thread[thread["id"]]
    spoken = [m for m in thread_messages if m["role"] != "system"]
    last = spoken[-1] if spoken else None
    return {
        **thread,
        "message_count": len(thread_messages),
        "last_author": last["author"] if last else None,
        "last_line": flatten_line(last["text"]) if last else None,
    }


def threads_of_room(room_id: str) -> list[dict[str, Any]]:
    """Newest first, as Room.threads is ordered."""
    owned = [t for t in threads.values() if t["room_id"] == room_id]
    return sorted(owned, key=lambda thread: thread["created_at"], reverse=True)


def room_view(room: dict[str, Any]) -> dict[str, Any]:
    return {**room, "threads": [thread_summary(t) for t in threads_of_room(room["id"])]}


def newest_open_thread(room: dict[str, Any]) -> dict[str, Any] | None:
    return next((t for t in threads_of_room(room["id"]) if not t["ended_at"]), None)


def make_room(title: str, seat_specs: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    room_id = make_id()
    room = {
        "id": room_id,
        "title": title,
        "folder": fields.get("folder") or f"/home/riad/Adda-Rooms/{slugify(title)}-{room_id[:4]}",
        "mode": fields.get("mode", "bidding"),
        "liveliness": fields.get("liveliness", 6),
        "max_concurrent": fields.get("max_concurrent", 2),
        "budget_usd": fields.get("budget_usd", 1.0),
        "spent_usd": fields.get("spent_usd", 0.0),
        "created_at": iso(fields.get("created_at", now())),
        "ended_at": fields.get("ended_at"),
        "seats": [],
    }
    room["seats"] = expand_new_seats(room_id, seat_specs, 0)
    rooms[room_id] = room
    return room


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "room"


# ---------------------------------------------------------------------------
# Persona memory (SPEC v0.5 F3)
# ---------------------------------------------------------------------------


def next_memory_file(slug: str, title: str) -> str:
    used = [int(note["file"][:3]) for note in memory_by_persona.get(slug, [])]
    return f"{(max(used) + 1 if used else 1):03d}-{slugify(title)[:40]}.md"


def write_memory_note(
    slug: str, title: str, body: str, thread: dict[str, Any] | None, written: datetime
) -> dict[str, Any]:
    room = rooms[thread["room_id"]] if thread else None
    note = {
        "file": next_memory_file(slug, title),
        "title": title,
        "body": body,
        "room_id": room["id"] if room else None,
        "room_title": room["title"] if room else None,
        "thread_id": thread["id"] if thread else None,
        "thread_title": thread["title"] if thread else None,
        "written": iso(written),
    }
    memory_by_persona.setdefault(slug, []).append(note)
    return note


def notes_newest_first(slug: str) -> list[dict[str, Any]]:
    # Legacy notes have no date; they sort last, in file order.
    notes = memory_by_persona.get(slug, [])
    return sorted(notes, key=lambda note: (note["written"] or "", note["file"]), reverse=True)


def find_memory_note(slug: str, file_name: str) -> dict[str, Any]:
    if not MEMORY_FILE_PATTERN.match(file_name):
        raise HTTPException(400, "not a memory note file name")
    if not any(persona["slug"] == slug for persona in PERSONAS):
        raise HTTPException(404, "no such persona")
    note = next((n for n in memory_by_persona.get(slug, []) if n["file"] == file_name), None)
    if note is None:
        raise HTTPException(404, "no such note")
    return note


def refresh_note_counts() -> None:
    for persona in PERSONAS:
        persona["note_count"] = len(memory_by_persona.get(persona["slug"], []))


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------


def seed_data() -> None:
    started = now() - timedelta(minutes=50)
    live = make_room(
        "Adda v0.5",
        [
            {
                "name": "Ravi",
                "program": "claude",
                "model": "sonnet",
                "thinking": "medium",
                "persona": "architecture",
            },
            {
                "name": "Tuli",
                "program": "opencode",
                "model": "deepseek/deepseek-flash",
                "persona": "implementation",
            },
            {"name": "Pi", "program": "pi", "model": "z-ai/glm-5.3-flash", "persona": "users-view"},
            {"name": "Scratch", "program": "pi", "instruction": "Answer in one line."},
        ],
        created_at=started - timedelta(days=2),
        folder="/home/riad/repos/adda",
        budget_usd=1.0,
    )
    ravi, tuli, pi_seat, _scratch = live["seats"]

    naming = make_thread(
        live["id"],
        "Name the memory folder",
        created_at=started - timedelta(days=2),
        ended_at=iso(started - timedelta(days=2) + timedelta(minutes=20)),
    )
    add_message(
        naming,
        "owner",
        "owner",
        "Where do memory notes live: next to the persona or in the data folder?",
        created_at=started - timedelta(days=2),
    )
    add_message(
        naming,
        "seat",
        "Ravi",
        "Next to the persona. Notes are part of who the persona is, and that folder is committed.",
        seat_id=ravi["id"],
        cost_usd=0.012,
        created_at=started - timedelta(days=2) + timedelta(minutes=2),
        meta={"bid_score": 7.1, "model": "sonnet", "latency_ms": 5200, "woken_by": "bid"},
    )

    recovery = make_thread(live["id"], "Crash recovery", created_at=started - timedelta(hours=5))
    add_message(
        recovery,
        "owner",
        "owner",
        "If the server dies mid-reply, what do we lose?",
        created_at=started - timedelta(hours=5),
    )
    add_message(
        recovery,
        "seat",
        "Tuli",
        "Only the reply in flight. Every finished message is already in SQLite.",
        seat_id=tuli["id"],
        cost_usd=0.004,
        created_at=started - timedelta(hours=5) + timedelta(minutes=1),
        meta={"bid_score": 6.8, "model": tuli["model"], "latency_ms": 3100, "woken_by": "bid"},
    )

    sessions = make_thread(
        live["id"], "Seat sessions per discussion or per persona?", created_at=started
    )
    add_message(
        sessions, "system", "system", "Room opened in /home/riad/repos/adda.", created_at=started
    )
    add_message(
        sessions,
        "owner",
        "owner",
        "Pi says notes are the memory and the session is a scratchpad. @all does anyone disagree, "
        "and what breaks first if we go per-discussion?",
        created_at=started + timedelta(minutes=1),
        mentions=[ravi["id"], tuli["id"], pi_seat["id"]],
    )
    clock = started + timedelta(minutes=2)
    spent = 0.016
    persona_seats = [ravi, tuli, pi_seat]
    for index in range(22):
        seat = persona_seats[index % 3]
        other = persona_seats[(index + 1) % 3]
        text = CANNED_REPLIES[index % len(CANNED_REPLIES)].replace("{other}", f"@{other['name']}")
        if index == 0:
            text = "# Crash cost\n\n" + text
        cost = round(0.004 + 0.009 * (index % 4), 4)
        spent += cost
        add_message(
            sessions,
            "seat",
            seat["name"],
            text,
            seat_id=seat["id"],
            created_at=clock,
            cost_usd=cost,
            meta={
                "bid_score": 6.5,
                "model": seat["model"],
                "latency_ms": 4000 + 700 * index,
                "woken_by": "all" if index < 3 else "bid",
            },
        )
        if index == 9:
            add_message(
                sessions,
                "system",
                "system",
                "Pi is cooling down; Ravi answers next.",
                created_at=clock,
            )
        clock += timedelta(seconds=40)
    live["spent_usd"] = round(spent, 4)

    ended = make_room(
        "Pick the OpenCode provider",
        [
            {"name": "Tuli", "program": "opencode", "persona": "implementation"},
            {"name": "Pi", "program": "pi", "persona": "users-view"},
        ],
        created_at=now() - timedelta(days=4),
        ended_at=iso(now() - timedelta(days=4) + timedelta(minutes=30)),
        spent_usd=0.42,
        folder="/srv/shared/opencode-provider",
    )
    provider = make_thread(
        ended["id"],
        "Pick the OpenCode provider",
        created_at=now() - timedelta(days=4),
        ended_at=iso(now() - timedelta(days=4) + timedelta(minutes=30)),
    )
    add_message(
        provider,
        "owner",
        "owner",
        "Which provider do we point OpenCode at?",
        created_at=now() - timedelta(days=4),
    )
    add_message(
        provider,
        "seat",
        "Tuli",
        "DeepSeek direct. It is cheapest and the key is already set.",
        seat_id=ended["seats"][0]["id"],
        cost_usd=0.003,
        created_at=now() - timedelta(days=4),
    )

    paused = make_room(
        "General chat with Pi",
        [{"name": "Pi", "program": "pi", "persona": "users-view"}],
        created_at=now() - timedelta(days=3),
        liveliness=0,
        budget_usd=0,
        spent_usd=0.01,
    )
    hello = make_thread(paused["id"], "General chat with Pi", created_at=now() - timedelta(days=3))
    add_message(hello, "owner", "owner", "Hello Pi.", created_at=now() - timedelta(days=3))
    add_message(
        hello,
        "seat",
        "Pi",
        "Hello. I only speak when you @Pi me while the room is paused.",
        seat_id=paused["seats"][0]["id"],
        cost_usd=0.0001,
        created_at=now() - timedelta(days=3),
    )

    seed_memory(naming, provider, recovery)


def seed_memory(naming: dict[str, Any], provider: dict[str, Any], recovery: dict[str, Any]) -> None:
    """Notes with and without provenance, so the drawer shows both kinds."""
    two_days_ago = now() - timedelta(days=2)
    four_days_ago = now() - timedelta(days=4)
    memory_by_persona["architecture"] = []
    memory_by_persona["architecture"].append(
        {
            "file": "001-store-is-the-source-of-truth.md",
            "title": "The store is the source of truth",
            "body": "Sessions are scratch space. Anything that must survive goes to SQLite first.",
            "room_id": None,
            "room_title": None,
            "thread_id": None,
            "thread_title": None,
            "written": None,
        }
    )
    write_memory_note(
        "architecture",
        "Notes live beside the persona",
        "Riad agreed: notes go in personas/<slug>/memory and are committed with the persona.",
        naming,
        two_days_ago + timedelta(minutes=20),
    )
    write_memory_note(
        "architecture",
        "One tool session per thread",
        "A thread gets a fresh session; the persona carries over only through notes.",
        recovery,
        now() - timedelta(hours=4),
    )
    write_memory_note(
        "implementation",
        "DeepSeek direct for OpenCode",
        "Cheapest working provider; the key is already in keys.env.",
        provider,
        four_days_ago + timedelta(minutes=30),
    )
    write_memory_note(
        "implementation",
        "Prefer one column over a new table",
        "Riad picks the smallest schema change that holds; a guarded ALTER is fine.",
        naming,
        two_days_ago + timedelta(minutes=20),
    )
    write_memory_note(
        "users-view",
        "Riad wants a decision at the end",
        "End every long answer with the one thing to do next.",
        provider,
        four_days_ago + timedelta(minutes=30),
    )
    write_memory_note(
        "users-view",
        "Ask what breaks for the owner",
        "Before agreeing to a design, name the first thing Riad would notice going wrong.",
        recovery,
        now() - timedelta(hours=4),
    )
    refresh_note_counts()


# ---------------------------------------------------------------------------
# Broadcasting and turn logic (one scheduler per thread; budget and spend live on the room)
# ---------------------------------------------------------------------------


async def broadcast(thread_id: str, frame: dict[str, Any]) -> None:
    for socket in list(sockets_by_thread.get(thread_id, ())):
        try:
            await socket.send_json(frame)
        except (RuntimeError, WebSocketDisconnect):
            sockets_by_thread[thread_id].discard(socket)


async def broadcast_to_room(room_id: str, frame: dict[str, Any]) -> None:
    """`room` and `thread` frames reach every open thread of the room (spend, seats, titles)."""
    for thread in threads_of_room(room_id):
        await broadcast(thread["id"], frame)


async def broadcast_room(room: dict[str, Any]) -> None:
    await broadcast_to_room(room["id"], {"type": "room", "room": room_view(room)})


def threshold_for(liveliness: int, automatic_turns: int) -> float:
    base = 7.5 - 0.5 * liveliness
    ramp = 0.3 - 0.025 * liveliness
    return max(THRESHOLD_FLOOR, base + ramp * automatic_turns)


def adjust_for_eagerness(raw_score: float, eagerness: int) -> float:
    """Eagerness 0 never bids; otherwise 0.4 per step away from 5, clamped to 0..10."""
    if eagerness == 0:
        return 0.0
    adjusted = raw_score + EAGERNESS_BID_PER_STEP * (eagerness - NEUTRAL_EAGERNESS)
    return round(min(10.0, max(0.0, adjusted)), 1)


def find_mentioned_seats(
    room: dict[str, Any], text: str, author_seat_id: str | None
) -> list[dict[str, Any]]:
    enabled = [s for s in room["seats"] if s["enabled"] and s["id"] != author_seat_id]
    queued: list[dict[str, Any]] = []
    for match in re.finditer(r"(?<!\w)@([A-Za-z0-9_-]+)(\*?)", text):
        token, star = match.group(1).lower(), match.group(2)
        if token == "all" and not star:
            candidates = [s for s in enabled if s["available"]]
        elif star:
            candidates = [s for s in enabled if s["handle"].startswith(token)]
        else:
            candidates = [s for s in enabled if s["handle"] == token]
        for seat in candidates:
            if seat not in queued:
                queued.append(seat)
    return queued


def over_budget(room: dict[str, Any]) -> bool:
    return room["budget_usd"] > 0 and room["spent_usd"] >= room["budget_usd"]


async def seat_speaks(
    room: dict[str, Any],
    thread: dict[str, Any],
    seat: dict[str, Any],
    woken_by: str,
    bid_score: float | None,
) -> None:
    """One reply from one seat; several of these run at once, so replies may cross."""
    thread_id = thread["id"]
    await broadcast(thread_id, {"type": "typing", "seat": seat["id"], "state": "start"})
    try:
        # A small random spread makes concurrent seats finish in a varying order.
        await asyncio.sleep(REPLY_DELAY_SECONDS + random.uniform(0, 0.8))
    finally:
        await broadcast(thread_id, {"type": "typing", "seat": seat["id"], "state": "stop"})
    others = [s for s in room["seats"] if s["id"] != seat["id"]]
    other_name = others[0]["name"] if others else "everyone"
    text = random.choice(CANNED_REPLIES).replace("{other}", f"@{other_name}")
    await post_seat_message(room, thread, seat, text, woken_by, bid_score)
    if random.random() < MORE_PROBABILITY and not over_budget(room):
        # The real scheduler strips a trailing [more] line and restarts the seat without a bid.
        await broadcast(thread_id, {"type": "typing", "seat": seat["id"], "state": "start"})
        try:
            await asyncio.sleep(REPLY_DELAY_SECONDS)
        finally:
            await broadcast(thread_id, {"type": "typing", "seat": seat["id"], "state": "stop"})
        await post_seat_message(room, thread, seat, random.choice(CANNED_MORE), "more", None)


async def post_seat_message(
    room: dict[str, Any],
    thread: dict[str, Any],
    seat: dict[str, Any],
    text: str,
    woken_by: str,
    bid_score: float | None,
) -> None:
    message = add_message(
        thread,
        "seat",
        seat["name"],
        text,
        seat_id=seat["id"],
        cost_usd=COST_PER_REPLY_USD,
        meta={
            "bid_score": bid_score,
            "model": seat["model"],
            "latency_ms": 1500,
            "woken_by": woken_by,
        },
    )
    room["spent_usd"] = round(room["spent_usd"] + COST_PER_REPLY_USD, 4)
    await broadcast(thread["id"], {"type": "message", "message": message})
    await broadcast_room(room)


async def run_bidding_round(
    room: dict[str, Any],
    thread: dict[str, Any],
    kind: str,
    automatic_turns: int,
    guarantee_speakers: int,
) -> bool:
    """Broadcast one bids frame and let the chosen seats speak at once. True if anyone spoke."""
    eligible = [s for s in room["seats"] if s["enabled"] and s["available"]]
    if not eligible:
        return False
    bids = []
    for seat_candidate in eligible:
        raw_score = round(random.uniform(2, 9), 1)
        bids.append(
            {
                "seat": seat_candidate["id"],
                "score": adjust_for_eagerness(raw_score, seat_candidate["eagerness"]),
                "raw_score": raw_score,
                "confidence": 0.7,
            }
        )
    threshold = threshold_for(room["liveliness"], automatic_turns)
    ranked = sorted(bids, key=lambda bid: bid["score"], reverse=True)
    limit = max(1, int(room.get("max_concurrent", 2)))
    chosen_bids = [bid for bid in ranked if bid["score"] >= threshold][:limit]
    # The demo needs a visible pair of speakers after an owner message, so lift the top scores.
    for bid in ranked[: min(guarantee_speakers, limit)]:
        if bid not in chosen_bids:
            bid["score"] = max(bid["score"], round(threshold + 0.5, 1))
            chosen_bids.append(bid)
    await broadcast(
        thread["id"],
        {
            "type": "bids",
            "bids": bids,
            "chosen": [bid["seat"] for bid in chosen_bids],
            "threshold": threshold,
            "router": "jev",
            "kind": kind,
        },
    )
    await asyncio.sleep(BID_DELAY_SECONDS)
    if not chosen_bids:
        return False
    chosen_ids = {bid["seat"] for bid in chosen_bids}
    await announce_pass(thread, [s for s in eligible if s["id"] not in chosen_ids])
    woken_by = "afterthought" if kind == "afterthought" else "bid"
    speaking = [
        seat_speaks(
            room,
            thread,
            next(s for s in eligible if s["id"] == bid["seat"]),
            woken_by,
            bid["score"],
        )
        for bid in chosen_bids
    ]
    await asyncio.gather(*speaking)
    return True


async def run_turns(
    room: dict[str, Any], thread: dict[str, Any], queued: list[dict[str, Any]], woken_by: str
) -> None:
    """One owner message or wake: mentioned seats at once, a bidding round, then quiet."""
    thread_id = thread["id"]
    mentioned = [] if over_budget(room) else queued
    await asyncio.gather(*(seat_speaks(room, thread, seat, woken_by, None) for seat in mentioned))

    if room["mode"] == "bidding" and room["liveliness"] > 0 and not over_budget(room):
        # The first round after an owner message always seats two voices; the second is random.
        guaranteed = 0 if queued else 2
        await run_bidding_round(room, thread, "message", 0, guaranteed)
        if not over_budget(room):
            await run_bidding_round(room, thread, "message", 1, 0)

    if over_budget(room):
        budget_notice = add_message(
            thread,
            "system",
            "system",
            f"Budget of ${room['budget_usd']:.2f} reached; raise it to continue.",
        )
        await broadcast(thread_id, {"type": "message", "message": budget_notice})
        await broadcast(
            thread_id,
            {"type": "quiet", "reason": "budget", "next_tick_in_s": None, "ticks_left": 0},
        )
    elif room["liveliness"] == 0 or room["mode"] != "bidding":
        await broadcast(
            thread_id,
            {"type": "quiet", "reason": "paused", "next_tick_in_s": None, "ticks_left": 0},
        )
    else:
        start_afterthoughts(room, thread)
        await broadcast_quiet_with_tick(thread, AFTERTHOUGHT_TICKS)


async def broadcast_quiet_with_tick(thread: dict[str, Any], ticks_left: int) -> None:
    next_tick = AFTERTHOUGHT_DELAY_SECONDS if ticks_left > 0 else None
    await broadcast(
        thread["id"],
        {
            "type": "quiet",
            "reason": "nobody",
            "next_tick_in_s": next_tick,
            "ticks_left": ticks_left,
        },
    )


async def run_afterthoughts(room: dict[str, Any], thread: dict[str, Any]) -> None:
    """Wait out each quiet delay, then hold an afterthought round; stops after the tick cap."""
    ticks_left = AFTERTHOUGHT_TICKS
    while ticks_left > 0:
        await asyncio.sleep(AFTERTHOUGHT_DELAY_SECONDS)
        if room["mode"] != "bidding" or room["liveliness"] == 0 or over_budget(room):
            return
        if thread["ended_at"]:
            return
        ticks_left -= 1
        await run_bidding_round(room, thread, "afterthought", 0, 1)
        await broadcast_quiet_with_tick(thread, ticks_left)


def start_afterthoughts(room: dict[str, Any], thread: dict[str, Any]) -> None:
    cancel_afterthoughts(thread["id"])
    afterthought_tasks[thread["id"]] = asyncio.create_task(run_afterthoughts(room, thread))


def cancel_afterthoughts(thread_id: str) -> None:
    task = afterthought_tasks.pop(thread_id, None)
    if task and not task.done():
        task.cancel()


async def announce_pass(thread: dict[str, Any], candidates: list[dict[str, Any]]) -> None:
    """Sometimes a seat that was not chosen says PASS, so the UI has a passed frame to show."""
    if candidates and random.random() < PASS_PROBABILITY:
        passer = random.choice(candidates)
        await broadcast(thread["id"], {"type": "passed", "seat": passer["id"]})


def start_turns(
    room: dict[str, Any], thread: dict[str, Any], queued: list[dict[str, Any]], woken_by: str
) -> None:
    """Owner messages do not cancel speakers (v0.4); they only reset the afterthought spell."""
    cancel_afterthoughts(thread["id"])
    task = asyncio.create_task(run_turns(room, thread, queued, woken_by))
    running = turn_tasks.setdefault(thread["id"], set())
    running.add(task)
    task.add_done_callback(running.discard)


def stop_everything(thread_id: str) -> bool:
    cancel_afterthoughts(thread_id)
    running = [task for task in turn_tasks.get(thread_id, ()) if not task.done()]
    for task in running:
        task.cancel()
    return bool(running)


# ---------------------------------------------------------------------------
# Mutations shared by REST and WebSocket
# ---------------------------------------------------------------------------

ROOM_PATCH_KEYS = {"title", "mode", "liveliness", "budget_usd", "max_concurrent"}
SEAT_PATCH_KEYS = {
    "name",
    "model",
    "thinking",
    "persona",
    "instruction",
    "color",
    "eagerness",
    "enabled",
    "position",
}


def get_room(room_id: str) -> dict[str, Any]:
    room = rooms.get(room_id)
    if room is None:
        raise HTTPException(404, "no such room")
    return room


def get_thread(thread_id: str) -> dict[str, Any]:
    thread = threads.get(thread_id)
    if thread is None:
        raise HTTPException(404, "no such thread")
    return thread


def apply_room_patch(room: dict[str, Any], patch: dict[str, Any]) -> None:
    unknown = set(patch) - ROOM_PATCH_KEYS
    if unknown:
        raise HTTPException(422, f"cannot patch {sorted(unknown)}")
    if "liveliness" in patch and not 0 <= int(patch["liveliness"]) <= 10:
        raise HTTPException(422, "liveliness must be 0..10")
    if "max_concurrent" in patch and not 1 <= int(patch["max_concurrent"]) <= 4:
        raise HTTPException(422, "max_concurrent must be 1..4")
    room.update(patch)


def apply_seat_patch(room: dict[str, Any], seat_id: str, patch: dict[str, Any]) -> None:
    seat = next((s for s in room["seats"] if s["id"] == seat_id), None)
    if seat is None:
        raise HTTPException(404, "no such seat")
    unknown = set(patch) - SEAT_PATCH_KEYS
    if unknown:
        raise HTTPException(422, f"cannot patch {sorted(unknown)}")
    if "eagerness" in patch and not 0 <= int(patch["eagerness"]) <= 10:
        raise HTTPException(422, "eagerness must be 0..10")
    if "name" in patch:
        new_name = patch["name"].strip()
        if not new_name:
            raise HTTPException(422, "name must not be empty")
        if any(s["name"] == new_name and s["id"] != seat_id for s in room["seats"]):
            raise HTTPException(422, "name already used in this room")
        for thread in threads_of_room(room["id"]):
            for message in messages_by_thread[thread["id"]]:
                if message["seat_id"] == seat_id:
                    message["author"] = new_name
        patch = {**patch, "name": new_name}
        seat["handle"] = make_handle(new_name)
    seat.update(patch)


def clean_thread_title(body: dict[str, Any]) -> str:
    title = str(body.get("title") or "").strip()
    if not title:
        raise HTTPException(422, "a thread needs a title")
    if len(title) > 200:
        raise HTTPException(422, "title is longer than 200 characters")
    return title


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Adda mock")


@app.get("/")
def rooms_page() -> FileResponse:
    return FileResponse(STATIC_DIRECTORY / "index.html")


@app.get("/room/{room_id}")
def room_page(room_id: str) -> FileResponse:
    return FileResponse(STATIC_DIRECTORY / "room.html")


@app.get("/room/{room_id}/thread/{thread_id}")
def thread_page(room_id: str, thread_id: str) -> FileResponse:
    return FileResponse(STATIC_DIRECTORY / "room.html")


@app.get("/settings")
def settings_page() -> FileResponse:
    return FileResponse(STATIC_DIRECTORY / "settings.html")


@app.get("/api/harnesses")
def list_harnesses() -> list[dict[str, Any]]:
    return HARNESSES


@app.get("/api/personas")
def list_personas() -> list[dict[str, Any]]:
    return PERSONAS


@app.get("/api/settings")
def read_settings() -> dict[str, Any]:
    return SETTINGS


@app.put("/api/settings")
async def write_settings(request: Request) -> dict[str, Any]:
    body = await request.json()
    for section in ("owner", "folders", "router", "room_defaults", "harness"):
        if section in body:
            for key, value in body[section].items():
                if isinstance(value, dict):
                    SETTINGS[section].setdefault(key, {}).update(value)
                else:
                    SETTINGS[section][key] = value
    for key_name, key_value in (body.get("keys") or {}).items():
        SETTINGS["keys"][key_name] = bool(key_value)
    for harness in HARNESSES:
        saved = SETTINGS["harness"].get(harness["program"])
        if saved:
            harness["default_model"] = saved["model"] or None
            harness["default_thinking"] = saved["thinking"]
            harness["enabled"] = saved["enabled"]
    return SETTINGS


@app.get("/api/rooms")
def list_rooms() -> list[dict[str, Any]]:
    newest_first = sorted(rooms.values(), key=lambda room: room["created_at"], reverse=True)
    return [room_view(room) for room in newest_first]


def resolve_sandbox_path(path_text: str) -> Path:
    """Empty means the rooms folder; anything outside the sandbox home is refused."""
    path = Path(path_text) if path_text else MOCK_ROOMS_DIR
    resolved = path.resolve()
    if resolved != SANDBOX_ROOT and SANDBOX_ROOT not in resolved.parents:
        raise HTTPException(400, "folder must be inside the home directory")
    return resolved


def describe_folder(path: Path) -> dict[str, Any]:
    folders = sorted(
        (child for child in path.iterdir() if child.is_dir() and not child.name.startswith(".")),
        key=lambda child: child.name.lower(),
    )
    return {
        "path": str(path),
        "parent": None if path == SANDBOX_ROOT else str(path.parent),
        "folders": [{"name": child.name, "path": str(child)} for child in folders],
    }


@app.get("/api/folders")
def list_folders(path: str = "") -> dict[str, Any]:
    resolved = resolve_sandbox_path(path)
    if not resolved.is_dir():
        raise HTTPException(404, "no such folder")
    return describe_folder(resolved)


@app.post("/api/folders")
async def create_folder(request: Request) -> dict[str, str]:
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name or "/" in name or name.startswith("."):
        raise HTTPException(422, "folder name must be a plain visible name")
    parent = resolve_sandbox_path(str(body.get("path", "")))
    new_folder = parent / name
    if new_folder.exists():
        raise HTTPException(409, "that folder already exists")
    new_folder.mkdir()
    return {"name": name, "path": str(new_folder)}


@app.get("/api/rooms/folder-suggestion")
def folder_suggestion(title: str = "") -> dict[str, str]:
    stem = f"{datetime.now().strftime('%Y%m%d')}-{slugify(title)}"
    candidate = MOCK_ROOMS_DIR / stem
    suffix = 2
    while candidate.exists():
        candidate = MOCK_ROOMS_DIR / f"{stem}-{suffix}"
        suffix += 1
    return {"folder": str(candidate)}


@app.get("/api/rooms/last-seats")
def last_seats() -> list[dict[str, Any]]:
    newest = list_rooms()[0] if rooms else None
    if newest is None:
        return []
    return [
        {
            key: seat[key]
            for key in (
                "name",
                "program",
                "model",
                "thinking",
                "persona",
                "instruction",
                "color",
                "eagerness",
            )
        }
        for seat in newest["seats"]
    ]


@app.post("/api/rooms")
async def create_room(request: Request) -> dict[str, Any]:
    body = await request.json()
    title = (body.get("title") or "").strip()
    if not title or not body.get("seats"):
        raise HTTPException(422, "a room needs a title and at least one seat")
    defaults = SETTINGS["room_defaults"]
    room = make_room(
        title,
        body["seats"],
        folder=body.get("folder"),
        mode=body.get("mode") or defaults["mode"],
        liveliness=body.get("liveliness", defaults["liveliness"]),
        budget_usd=body.get("budget_usd", defaults["budget_usd"]),
        max_concurrent=body.get("max_concurrent", defaults["max_concurrent"]),
    )
    first_thread = make_thread(room["id"], title)
    add_message(first_thread, "system", "system", f"Room opened in {room['folder']}.")
    return room_view(room)


@app.get("/api/rooms/{room_id}")
def read_room(room_id: str) -> dict[str, Any]:
    """Kept for v0.4 callers: the room plus the messages of its newest open thread."""
    room = get_room(room_id)
    newest = newest_open_thread(room)
    return {
        "room": room_view(room),
        "messages": messages_by_thread[newest["id"]] if newest else [],
    }


@app.patch("/api/rooms/{room_id}")
async def patch_room(room_id: str, request: Request) -> dict[str, Any]:
    room = get_room(room_id)
    apply_room_patch(room, await request.json())
    await broadcast_room(room)
    return room_view(room)


@app.post("/api/rooms/{room_id}/seats")
async def add_seat(room_id: str, request: Request) -> dict[str, Any]:
    room = get_room(room_id)
    room["seats"].extend(expand_new_seats(room_id, [await request.json()], len(room["seats"])))
    await broadcast_room(room)
    return room_view(room)


@app.patch("/api/rooms/{room_id}/seats/{seat_id}")
async def patch_seat(room_id: str, seat_id: str, request: Request) -> dict[str, Any]:
    room = get_room(room_id)
    apply_seat_patch(room, seat_id, await request.json())
    await broadcast_room(room)
    return room_view(room)


@app.delete("/api/rooms/{room_id}/seats/{seat_id}")
async def delete_seat(room_id: str, seat_id: str) -> dict[str, Any]:
    room = get_room(room_id)
    room["seats"] = [s for s in room["seats"] if s["id"] != seat_id]
    await broadcast_room(room)
    return room_view(room)


@app.post("/api/rooms/{room_id}/end")
async def end_room(room_id: str) -> dict[str, Any]:
    """Archives the room: every open thread ends first, each writing its notes."""
    room = get_room(room_id)
    notes: dict[str, list[str]] = {}
    for thread in threads_of_room(room_id):
        if not thread["ended_at"]:
            for seat_id, seat_notes in (await finish_thread(room, thread)).items():
                notes.setdefault(seat_id, []).extend(seat_notes)
    room["ended_at"] = iso(now())
    await broadcast_room(room)
    return {"room": room_view(room), "notes": notes}


@app.post("/api/rooms/{room_id}/open-folder")
def open_folder(room_id: str) -> dict[str, str]:
    """The real server runs xdg-open; the mock only answers, and refuses a room with no folder."""
    room = get_room(room_id)
    if not room["folder"]:
        raise HTTPException(400, "this room has no folder")
    if not room["folder"].startswith("/home/riad") and not room["folder"].startswith(
        str(SANDBOX_ROOT)
    ):
        raise HTTPException(400, "folder must be inside the home directory")
    return {"opened": room["folder"]}


@app.get("/api/rooms/{room_id}/threads")
def list_threads(room_id: str) -> list[dict[str, Any]]:
    get_room(room_id)
    return [thread_summary(thread) for thread in threads_of_room(room_id)]


@app.post("/api/rooms/{room_id}/threads")
async def create_thread(room_id: str, request: Request) -> dict[str, Any]:
    room = get_room(room_id)
    if room["ended_at"]:
        raise HTTPException(409, "this room is archived")
    thread = make_thread(room_id, clean_thread_title(await request.json()))
    # Other tabs on this room learn about the new thread through the room's thread list.
    await broadcast_room(room)
    return thread_summary(thread)


@app.get("/api/threads/{thread_id}")
def read_thread(thread_id: str) -> dict[str, Any]:
    thread = get_thread(thread_id)
    return {
        "thread": thread_summary(thread),
        "room": room_view(rooms[thread["room_id"]]),
        "messages": messages_by_thread[thread_id],
    }


@app.patch("/api/threads/{thread_id}")
async def rename_thread(thread_id: str, request: Request) -> dict[str, Any]:
    thread = get_thread(thread_id)
    thread["title"] = clean_thread_title(await request.json())
    summary = thread_summary(thread)
    await broadcast_to_room(thread["room_id"], {"type": "thread", "thread": summary})
    return summary


@app.post("/api/threads/{thread_id}/end")
async def end_thread(thread_id: str) -> dict[str, Any]:
    thread = get_thread(thread_id)
    room = rooms[thread["room_id"]]
    if thread["ended_at"]:
        raise HTTPException(409, "this thread has already ended")
    notes = await finish_thread(room, thread)
    return {"thread": thread_summary(thread), "notes": notes}


async def finish_thread(room: dict[str, Any], thread: dict[str, Any]) -> dict[str, list[str]]:
    """Ending a thread asks every seat with a persona that spoke for notes and stores them."""
    stop_everything(thread["id"])
    thread["ended_at"] = iso(now())
    spoke = {m["seat_id"] for m in messages_by_thread[thread["id"]] if m["seat_id"]}
    notes: dict[str, list[str]] = {}
    for seat in room["seats"]:
        if seat["id"] not in spoke or not seat["persona"]:
            continue
        title = f"Decided in {thread['title']}"[:80]
        body = f"{seat['name']} remembers: keep one tool session per thread."
        write_memory_note(seat["persona"], title, body, thread, now())
        notes[seat["id"]] = [body]
    refresh_note_counts()
    await broadcast_to_room(room["id"], {"type": "thread", "thread": thread_summary(thread)})
    await broadcast(thread["id"], {"type": "ended", "notes": notes})
    return notes


@app.get("/api/personas/{slug}/memory")
def list_memory(slug: str) -> list[dict[str, Any]]:
    if not any(persona["slug"] == slug for persona in PERSONAS):
        raise HTTPException(404, "no such persona")
    return notes_newest_first(slug)


@app.put("/api/personas/{slug}/memory/{file_name}")
async def update_memory(slug: str, file_name: str, request: Request) -> dict[str, Any]:
    note = find_memory_note(slug, file_name)
    body = await request.json()
    if "title" in body:
        title = str(body["title"]).strip()
        if not title:
            raise HTTPException(422, "a note needs a title")
        note["title"] = title
    if "body" in body:
        note["body"] = str(body["body"]).strip()
    return note


@app.delete("/api/personas/{slug}/memory/{file_name}")
def delete_memory(slug: str, file_name: str) -> dict[str, str]:
    note = find_memory_note(slug, file_name)
    memory_by_persona[slug].remove(note)
    refresh_note_counts()
    return {"deleted": file_name}


@app.get("/api/rooms/{room_id}/memory")
def room_memory(room_id: str) -> dict[str, list[dict[str, Any]]]:
    get_room(room_id)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for persona in PERSONAS:
        from_room = [n for n in notes_newest_first(persona["slug"]) if n["room_id"] == room_id]
        if from_room:
            grouped[persona["slug"]] = from_room
    return grouped


@app.get("/api/search")
def search(q: str = "") -> list[dict[str, Any]]:
    needle = q.strip().lower()
    results = []
    for thread_id, thread_messages in messages_by_thread.items():
        thread = threads[thread_id]
        for message in thread_messages:
            if needle and needle in message["text"].lower():
                results.append(
                    {
                        "message": message,
                        "room_title": rooms[thread["room_id"]]["title"],
                        "thread_id": thread_id,
                        "thread_title": thread["title"],
                        "snippet": message["text"][:120],
                    }
                )
    return results[:30]


async def handle_client_frame(
    room: dict[str, Any], thread: dict[str, Any], frame: dict[str, Any]
) -> None:
    kind = frame.get("type")
    if kind == "say":
        await handle_say(room, thread, str(frame.get("text", "")).strip())
    elif kind == "wake":
        seat = next((s for s in room["seats"] if s["id"] == frame.get("seat")), None)
        if seat is None:
            raise HTTPException(404, "no such seat")
        refuse_if_unavailable(room, thread)
        start_turns(room, thread, [seat], "wake")
    elif kind == "wake_all":
        refuse_if_unavailable(room, thread)
        enabled = [s for s in room["seats"] if s["enabled"] and s["available"]]
        start_turns(room, thread, enabled, "wake")
    elif kind == "stop":
        stop_everything(thread["id"])
        await broadcast(
            thread["id"],
            {"type": "quiet", "reason": "nobody", "next_tick_in_s": None, "ticks_left": 0},
        )
    elif kind == "end":
        if thread["ended_at"]:
            raise HTTPException(409, "this thread has already ended")
        await finish_thread(room, thread)
    elif kind == "room":
        apply_room_patch(room, frame.get("patch") or {})
        await broadcast_room(room)
    elif kind == "seat":
        apply_seat_patch(room, frame.get("seat"), frame.get("patch") or {})
        await broadcast_room(room)
    else:
        raise HTTPException(422, f"unknown frame type {kind}")


def refuse_if_unavailable(room: dict[str, Any], thread: dict[str, Any]) -> None:
    if room["ended_at"]:
        raise HTTPException(409, "this room is archived")
    if thread["ended_at"]:
        raise HTTPException(409, "this thread has ended")
    if over_budget(room):
        raise HTTPException(
            409, f"Budget of ${room['budget_usd']:.2f} reached; raise it to continue."
        )


async def handle_say(room: dict[str, Any], thread: dict[str, Any], text: str) -> None:
    if not text:
        return
    refuse_if_unavailable(room, thread)
    queued = find_mentioned_seats(room, text, None)
    message = add_message(thread, "owner", "owner", text, mentions=[s["id"] for s in queued])
    await broadcast(thread["id"], {"type": "message", "message": message})
    woken_by = "all" if re.search(r"(?<!\w)@all\b", text, re.I) else "mention"
    start_turns(room, thread, queued, woken_by)


@app.websocket("/ws/threads/{thread_id}")
async def thread_socket(socket: WebSocket, thread_id: str) -> None:
    await socket.accept()
    thread = threads.get(thread_id)
    if thread is None:
        await socket.close(code=4404)
        return
    room = rooms[thread["room_id"]]
    sockets_by_thread.setdefault(thread_id, set()).add(socket)
    await socket.send_json(
        {
            "type": "hello",
            "room": room_view(room),
            "thread": thread_summary(thread),
            "messages": messages_by_thread[thread_id],
            "owner": {"name": SETTINGS["owner"]["name"]},
            "router": "jev",
        }
    )
    try:
        while True:
            frame = await socket.receive_json()
            try:
                await handle_client_frame(room, thread, frame)
            except HTTPException as problem:
                await socket.send_json({"type": "error", "text": str(problem.detail)})
    except WebSocketDisconnect:
        pass
    finally:
        sockets_by_thread[thread_id].discard(socket)


app.mount("/static", StaticFiles(directory=STATIC_DIRECTORY), name="static")
seed_data()

if __name__ == "__main__":
    assert MOCK_PORT != REAL_ADDA_PORT, "the mock must never bind the real Adda port"
    uvicorn.run(app, host=MOCK_HOST, port=MOCK_PORT, log_level="warning")
