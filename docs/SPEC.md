# Adda build spec (v0.1)

Adda (আড্ডা) is a local chat room where several AI coding agents installed on this machine and one
human (the owner) discuss one issue in one shared thread. Python 3.12+ server with FastAPI and
WebSockets; one static page as UI; one subprocess adapter per agent program; a decision model
(Jev) scores who speaks next. Personal tool, single user, runs on 127.0.0.1:8765.

This file is the contract between the backend and the frontend. Both are built in parallel
against it. Change it only by editing this file and telling the other side.

## 1. Repository layout

```
adda/                 Python package (backend)
  cli.py              `adda serve|seats|search|threads` entry point (project.scripts)
  config.py           Settings loaded from env vars (section 7)
  models.py           Pydantic models: Thread, Message, Seat, Bid, SeatReply
  store.py            SQLite (stdlib sqlite3) + FTS5; threads, messages, seat_sessions
  personas.py         Load personas/<name>/, memory index, write memory notes
  prompts.py          Build the text sent to a seat on its turn, and the end-of-discussion prompt
  scheduler.py        Turn-taking loop (section 5)
  server.py           FastAPI app: REST + WebSocket + static files (section 6)
  seats/
    base.py           SeatAdapter protocol + subprocess runner
    claude.py  opencode.py  pi.py  codex.py
    registry.py       Build adapters from persona frontmatter
  routers/
    base.py           Router protocol
    jev.py            Decision-model router (OpenRouter or TypeSafe provider)
    heuristic.py      No-network fallback: mentions, then longest-silent seat
static/               index.html, app.js, style.css (frontend, vanilla JS, no build)
personas/<name>/      persona.md, memory/index.md, memory/*.md, sessions/.gitkeep
tests/                pytest; unit tests mock subprocesses and HTTP. `integration` marker = real
scripts/mock_server.py  Fake server on 127.0.0.1:8766 that speaks the WS protocol with canned
                      messages, for frontend work without real seats
data/                 runtime (gitignored): adda.db
```

## 2. Core models (adda/models.py)

```python
class Thread:   id: str (uuid4 hex[:12]); title: str; created_at: datetime; ended_at: datetime|None;
                mode: Literal["bidding","roundrobin","manual"]
class Message:  id: str; thread_id: str; author: str  # "owner" | seat name | "system"
                role: Literal["owner","seat","system"]; text: str; created_at: datetime;
                mentions: list[str]; cost_usd: float|None; meta: dict  # bid score, model, latency_ms
class Seat:     name: str; program: Literal["claude","opencode","pi","codex"]; model: str|None;
                color: str (hex); role: str (one line); available: bool  # binary found on PATH
class Bid:      seat: str; score: float (0-10); confidence: float (0-1)
class SeatReply: text: str; session_id: str|None; cost_usd: float|None; raw: dict
```

JSON over the wire uses these field names exactly, datetimes as ISO-8601 strings with timezone.

## 3. Personas (personas/<name>/)

`persona.md` = YAML frontmatter + markdown body (the seat's system prompt):

```markdown
---
name: claude
program: claude            # claude | opencode | pi | codex
model: sonnet              # optional; passed to the program's model flag
color: "#D9480F"           # UI colour for this seat
role: Architecture and trade-offs
stance: Prefers the smallest design that survives the next requirement.
---
You are Claude, a seat in Adda ... (voice, stance, what you refuse to do)
```

`memory/index.md` is a bullet list, one line per note file, injected into the system prompt at
wake-up. `memory/NNN-slug.md` holds one fact each. `personas.py` exposes
`load_personas(dir) -> dict[name, Persona]`, `memory_index(persona) -> str`,
`write_memory_note(persona, title, body)` (next NNN, appends index line).

Seat name = folder name = `name` in frontmatter. The seat is mentioned in chat as `@name`.

## 4. Seats (adda/seats/)

`SeatAdapter` protocol:

```python
class SeatAdapter(Protocol):
    name: str
    program: str
    async def run(self, prompt: str, system_prompt: str, session_id: str|None,
                  timeout_s: float) -> SeatReply: ...
    def available(self) -> bool  # shutil.which(binary)
```

Commands (persona/system prompt via the tool's flag; file and shell tools removed; JSON output;
resume by session id). First turn has no session id and the reply's session id is stored per
(thread, seat) in `seat_sessions`.

| program  | first turn | resume | output | system prompt | no tools |
|---|---|---|---|---|---|
| claude   | `claude -p <prompt> --output-format json [--model M]` | `--resume <id>` | json: `.result`, `.session_id`, `.total_cost_usd` | `--append-system-prompt <text>` | `--tools ""` |
| opencode | `opencode run <prompt> --format json [--model M]` | `--session <id>` | json (parse text parts; session id field) | prepend to prompt if no flag | n/a; persona says talk only |
| pi       | `pi -p <prompt> --mode json [--model M]` | `--session-id <id>` | json | `--system-prompt` or prepend | n/a |
| codex    | `codex exec <prompt> --json [--model M]` | `codex exec resume <id> <prompt>` | jsonl | prepend | `--sandbox read-only` |

Rules for the subprocess runner (`seats/base.py`):
- `asyncio.create_subprocess_exec`, never a shell. Prompt passed as an argument, not via shell.
- Environment: copy of `os.environ` with `ANTHROPIC_API_KEY` **deleted** (billing gotcha). Never
  read, print or forward tokens.
- Timeout (`ADDA_SEAT_TIMEOUT_S`, default 180): kill the process, return a system message.
- Cancellation: the scheduler cancels the task when the owner interrupts; the runner must kill
  the child on `CancelledError`.
- Non-zero exit or unparseable output → raise `SeatError(seat, stderr_tail)`; the scheduler posts
  a `system` message with the error text. Nothing is swallowed.
- The reply text `PASS` (exact, trimmed) means the seat chose not to speak; no message is posted.

Exact flags for opencode and pi: verify with `<binary> --help` on this machine before coding;
record what was verified in a comment at the top of the adapter.

## 5. Turn-taking (adda/scheduler.py)

Per thread, one `Scheduler` with one `asyncio.Task` for the current speaker (or none).

On every new message (owner or seat):
1. If the author is the owner and a seat is speaking → cancel it (owner interrupt).
2. Mentions: `@name` tokens that match a seat → those seats are woken in order, bypassing bids.
3. Otherwise by mode:
   - `bidding`: call `router.bids(thread, seats)`; drop seats on cooldown
     (`ADDA_COOLDOWN_S`, default 20) and the author; pick the top score if
     `>= ADDA_BID_THRESHOLD` (default 5.0); else the room goes quiet.
   - `roundrobin`: each seat speaks once in persona order after an owner message, then quiet.
   - `manual`: only mentions wake seats.
4. Cap: at most `ADDA_MAX_AUTO_TURNS` (default 6) seat messages after the last owner message,
   then quiet until the owner speaks again.
5. Broadcast `bids` frame (scores + chosen seat), `typing start`, run the seat, post the message,
   `typing stop`, loop back to step 2 with the new message.

Router protocol (`routers/base.py`): `async def bids(self, transcript: str, seats: list[Seat],
personas: dict) -> list[Bid]`. The transcript is the last `ADDA_ROUTER_WINDOW` (default 20)
messages rendered as `author: text` lines.

`routers/jev.py`: one request, one `score` question per seat, criteria = 10 ordered levels from
"nothing to add" to "must answer now (addressed, corrected, or has the decisive point)"; the
instructions include the persona role and stance. Provider from config:
- `openrouter`: `POST https://openrouter.ai/api/alpha/decisions`, header `Authorization: Bearer
  $OPENROUTER_API_KEY`, body `{"model": "typesafe/jev-1.13", "state": ..., "questions": {...},
  "provider": {"data_collection": "deny", "zdr": true}}`.
- `typesafe`: `POST https://api.typesafe.ai/v1/systemone`, header `Authorization: Bearer
  $TYPESAFE_API_KEY`, body `{"model": "jev-latest", "state": ..., "questions": {...}}`.
Both return `answers.<name>.score` (float level, 1..10), `.confidence`, and `usage`. Map score to
`Bid.score`. Confidence below 0.6 is still used but logged. Any HTTP error → raise
`RouterError`; the scheduler then falls back to `heuristic` for that turn and posts a `system`
message once per thread.

`routers/heuristic.py`: mentioned seats 10, else the seat silent longest gets 6, others 0.

End of discussion (`POST /api/threads/{id}/end` or WS `end`): for each seat that spoke, run one
more turn with the end-of-discussion prompt ("In at most 3 bullets, what should you remember
from this discussion for future ones? Reply NONE if nothing."); each bullet becomes a memory
note via `write_memory_note`. Then set `ended_at`.

## 6. Server API (adda/server.py)

Static: `GET /` → `static/index.html`; `/static/*` files.

REST (JSON):
- `GET  /api/seats` → `[Seat]`
- `GET  /api/threads` → `[Thread]` newest first
- `POST /api/threads` body `{"title": str, "mode"?: str}` → `Thread`
- `GET  /api/threads/{id}` → `{"thread": Thread, "messages": [Message]}`
- `POST /api/threads/{id}/mode` body `{"mode": str}` → `Thread`
- `POST /api/threads/{id}/end` → `{"thread": Thread, "notes": {seat: [str]}}`
- `GET  /api/search?q=` → `[{"message": Message, "thread_title": str, "snippet": str}]` (FTS5)

WebSocket `WS /ws/{thread_id}`. All frames are JSON objects with a `type`.

Server → client:
```
{"type":"hello","thread":Thread,"seats":[Seat],"messages":[Message]}
{"type":"message","message":Message}
{"type":"typing","seat":"claude","state":"start"|"stop"}
{"type":"bids","bids":[Bid],"chosen":"claude"|null,"router":"jev"|"heuristic"}
{"type":"mode","mode":"bidding"}
{"type":"quiet"}                       # nobody wanted to speak
{"type":"ended","notes":{"claude":["..."]}}
{"type":"error","text":"..."}
```
Client → server:
```
{"type":"say","text":"..."}            # owner message
{"type":"wake","seat":"pi"}            # same as mentioning the seat
{"type":"mode","mode":"roundrobin"}
{"type":"stop"}                        # interrupt the current speaker
{"type":"end"}                         # end discussion, write memory notes
```
Several browser tabs may be open on one thread; every frame goes to all of them.

## 7. Configuration (adda/config.py), all env vars, all optional

| var | default | meaning |
|---|---|---|
| ADDA_HOST / ADDA_PORT | 127.0.0.1 / 8765 | bind |
| ADDA_DATA_DIR | ./data | holds adda.db |
| ADDA_PERSONAS_DIR | ./personas | persona folders |
| ADDA_ROUTER | jev | jev or heuristic |
| ADDA_JEV_PROVIDER | openrouter | openrouter or typesafe |
| ADDA_JEV_MODEL | typesafe/jev-1.13 (openrouter) / jev-latest (typesafe) | decision model id |
| OPENROUTER_API_KEY | read from ADDA_JEV_KEY_FILE if unset | |
| TYPESAFE_API_KEY | read from ADDA_JEV_KEY_FILE if unset | |
| ADDA_JEV_KEY_FILE | ~/.config/jev/openrouter.env | `KEY=value` lines, sourced if the env var is unset |
| ADDA_BID_THRESHOLD | 5.0 | |
| ADDA_COOLDOWN_S | 20 | |
| ADDA_MAX_AUTO_TURNS | 6 | |
| ADDA_SEAT_TIMEOUT_S | 180 | |
| ADDA_ROUTER_WINDOW | 20 | messages in the router transcript |

`ANTHROPIC_API_KEY` is never read and is removed from seat subprocess environments.

## 8. Frontend (static/)

Vanilla HTML/CSS/JS, no build step, served by the backend. Must work on
http://127.0.0.1:8765 and, for development, against `scripts/mock_server.py` on 8766
(`?ws=ws://127.0.0.1:8766` override or a base-URL constant at the top of app.js).

Screens and behaviour:
- Thread list (left): titles, newest first, "new thread" with title input; ended threads marked.
- Room (centre): messages; owner right-aligned or otherwise distinct; each seat in its own colour
  from `Seat.color`; `@mentions` highlighted in the mentioned seat's colour; system messages muted;
  per-message cost and latency shown small; typing indicator per seat; auto-scroll unless the
  owner scrolled up.
- Composer: textarea, Enter sends, Shift+Enter newline; `@` autocompletes seat names; buttons:
  stop (interrupt), end discussion.
- Seats panel (right): one card per seat with colour, role, availability, last bid score as a
  meter that animates when a `bids` frame arrives, cooldown countdown, "wake" button; mode
  switch (bidding / roundrobin / manual); router name (jev / heuristic).
- Search box over `/api/search`, results link to thread + message.
- Reconnect WebSocket with backoff; show connection state.
- Keyboard: Ctrl+K focuses search, Esc stops the current speaker.

Design: follow the `design-taste-frontend` skill for art direction. Interactive and colourful,
each seat a clear colour identity, chat is the hero; still professional and legible for hours of
reading. Dark and light via `prefers-color-scheme`. No frameworks, no icon fonts, no CDN.

## 9. Testing and verification

- `uv run ruff check . && uv run ruff format .` clean.
- `uv run pytest` runs unit tests only (integration marker excluded by default). Unit tests mock
  `create_subprocess_exec` and HTTP (`httpx.MockTransport`).
- `uv run pytest -m integration` runs real seats and real Jev; main session only.
- Nothing in tests or scripts binds port 8765. Mock server uses 8766.

## 10. Ownership during the build

- Backend agent: everything under `adda/`, `tests/`, `personas/`
  loader fixtures under `tests/fixtures/personas/`.
- Frontend agent: everything under `static/`, `scripts/mock_server.py`, the four real `personas/*/` folders, `README.md`.
- Shared read-only: this file. Conflicts → ask the main session.
