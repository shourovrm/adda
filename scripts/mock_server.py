"""Fake Adda server for UI work (SPEC section 6 and the v0.3 delta).

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
messages_by_room: dict[str, list[dict[str, Any]]] = {}
sockets_by_room: dict[str, set[WebSocket]] = {}
turn_tasks: dict[str, set[asyncio.Task]] = {}
afterthought_tasks: dict[str, asyncio.Task] = {}
message_counter = 0


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


def add_message(room_id: str, role: str, author: str, text: str, **extra: Any) -> dict[str, Any]:
    global message_counter
    message_counter += 1
    message = {
        "id": message_counter,
        "room_id": room_id,
        "seat_id": extra.pop("seat_id", None),
        "author": author,
        "role": role,
        "text": text,
        "created_at": iso(extra.pop("created_at", now())),
        "mentions": extra.pop("mentions", []),
        "cost_usd": extra.pop("cost_usd", None),
        "meta": extra.pop("meta", {}),
    }
    messages_by_room.setdefault(room_id, []).append(message)
    return message


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
    messages_by_room[room_id] = []
    return room


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "room"


def seed_data() -> None:
    started = now() - timedelta(minutes=50)
    live = make_room(
        "Should Adda keep seat sessions per discussion or per persona?",
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
        ],
        created_at=started,
        folder="/home/riad/repos/adda",
        budget_usd=1.0,
    )
    ravi, tuli, pi_seat = live["seats"]
    live_id = live["id"]
    add_message(
        live_id, "system", "system", "Room opened in /home/riad/repos/adda.", created_at=started
    )
    add_message(
        live_id,
        "owner",
        "owner",
        "Pi says notes are the memory and the session is a scratchpad. @all does anyone disagree, "
        "and what breaks first if we go per-discussion?",
        created_at=started + timedelta(minutes=1),
        mentions=[ravi["id"], tuli["id"], pi_seat["id"]],
    )
    clock = started + timedelta(minutes=2)
    spent = 0.0
    for index in range(22):
        seat = live["seats"][index % 3]
        other = live["seats"][(index + 1) % 3]
        text = CANNED_REPLIES[index % len(CANNED_REPLIES)].replace("{other}", f"@{other['name']}")
        if index == 0:
            text = "# Crash cost\n\n" + text
        cost = round(0.004 + 0.009 * (index % 4), 4)
        spent += cost
        add_message(
            live_id,
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
                live_id,
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
        created_at=now() - timedelta(days=1),
        ended_at=iso(now() - timedelta(days=1) + timedelta(minutes=30)),
        spent_usd=0.42,
        folder="",
    )
    add_message(
        ended["id"],
        "owner",
        "owner",
        "Which provider do we point OpenCode at?",
        created_at=now() - timedelta(days=1),
    )
    add_message(
        ended["id"],
        "seat",
        "Tuli",
        "DeepSeek direct. It is cheapest and the key is already set.",
        seat_id=ended["seats"][0]["id"],
        cost_usd=0.003,
        created_at=now() - timedelta(days=1),
    )

    paused = make_room(
        "General chat with Pi",
        [{"name": "Pi", "program": "pi", "persona": "users-view"}],
        created_at=now() - timedelta(hours=3),
        liveliness=0,
        budget_usd=0,
        spent_usd=0.01,
    )
    add_message(paused["id"], "owner", "owner", "Hello Pi.", created_at=now() - timedelta(hours=3))
    add_message(
        paused["id"],
        "seat",
        "Pi",
        "Hello. I only speak when you @Pi me while the room is paused.",
        seat_id=paused["seats"][0]["id"],
        cost_usd=0.0001,
        created_at=now() - timedelta(hours=3),
    )


# ---------------------------------------------------------------------------
# Broadcasting and turn logic
# ---------------------------------------------------------------------------


async def broadcast(room_id: str, frame: dict[str, Any]) -> None:
    for socket in list(sockets_by_room.get(room_id, ())):
        try:
            await socket.send_json(frame)
        except (RuntimeError, WebSocketDisconnect):
            sockets_by_room[room_id].discard(socket)


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
    room: dict[str, Any], seat: dict[str, Any], woken_by: str, bid_score: float | None
) -> None:
    """One reply from one seat; several of these run at once, so replies may cross."""
    room_id = room["id"]
    await broadcast(room_id, {"type": "typing", "seat": seat["id"], "state": "start"})
    try:
        # A small random spread makes concurrent seats finish in a varying order.
        await asyncio.sleep(REPLY_DELAY_SECONDS + random.uniform(0, 0.8))
    finally:
        await broadcast(room_id, {"type": "typing", "seat": seat["id"], "state": "stop"})
    others = [s for s in room["seats"] if s["id"] != seat["id"]]
    other_name = others[0]["name"] if others else "everyone"
    text = random.choice(CANNED_REPLIES).replace("{other}", f"@{other_name}")
    await post_seat_message(room, seat, text, woken_by, bid_score)
    if random.random() < MORE_PROBABILITY and not over_budget(room):
        # The real scheduler strips a trailing [more] line and restarts the seat without a bid.
        await broadcast(room_id, {"type": "typing", "seat": seat["id"], "state": "start"})
        try:
            await asyncio.sleep(REPLY_DELAY_SECONDS)
        finally:
            await broadcast(room_id, {"type": "typing", "seat": seat["id"], "state": "stop"})
        await post_seat_message(room, seat, random.choice(CANNED_MORE), "more", None)


async def post_seat_message(
    room: dict[str, Any], seat: dict[str, Any], text: str, woken_by: str, bid_score: float | None
) -> None:
    room_id = room["id"]
    message = add_message(
        room_id,
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
    await broadcast(room_id, {"type": "message", "message": message})
    await broadcast(room_id, {"type": "room", "room": room})


async def run_bidding_round(
    room: dict[str, Any], kind: str, automatic_turns: int, guarantee_speakers: int
) -> bool:
    """Broadcast one bids frame and let the chosen seats speak at once. True if anyone spoke."""
    room_id = room["id"]
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
        room_id,
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
    await announce_pass(room, [s for s in eligible if s["id"] not in chosen_ids])
    woken_by = "afterthought" if kind == "afterthought" else "bid"
    speaking = [
        seat_speaks(
            room, next(s for s in eligible if s["id"] == bid["seat"]), woken_by, bid["score"]
        )
        for bid in chosen_bids
    ]
    await asyncio.gather(*speaking)
    return True


async def run_turns(room: dict[str, Any], queued: list[dict[str, Any]], woken_by: str) -> None:
    """One owner message or wake: mentioned seats at once, a bidding round, then quiet."""
    room_id = room["id"]
    mentioned = [] if over_budget(room) else queued
    await asyncio.gather(*(seat_speaks(room, seat, woken_by, None) for seat in mentioned))

    if room["mode"] == "bidding" and room["liveliness"] > 0 and not over_budget(room):
        # The first round after an owner message always seats two voices; the second is random.
        guaranteed = 0 if queued else 2
        await run_bidding_round(room, "message", 0, guaranteed)
        if not over_budget(room):
            await run_bidding_round(room, "message", 1, 0)

    if over_budget(room):
        add_budget_notice = add_message(
            room_id,
            "system",
            "system",
            f"Budget of ${room['budget_usd']:.2f} reached; raise it to continue.",
        )
        await broadcast(room_id, {"type": "message", "message": add_budget_notice})
        await broadcast(
            room_id, {"type": "quiet", "reason": "budget", "next_tick_in_s": None, "ticks_left": 0}
        )
    elif room["liveliness"] == 0 or room["mode"] != "bidding":
        await broadcast(
            room_id, {"type": "quiet", "reason": "paused", "next_tick_in_s": None, "ticks_left": 0}
        )
    else:
        start_afterthoughts(room)
        await broadcast_quiet_with_tick(room, AFTERTHOUGHT_TICKS)


async def broadcast_quiet_with_tick(room: dict[str, Any], ticks_left: int) -> None:
    next_tick = AFTERTHOUGHT_DELAY_SECONDS if ticks_left > 0 else None
    await broadcast(
        room["id"],
        {
            "type": "quiet",
            "reason": "nobody",
            "next_tick_in_s": next_tick,
            "ticks_left": ticks_left,
        },
    )


async def run_afterthoughts(room: dict[str, Any]) -> None:
    """Wait out each quiet delay, then hold an afterthought round; stops after the tick cap."""
    ticks_left = AFTERTHOUGHT_TICKS
    while ticks_left > 0:
        await asyncio.sleep(AFTERTHOUGHT_DELAY_SECONDS)
        if room["mode"] != "bidding" or room["liveliness"] == 0 or over_budget(room):
            return
        ticks_left -= 1
        await run_bidding_round(room, "afterthought", 0, 1)
        await broadcast_quiet_with_tick(room, ticks_left)


def start_afterthoughts(room: dict[str, Any]) -> None:
    cancel_afterthoughts(room["id"])
    afterthought_tasks[room["id"]] = asyncio.create_task(run_afterthoughts(room))


def cancel_afterthoughts(room_id: str) -> None:
    task = afterthought_tasks.pop(room_id, None)
    if task and not task.done():
        task.cancel()


async def announce_pass(room: dict[str, Any], candidates: list[dict[str, Any]]) -> None:
    """Sometimes a seat that was not chosen says PASS, so the UI has a passed frame to show."""
    if candidates and random.random() < PASS_PROBABILITY:
        passer = random.choice(candidates)
        await broadcast(room["id"], {"type": "passed", "seat": passer["id"]})


def start_turns(room: dict[str, Any], queued: list[dict[str, Any]], woken_by: str) -> None:
    """Owner messages do not cancel speakers (v0.4); they only reset the afterthought spell."""
    cancel_afterthoughts(room["id"])
    task = asyncio.create_task(run_turns(room, queued, woken_by))
    running = turn_tasks.setdefault(room["id"], set())
    running.add(task)
    task.add_done_callback(running.discard)


def stop_everything(room_id: str) -> bool:
    cancel_afterthoughts(room_id)
    running = [task for task in turn_tasks.get(room_id, ()) if not task.done()]
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
        for message in messages_by_room[room["id"]]:
            if message["seat_id"] == seat_id:
                message["author"] = new_name
        patch = {**patch, "name": new_name}
        seat["handle"] = make_handle(new_name)
    seat.update(patch)


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
    return sorted(rooms.values(), key=lambda room: room["created_at"], reverse=True)


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
    add_message(room["id"], "system", "system", f"Room opened in {room['folder']}.")
    return room


@app.get("/api/rooms/{room_id}")
def read_room(room_id: str) -> dict[str, Any]:
    return {"room": get_room(room_id), "messages": messages_by_room[room_id]}


@app.patch("/api/rooms/{room_id}")
async def patch_room(room_id: str, request: Request) -> dict[str, Any]:
    room = get_room(room_id)
    apply_room_patch(room, await request.json())
    await broadcast(room_id, {"type": "room", "room": room})
    return room


@app.post("/api/rooms/{room_id}/seats")
async def add_seat(room_id: str, request: Request) -> dict[str, Any]:
    room = get_room(room_id)
    room["seats"].extend(expand_new_seats(room_id, [await request.json()], len(room["seats"])))
    await broadcast(room_id, {"type": "room", "room": room})
    return room


@app.patch("/api/rooms/{room_id}/seats/{seat_id}")
async def patch_seat(room_id: str, seat_id: str, request: Request) -> dict[str, Any]:
    room = get_room(room_id)
    apply_seat_patch(room, seat_id, await request.json())
    await broadcast(room_id, {"type": "room", "room": room})
    return room


@app.delete("/api/rooms/{room_id}/seats/{seat_id}")
async def delete_seat(room_id: str, seat_id: str) -> dict[str, Any]:
    room = get_room(room_id)
    room["seats"] = [s for s in room["seats"] if s["id"] != seat_id]
    await broadcast(room_id, {"type": "room", "room": room})
    return room


@app.post("/api/rooms/{room_id}/end")
async def end_room(room_id: str) -> dict[str, Any]:
    room = get_room(room_id)
    notes = await finish_room(room)
    return {"room": room, "notes": notes}


async def finish_room(room: dict[str, Any]) -> dict[str, list[str]]:
    stop_everything(room["id"])
    room["ended_at"] = iso(now())
    spoke = {m["seat_id"] for m in messages_by_room[room["id"]] if m["seat_id"]}
    notes = {
        seat["id"]: [f"{seat['name']} remembers: keep sessions per discussion."]
        for seat in room["seats"]
        if seat["id"] in spoke and seat["persona"]
    }
    await broadcast(room["id"], {"type": "room", "room": room})
    await broadcast(room["id"], {"type": "ended", "notes": notes})
    return notes


@app.get("/api/search")
def search(q: str = "") -> list[dict[str, Any]]:
    needle = q.strip().lower()
    results = []
    for room_id, room_messages in messages_by_room.items():
        for message in room_messages:
            if needle and needle in message["text"].lower():
                results.append(
                    {
                        "message": message,
                        "room_title": rooms[room_id]["title"],
                        "snippet": message["text"][:120],
                    }
                )
    return results[:30]


async def handle_client_frame(room: dict[str, Any], frame: dict[str, Any]) -> None:
    room_id = room["id"]
    kind = frame.get("type")
    if kind == "say":
        await handle_say(room, str(frame.get("text", "")).strip())
    elif kind == "wake":
        seat = next((s for s in room["seats"] if s["id"] == frame.get("seat")), None)
        if seat is None:
            raise HTTPException(404, "no such seat")
        refuse_if_unavailable(room)
        start_turns(room, [seat], "wake")
    elif kind == "wake_all":
        refuse_if_unavailable(room)
        start_turns(room, [s for s in room["seats"] if s["enabled"] and s["available"]], "wake")
    elif kind == "stop":
        stop_everything(room_id)
        await broadcast(
            room_id, {"type": "quiet", "reason": "nobody", "next_tick_in_s": None, "ticks_left": 0}
        )
    elif kind == "end":
        await finish_room(room)
    elif kind == "room":
        apply_room_patch(room, frame.get("patch") or {})
        await broadcast(room_id, {"type": "room", "room": room})
    elif kind == "seat":
        apply_seat_patch(room, frame.get("seat"), frame.get("patch") or {})
        await broadcast(room_id, {"type": "room", "room": room})
    else:
        raise HTTPException(422, f"unknown frame type {kind}")


def refuse_if_unavailable(room: dict[str, Any]) -> None:
    if room["ended_at"]:
        raise HTTPException(409, "this room has ended")
    if over_budget(room):
        raise HTTPException(
            409, f"Budget of ${room['budget_usd']:.2f} reached; raise it to continue."
        )


async def handle_say(room: dict[str, Any], text: str) -> None:
    if not text:
        return
    refuse_if_unavailable(room)
    queued = find_mentioned_seats(room, text, None)
    message = add_message(room["id"], "owner", "owner", text, mentions=[s["id"] for s in queued])
    await broadcast(room["id"], {"type": "message", "message": message})
    start_turns(room, queued, "all" if re.search(r"(?<!\w)@all\b", text, re.I) else "mention")


@app.websocket("/ws/{room_id}")
async def room_socket(socket: WebSocket, room_id: str) -> None:
    await socket.accept()
    room = rooms.get(room_id)
    if room is None:
        await socket.close(code=4404)
        return
    sockets_by_room.setdefault(room_id, set()).add(socket)
    await socket.send_json(
        {
            "type": "hello",
            "room": room,
            "messages": messages_by_room[room_id],
            "owner": {"name": SETTINGS["owner"]["name"]},
            "router": "jev",
        }
    )
    try:
        while True:
            frame = await socket.receive_json()
            try:
                await handle_client_frame(room, frame)
            except HTTPException as problem:
                await socket.send_json({"type": "error", "text": str(problem.detail)})
    except WebSocketDisconnect:
        pass
    finally:
        sockets_by_room[room_id].discard(socket)


app.mount("/static", StaticFiles(directory=STATIC_DIRECTORY), name="static")
seed_data()

if __name__ == "__main__":
    assert MOCK_PORT != REAL_ADDA_PORT, "the mock must never bind the real Adda port"
    uvicorn.run(app, host=MOCK_HOST, port=MOCK_PORT, log_level="warning")
