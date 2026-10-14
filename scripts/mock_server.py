# ruff: noqa: E501  (canned chat text is kept on single lines for readability)
"""Fake Adda backend for frontend development.

Serves the static UI and speaks the REST and WebSocket protocol of docs/SPEC.md
section 6 with canned data and canned seat replies. No agent program is ever
started. Run: uv run python scripts/mock_server.py  (binds 127.0.0.1:8766 only).
"""

import asyncio
import json
import random
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

HOST = "127.0.0.1"
PORT = 8766
REAL_ADDA_PORT = 8765  # never touched by this mock
STATIC_DIR = Path(__file__).parent.parent / "static"

VALID_MODES = ("bidding", "roundrobin", "manual")
BID_THRESHOLD = 5.0
MAX_AUTO_TURNS = 3
CONTINUE_PROBABILITY = 0.4
QUIET_PROBABILITY = 0.25
HEURISTIC_ROUTER_PROBABILITY = 1 / 6

SEATS = [
    {
        "name": "claude",
        "program": "claude",
        "model": "sonnet",
        "color": "#CF5A26",
        "role": "Architecture and trade-offs",
        "available": True,
    },
    {
        "name": "opencode",
        "program": "opencode",
        "model": "openrouter/qwen3-coder",
        "color": "#2A8F5E",
        "role": "Implementation detail and edge cases",
        "available": True,
    },
    {
        "name": "pi",
        "program": "pi",
        "model": None,
        "color": "#D6409F",
        "role": "The user's view and simplicity",
        "available": True,
    },
    {
        "name": "codex",
        "program": "codex",
        "model": "gpt-5-codex",
        "color": "#3E7FD6",
        "role": "Testing and verification",
        "available": False,
    },
]
SEAT_BY_NAME = {seat["name"]: seat for seat in SEATS}
SEAT_NAMES = [seat["name"] for seat in SEATS]
# codex and opencode sometimes report no cost, like the real adapters.
SEATS_WITH_UNRELIABLE_COST = {"codex", "opencode"}

# ---------------------------------------------------------------- canned replies

CANNED_REPLIES = {
    "claude": [
        "Whichever store we pick, the real question is who owns the session: our database or the tool's own directory.",
        "I would keep one SQLite file for threads and messages, and leave tool sessions where the tools write them.",
        "The trade-off is failure modes. A single database file has one crash story. Many session files have many.",
        "@opencode what does your tool do if its session file vanishes mid-discussion? That decides whether we can depend on it.",
        "Moving sessions into SQLite buys atomic writes and full-text search, and costs us a migration for every tool format change.",
        "I lean toward a thin interface: `SessionStore` with two methods, so the backend can change later without touching seats.",
        "@pi the user-facing cost is small either way. The cost that matters is ours: one more format to keep in sync.",
        "Backups get simpler with one file. `sqlite3 .backup` is online and consistent; copying a directory of files is not.",
        "I disagree with starting sessions in SQLite. Sessions are the tools' state, and we only rent them for one discussion.",
        "Short version: SQLite for what we own, disk for what the tools own.",
        "If we ever need to replay a discussion, the transcript in SQLite is enough. We do not need the tool session for that.",
        "@codex a fixture that builds an in-memory database is a one-liner. A fixture that fakes three tool directories is not.",
        "Let us decide the boundary first, then the technology. The boundary is: transcripts ours, sessions theirs.",
        "I would write the decision down in the topic file so nobody reopens it in a month.",
        "One risk: WAL mode leaves `-wal` and `-shm` files next to the database, so a naive file copy backup breaks.",
    ],
    "opencode": [
        "Concrete detail: opencode keeps sessions under its own data directory, one JSON file per message. We cannot move that.",
        "FTS5 needs a content table plus triggers, or an external-content table. About 25 lines of SQL, once.",
        "Edge case: two server processes writing the same SQLite file. With WAL that is fine; without it, expect `database is locked`.",
        "@claude if the session file vanishes, `opencode run --session <id>` fails with a clear error and we start a new session.",
        "Set `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` at connect. That removes most lock errors in practice.",
        "Greppability is real: `grep -r` over session files finds a phrase in a second. With SQLite I need the `sqlite3` CLI.",
        "Migration cost is low today: we have no production data. It rises with every thread we store.",
        "@pi WAL is not scary. It is one pragma, and readers stop blocking the writer.",
        "I would add a `schema_version` table now. Forward-only migrations need a number to compare against.",
        "Sizes: a 100-message thread is about 40 KB of text. Ten thousand threads fit in 400 MB. Size is not the issue.",
        "Crash safety: SQLite with `synchronous=NORMAL` in WAL mode can lose the last commit on power loss, never corrupt the file.",
        "@claude an index on `(thread_id, created_at)` makes thread loads a single range scan.",
        "Edge case: messages containing NUL bytes or lone surrogates. SQLite stores them; JSON encoders may not.",
        "I can write the schema and the FTS triggers in one small module and cover it with a temp-file test.",
        "Wrapping each message insert in its own transaction costs about 1 ms. Batching is premature.",
    ],
    "pi": [
        "From the owner's side: I want to search old discussions and open one fast. Where the bytes live is invisible to me.",
        "Simple wins. One file I can copy to a USB stick is easier to trust than a directory tree.",
        "@opencode you keep saying WAL is easy, but now I have three files to remember when I back up.",
        "If I have to read a doc to restore a backup, the design failed.",
        "I would pick the option with the fewest moving parts, and SQLite is one part.",
        "@claude your boundary makes sense to me: my words in our file, tool internals in theirs.",
        "Will the search box feel instant? That is the test I care about.",
        "Please do not make me run a migration command. Upgrade should be: start the server.",
        "What does the owner see when a session file is missing? A quiet failure would be worse than an error message.",
        "I like greppable files, but I will never grep them. I will use the search box.",
        "Can the room show where a thread is stored? Even a small footer line would help me trust it.",
        "If it is two stores, say so in the README in two sentences.",
        "@codex can the tests show me a restore from backup working? That is the story I want to believe.",
        "Fewer settings. A path to the database file, nothing else.",
        "I think we are agreeing more than it sounds. Transcripts in SQLite, sessions left alone.",
    ],
    "codex": [
        "Tests first: a temp database per test keeps cases independent, and SQLite makes that trivial.",
        "I would add a test that kills the writer mid-transaction and checks the file still opens.",
        "@opencode please add a fixture with a thread of 1,000 messages so we have a search timing baseline.",
        "Verification plan: write, close, reopen, search. If the hit comes back, storage and FTS both work.",
        "File-per-session fixtures need cleanup of leftover directories; the database needs one `tmp_path`.",
        "Backups should be tested too: copy the file with `.backup`, open the copy, compare row counts.",
        "@claude what is the acceptance check for the boundary? I suggest: no import of a tool path inside the storage module.",
        "A migration test should load a database at version N and assert the schema at N+1.",
        "Flaky-test risk: timing-based lock tests. Use `busy_timeout=0` and expect the error deterministically.",
        "I would run the search tests with unicode text, including Bengali, since the project name is.",
        "Property to check: the count of messages returned by a thread equals the count inserted, in order.",
        "@pi a restore test is cheap and I agree it earns trust. I will list it first.",
        "Coverage gap: the 'session file vanished' path has no test yet.",
        "Every pragma we set should have a test that reads it back.",
        "Run `PRAGMA integrity_check` at the end of each storage test; it costs milliseconds.",
    ],
}

# ---------------------------------------------------------------- state helpers


def now_iso(offset_minutes: float = 0.0) -> str:
    return (datetime.now(UTC) + timedelta(minutes=offset_minutes)).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def extract_mentions(text: str) -> list[str]:
    """Seat names written as @name, in order of first appearance, without repeats."""
    mentioned_names: list[str] = []
    for token in re.findall(r"@([A-Za-z0-9_-]+)", text):
        if token in SEAT_BY_NAME and token not in mentioned_names:
            mentioned_names.append(token)
    return mentioned_names


def build_message(
    thread_id: str,
    author: str,
    role: str,
    text: str,
    created_at: str,
    cost_usd: float | None = None,
    meta: dict | None = None,
) -> dict:
    return {
        "id": new_id(),
        "thread_id": thread_id,
        "author": author,
        "role": role,
        "text": text,
        "created_at": created_at,
        "mentions": extract_mentions(text),
        "cost_usd": cost_usd,
        "meta": meta or {},
    }


def random_seat_cost(seat_name: str) -> float | None:
    if seat_name in SEATS_WITH_UNRELIABLE_COST and random.random() < 0.4:
        return None
    return round(random.uniform(0.004, 0.03), 4)


def random_seat_meta(seat_name: str, bid_score: float) -> dict:
    model = SEAT_BY_NAME[seat_name]["model"] or "pi-default"
    return {
        "bid": bid_score,
        "model": model,
        "latency_ms": random.randint(2000, 40000),
    }


class Room:
    """In-memory state of one thread plus its live sockets and speaker task."""

    def __init__(self, title: str, mode: str, created_at: str, ended_at: str | None = None):
        self.thread = {
            "id": new_id(),
            "title": title,
            "created_at": created_at,
            "ended_at": ended_at,
            "mode": mode,
        }
        self.messages: list[dict] = []
        self.sockets: set[WebSocket] = set()
        self.turn_task: asyncio.Task | None = None
        self.current_speaker: str | None = None
        self.heuristic_notice_posted = False

    @property
    def thread_id(self) -> str:
        return self.thread["id"]

    def add_seed_message(self, author: str, minutes_after_start: float, text: str) -> None:
        start = datetime.fromisoformat(self.thread["created_at"])
        created_at = (start + timedelta(minutes=minutes_after_start)).isoformat()
        if author == "owner":
            message = build_message(self.thread_id, "owner", "owner", text, created_at)
        elif author == "system":
            message = build_message(self.thread_id, "system", "system", text, created_at)
        else:
            message = build_message(
                self.thread_id,
                author,
                "seat",
                text,
                created_at,
                cost_usd=random_seat_cost(author),
                meta=random_seat_meta(author, round(random.uniform(5.0, 9.5), 1)),
            )
        self.messages.append(message)

    async def broadcast(self, frame: dict) -> None:
        for socket in list(self.sockets):
            try:
                await socket.send_json(frame)
            except (RuntimeError, WebSocketDisconnect):
                # The tab closed between our last check and this send; its own
                # handler removes it, so skipping it here is correct.
                self.sockets.discard(socket)

    async def post_message(self, message: dict) -> None:
        self.messages.append(message)
        await self.broadcast({"type": "message", "message": message})

    async def post_system(self, text: str) -> None:
        message = build_message(self.thread_id, "system", "system", text, now_iso())
        await self.post_message(message)


ROOMS: dict[str, Room] = {}


def get_room_or_404(thread_id: str) -> Room:
    room = ROOMS.get(thread_id)
    if room is None:
        raise HTTPException(status_code=404, detail=f"Unknown thread id: {thread_id}")
    return room


# ---------------------------------------------------------------- seed data


def seed_storage_thread() -> Room:
    room = Room(
        "Should Adda store seat sessions in SQLite or on disk?",
        "bidding",
        now_iso(-45),
    )
    script = [
        (
            "owner",
            0,
            "Issue: where should Adda keep seat sessions? Today I assume one SQLite file for everything. Is that right, or should sessions stay as files on disk?",
        ),
        (
            "claude",
            1.5,
            "First split the question. There are two kinds of data: transcripts, which we write and own, and tool sessions, which `claude`, `opencode` and `pi` write under their own directories. Transcripts belong in SQLite without much debate. The open part is whether we copy tool session state into our database too. I would not: it duplicates state we cannot interpret, and every tool update could change its format under us.",
        ),
        (
            "opencode",
            3,
            "Agreed on the split. Concrete point for SQLite: FTS5 gives us ranked full-text search over every message in one query. Doing that over session files means writing our own indexer.",
        ),
        ("system", 3.5, "codex is not available: binary not found on PATH."),
        (
            "pi",
            5,
            "As the person using this, I only care that search is fast and that I can back it up. One file sounds easier to back up than a folder of files.",
        ),
        (
            "claude",
            6.5,
            "@pi it is easier only if the backup is taken correctly. With WAL mode there are `-wal` and `-shm` files beside the database, and a plain `cp` of just the main file can lose recent commits. `sqlite3 .backup` handles it, but then backup is a command, not a copy.",
        ),
        (
            "opencode",
            8,
            "@claude fair, but WAL is still the right default. It lets the UI read while a seat message is being written. Without it a search would block on an insert and the page would stutter.",
        ),
        (
            "pi",
            9.5,
            "@opencode I think you are underselling the backup cost. I would take slower writes and one file over WAL and three files.",
        ),
        (
            "owner",
            11,
            "Good point from pi. Can we get both: WAL for the server, and a backup that is just one command?",
        ),
        (
            "claude",
            12.5,
            "Yes. Keep WAL, and add `adda backup <path>` that calls the SQLite online backup API. The user runs one command and gets one consistent file. That also answers pi's concern without giving up concurrent reads.",
        ),
        (
            "opencode",
            14,
            "Fine by me. Crash safety is good too: in WAL mode with `synchronous=NORMAL` a power loss can drop the last transaction but cannot corrupt the file. Session files written by tools have no such promise; a half-written JSON file is possible.",
        ),
        (
            "pi",
            15.5,
            "Ok. What about the files I can open in an editor? I like being able to read a session with `less`.",
        ),
        (
            "opencode",
            17,
            'Greppability is the real loss. With files, `grep -r "WAL" sessions/` just works. With SQLite you need `sqlite3 adda.db "select ..."`. We can offer `adda export <thread>` that writes Markdown, so reading stays easy.',
        ),
        (
            "claude",
            18.5,
            "Options as I see them:\n- SQLite for transcripts only, tools keep their sessions (my pick)\n- SQLite for everything, including copied session blobs\n- Plain files for everything, with a separate search index\nThe third needs two sources of truth, which is the worst failure mode of the three.",
        ),
        (
            "owner",
            20,
            "I lean toward the first. What does migration look like if I change my mind later?",
        ),
        (
            "opencode",
            21.5,
            "Cheap if we start with a `schema_version` table and forward-only migrations. Moving later from tool-owned sessions to our own blobs is an additive table, not a rewrite.",
        ),
        ("system", 22, "Room went quiet: no seat scored above 5.0."),
        ("owner", 24, "Still need a view on tests. How would we check this storage layer?"),
        (
            "claude",
            25.5,
            "@codex would normally answer this, but it is not on PATH, so I will cover it. A temp-file database per test, a write-close-reopen-search round trip, and one test that kills the writer mid-transaction and asserts the file still opens. No fake tool directories needed, which is another point for keeping sessions out of our store.",
        ),
        (
            "opencode",
            27,
            "Add `PRAGMA integrity_check` at the end of each storage test. It costs milliseconds and catches the corruption class we care about.",
        ),
        (
            "pi",
            29,
            "I would also like one test that restores from a backup and finds an old message. That is the story I want to believe.",
        ),
        ("owner", 31, "Do that one. Anything that blocks starting?"),
        (
            "claude",
            33,
            "Nothing blocks. One decision to record: busy timeout. I suggest `busy_timeout=5000` so two processes (server and a CLI export) do not fail with `database is locked`.",
        ),
        (
            "opencode",
            35,
            "I can write the schema, the FTS triggers and the backup command in one module. `PRAGMA journal_mode=WAL` goes in the connect function so no caller can forget it.",
        ),
        ("pi", 37, "Sounds good. Please keep the README to two sentences about where data lives."),
        (
            "owner",
            40,
            "Decision: SQLite for transcripts with WAL and an online backup command; tool sessions stay where the tools put them.",
        ),
    ]
    for author, minute, text in script:
        room.add_seed_message(author, minute, text)
    return room


def seed_threshold_thread() -> Room:
    room = Room(
        "Bidding threshold: 5.0 or adaptive?",
        "bidding",
        now_iso(-180),
        ended_at=now_iso(-150),
    )
    script = [
        (
            "owner",
            0,
            "Is a fixed bid threshold of 5.0 good enough, or should it adapt to how busy the room is?",
        ),
        (
            "claude",
            2,
            "A fixed threshold is predictable, and predictability matters when the owner is watching. Adaptive thresholds hide why a seat stayed quiet.",
        ),
        (
            "pi",
            4,
            "Predictable. If the room is quiet I want to know why, and 'below 5' is a reason I can understand.",
        ),
        (
            "opencode",
            6,
            "Edge case for adaptive: the first message of a thread has no history, so any moving average is undefined. We would need a default anyway, and that default is the fixed threshold.",
        ),
        (
            "claude",
            8,
            "@opencode good point. Keep 5.0 as the default and expose it as `ADDA_BID_THRESHOLD`, which is one line of config.",
        ),
        ("owner", 10, "Fine. Keep it fixed and configurable."),
        (
            "pi",
            12,
            "Can the UI show the winning score next to the message? Then I can tune it by feel.",
        ),
        (
            "claude",
            14,
            "Yes, `meta.bid` is already stored on each seat message, so the UI only has to display it.",
        ),
    ]
    for author, minute, text in script:
        room.add_seed_message(author, minute, text)
    return room


def seed_naming_thread() -> Room:
    room = Room(
        "Name the end-of-discussion memory prompt",
        "roundrobin",
        now_iso(-1500),
    )
    script = [
        (
            "owner",
            0,
            "We need a short name for the prompt that asks each seat what to remember. Ideas?",
        ),
        ("claude", 1, "`debrief`. It says what happens: the seat looks back and reports."),
        (
            "opencode",
            2,
            "`retro` is shorter, but it suggests a team retrospective with action items, which we do not want.",
        ),
        ("pi", 3, "Call it 'Remember'. The button says what the seat does with it."),
        ("owner", 5, "I like debrief for the code and 'Remember' for the button."),
    ]
    for author, minute, text in script:
        room.add_seed_message(author, minute, text)
    return room


def seed_rooms() -> None:
    # Insertion order does not matter: /api/threads sorts by created_at.
    for room in (seed_naming_thread(), seed_threshold_thread(), seed_storage_thread()):
        ROOMS[room.thread_id] = room


# ---------------------------------------------------------------- fake turns


def make_bids(room: Room, excluded_seat: str | None) -> tuple[list[dict], str | None]:
    """Random bids for every seat. About a quarter of the time nobody reaches the threshold."""
    room_goes_quiet = random.random() < QUIET_PROBABILITY
    bids = []
    for seat in SEATS:
        if not seat["available"] or seat["name"] == excluded_seat:
            score = 0.0
        elif room_goes_quiet:
            score = round(random.uniform(0.5, 4.9), 1)
        else:
            score = round(random.uniform(1.0, 9.8), 1)
        bids.append(
            {
                "seat": seat["name"],
                "score": score,
                "confidence": round(random.uniform(0.5, 1.0), 2),
            }
        )
    best_bid = max(bids, key=lambda bid: bid["score"])
    chosen = best_bid["seat"] if best_bid["score"] >= BID_THRESHOLD else None
    return bids, chosen


async def announce_unavailable(room: Room, seat_name: str) -> None:
    await room.post_system(f"{seat_name} is not available: binary not found on PATH.")


async def seat_speaks(room: Room, seat_name: str, bid_score: float) -> dict | None:
    """One fake seat turn. Returns the posted message, or None if the seat is unavailable."""
    if not SEAT_BY_NAME[seat_name]["available"]:
        await announce_unavailable(room, seat_name)
        return None
    room.current_speaker = seat_name
    await room.broadcast({"type": "typing", "seat": seat_name, "state": "start"})
    try:
        await asyncio.sleep(random.uniform(1.2, 2.0))
        message = build_message(
            room.thread_id,
            seat_name,
            "seat",
            random.choice(CANNED_REPLIES[seat_name]),
            now_iso(),
            cost_usd=random_seat_cost(seat_name),
            meta=random_seat_meta(seat_name, bid_score),
        )
        await room.post_message(message)
    finally:
        # Runs on cancellation too, so a stopped seat never stays "typing" in any tab.
        room.current_speaker = None
        await room.broadcast({"type": "typing", "seat": seat_name, "state": "stop"})
    return message


async def choose_speakers(room: Room, last_author: str | None) -> list[tuple[str, float]]:
    """Pick (seat, bid score) pairs for one round according to the thread mode."""
    mode = room.thread["mode"]
    if mode == "manual":
        return []
    if mode == "roundrobin":
        return [(name, 10.0) for name in SEAT_NAMES]
    bids, chosen = make_bids(room, last_author)
    router = "jev"
    if random.random() < HEURISTIC_ROUTER_PROBABILITY:
        router = "heuristic"
        if not room.heuristic_notice_posted:
            room.heuristic_notice_posted = True
            await room.post_system("Jev unreachable; falling back to heuristic for this turn.")
    await room.broadcast({"type": "bids", "bids": bids, "chosen": chosen, "router": router})
    if chosen is None:
        return []
    chosen_score = next(bid["score"] for bid in bids if bid["seat"] == chosen)
    return [(chosen, chosen_score)]


async def run_turn_loop(room: Room, mentioned_seats: list[str], first_author: str) -> None:
    """Fake scheduler for one owner message or wake: SPEC 5 steps 2-5 with a cap of 3."""
    pending_mentions = list(mentioned_seats)
    last_author = first_author
    auto_turns = 0
    while auto_turns < MAX_AUTO_TURNS:
        if pending_mentions:
            speakers = [(name, 10.0) for name in pending_mentions]
            pending_mentions = []
        else:
            speakers = await choose_speakers(room, last_author)
        if not speakers:
            break
        last_message = None
        for seat_name, bid_score in speakers:
            posted = await seat_speaks(room, seat_name, bid_score)
            if posted is not None:
                last_message = posted
                last_author = seat_name
                auto_turns += 1
        if last_message is None or room.thread["mode"] != "bidding":
            break
        if random.random() >= CONTINUE_PROBABILITY:
            break
        pending_mentions = [
            name
            for name in last_message["mentions"]
            if name != last_message["author"] and SEAT_BY_NAME[name]["available"]
        ]
    await room.broadcast({"type": "quiet"})


async def cancel_turn_task(room: Room) -> None:
    task = room.turn_task
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # expected: we just cancelled it ourselves
    room.turn_task = None


async def start_turns(room: Room, mentioned_seats: list[str], first_author: str) -> None:
    await cancel_turn_task(room)
    room.turn_task = asyncio.create_task(run_turn_loop(room, mentioned_seats, first_author))


# ---------------------------------------------------------------- shared actions


def set_mode(room: Room, mode: str) -> None:
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid mode {mode!r}; expected one of {', '.join(VALID_MODES)}.")
    room.thread["mode"] = mode


def build_end_notes(room: Room) -> dict[str, list[str]]:
    speaking_seats = []
    for message in room.messages:
        if message["role"] == "seat" and message["author"] not in speaking_seats:
            speaking_seats.append(message["author"])
    return {
        seat_name: [
            f"{seat_name}: remember the boundary (transcripts ours, sessions theirs).",
            f"{seat_name}: ask for a restore test before trusting a backup.",
        ]
        for seat_name in speaking_seats
    }


async def end_discussion(room: Room) -> dict[str, list[str]]:
    await cancel_turn_task(room)
    notes = build_end_notes(room)
    room.thread["ended_at"] = now_iso()
    await room.broadcast({"type": "ended", "notes": notes})
    await room.post_system("Discussion ended. Memory notes were written.")
    return notes


async def handle_say(room: Room, text: str) -> None:
    await cancel_turn_task(room)  # owner interrupt, SPEC 5 step 1
    message = build_message(room.thread_id, "owner", "owner", text, now_iso())
    await room.post_message(message)
    await start_turns(room, message["mentions"], "owner")


async def handle_stop(room: Room) -> None:
    speaker = room.current_speaker
    if speaker is None:
        await room.broadcast({"type": "error", "text": "Nobody is speaking."})
        return
    await cancel_turn_task(room)
    await room.post_system(f"Owner stopped {speaker}.")


async def handle_client_frame(room: Room, frame: dict) -> None:
    frame_type = frame.get("type")
    if frame_type == "say":
        text = frame.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("say needs a non-empty text.")
        await handle_say(room, text.strip())
    elif frame_type == "wake":
        seat_name = frame.get("seat")
        if seat_name not in SEAT_BY_NAME:
            raise ValueError(f"wake needs a known seat, got {seat_name!r}.")
        await start_turns(room, [seat_name], "owner")
    elif frame_type == "mode":
        set_mode(room, frame.get("mode"))
        await room.broadcast({"type": "mode", "mode": room.thread["mode"]})
    elif frame_type == "stop":
        await handle_stop(room)
    elif frame_type == "end":
        await end_discussion(room)
    else:
        raise ValueError(f"Unknown frame type {frame_type!r}.")


# ---------------------------------------------------------------- search


def build_snippet(text: str, query: str) -> str | None:
    """About 120 plain characters around the first hit, or None when there is no hit."""
    flat_text = " ".join(text.split())
    hit_index = flat_text.lower().find(query.lower())
    if hit_index < 0:
        return None
    window_start = max(0, hit_index - 50)
    window_end = min(len(flat_text), window_start + 120)
    snippet = flat_text[window_start:window_end]
    if window_start > 0:
        snippet = "…" + snippet
    if window_end < len(flat_text):
        snippet = snippet + "…"
    return snippet


# ---------------------------------------------------------------- app

app = FastAPI(title="Adda mock server")
app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/seats")
async def list_seats() -> list[dict]:
    return SEATS


@app.get("/api/threads")
async def list_threads() -> list[dict]:
    threads = [room.thread for room in ROOMS.values()]
    return sorted(threads, key=lambda thread: thread["created_at"], reverse=True)


@app.post("/api/threads")
async def create_thread(body: dict = Body(...)) -> dict:  # noqa: B008
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        raise HTTPException(status_code=400, detail="title must be a non-empty string.")
    mode = body.get("mode", "bidding")
    if mode not in VALID_MODES:
        raise HTTPException(status_code=400, detail=f"Invalid mode {mode!r}.")
    room = Room(title.strip(), mode, now_iso())
    ROOMS[room.thread_id] = room
    return room.thread


@app.get("/api/threads/{thread_id}")
async def get_thread(thread_id: str) -> dict:
    room = get_room_or_404(thread_id)
    return {"thread": room.thread, "messages": room.messages}


@app.post("/api/threads/{thread_id}/mode")
async def post_thread_mode(thread_id: str, body: dict = Body(...)) -> dict:  # noqa: B008
    room = get_room_or_404(thread_id)
    try:
        set_mode(room, body.get("mode"))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    await room.broadcast({"type": "mode", "mode": room.thread["mode"]})
    return room.thread


@app.post("/api/threads/{thread_id}/end")
async def post_thread_end(thread_id: str) -> dict:
    room = get_room_or_404(thread_id)
    notes = await end_discussion(room)
    return {"thread": room.thread, "notes": notes}


@app.get("/api/search")
async def search(q: str = "") -> list[dict]:
    if not q.strip():
        return []
    results = []
    for room in ROOMS.values():
        for message in room.messages:
            snippet = build_snippet(message["text"], q.strip())
            if snippet is not None:
                results.append(
                    {
                        "message": message,
                        "thread_title": room.thread["title"],
                        "snippet": snippet,
                    }
                )
    return results


@app.websocket("/ws/{thread_id}")
async def websocket_endpoint(websocket: WebSocket, thread_id: str) -> None:
    await websocket.accept()
    room = ROOMS.get(thread_id)
    if room is None:
        await websocket.send_json({"type": "error", "text": f"Unknown thread id: {thread_id}"})
        await websocket.close()
        return
    room.sockets.add(websocket)
    try:
        await websocket.send_json(
            {
                "type": "hello",
                "thread": room.thread,
                "seats": SEATS,
                "messages": room.messages,
            }
        )
        while True:
            raw_text = await websocket.receive_text()
            try:
                frame = json.loads(raw_text)
                if not isinstance(frame, dict):
                    raise ValueError("Frame must be a JSON object.")
                await handle_client_frame(room, frame)
            except (json.JSONDecodeError, ValueError) as error:
                await websocket.send_json({"type": "error", "text": f"Bad frame: {error}"})
    except WebSocketDisconnect:
        pass  # normal tab close
    finally:
        room.sockets.discard(websocket)


seed_rooms()

if __name__ == "__main__":
    assert PORT != REAL_ADDA_PORT, "The mock must never use the real Adda port."
    print(f"Adda mock server: http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
