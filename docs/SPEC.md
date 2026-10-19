# Adda build spec (v0.2)

Adda (আড্ডা) is a local chat room where several AI coding agents (seats) and one human (the owner)
discuss one issue in one shared thread. Python 3.12+ server with FastAPI and WebSockets; static
pages as UI; one subprocess adapter per harness; a decision model (Jev) scores who speaks next.
Personal tool, single user, 127.0.0.1:8765.

v0.2 changes v0.1 in these ways: rooms replace threads as the top-level object and carry their
own seats, folder, budget and liveliness; personas become a library that seats reference;
the hard auto-turn cap is replaced by liveliness and budget; `@all` and wildcard mentions;
Markdown rendering with copy; the owner has a name; settings live in a TOML file and are
editable from a settings page; three pages instead of one. Section 11 lists what v0.1 code
must change. Sections marked **unchanged** are the same as v0.1.

This file is the contract between agents building in parallel. Change it only by editing this
file and telling the main session.

## 1. Repository layout

```
adda/
  cli.py            adda serve|rooms|search|harnesses
  config.py         Settings: defaults < ~/.config/adda/config.toml < env vars (section 7)
  harnesses.py      Detect installed harnesses, versions, model suggestions, thinking support
  models.py         Pydantic models (section 2)
  store.py          SQLite + FTS5: rooms, room_seats, messages, seat_sessions
  personas.py       Persona library: load, memory index, write memory notes
  prompts.py        Seat system prompt and turn prompt, end-of-room prompt
  scheduler.py      Turn-taking (section 5)
  server.py         REST + WebSocket + pages (section 6)
  seats/            base.py, claude.py, opencode.py, pi.py, codex.py, registry.py
  routers/          base.py, jev.py, heuristic.py  (unchanged)
static/
  index.html rooms.js        Rooms page: list + new-room card
  room.html  room.js         The chat room
  settings.html settings.js  Settings page
  common.js                  fetchJson, WebSocket client, colour helpers, toasts
  markdown.js                Markdown subset renderer + copy helpers (section 9)
  style.css, fonts/
personas/<slug>/     persona.md, memory/index.md, memory/NNN-slug.md
scripts/mock_server.py   Fake server on 8766 speaking this spec, for UI work
tests/
```

## 2. Models (adda/models.py)

```python
Mode = Literal["bidding", "roundrobin", "manual"]
Thinking = Literal["default", "low", "medium", "high"]

class SeatConfig:      # one seat in one room; stored in room_seats
    id: str            # uuid4 hex[:12]; stable even if the seat is renamed
    room_id: str
    name: str          # display name, unique within the room, e.g. "Ravi", "Scout2"
    program: Literal["claude", "opencode", "pi", "codex"]
    model: str | None  # None = harness default from settings
    thinking: Thinking # "default" when the harness has no such flag
    persona: str | None      # persona slug from the library, or None
    instruction: str | None  # custom instruction text; may be combined with a persona
    color: str         # hex
    enabled: bool
    position: int      # order in the room (roster order; @all uses it)

class Seat(SeatConfig):  # what the UI gets: config plus live facts
    handle: str        # mention handle: name with whitespace removed, lowercased
    available: bool    # harness binary found
    harness_version: str | None

class Room:
    id: str; title: str; folder: str          # absolute path, created on room creation
    mode: Mode; liveliness: int (0..10); budget_usd: float (0 = unlimited)
    spent_usd: float; created_at; ended_at: datetime | None
    seats: list[Seat]

class Message:  # unchanged fields plus:
    seat_id: str | None   # None for owner/system
    author: str           # display name at the time ("owner" stays "owner" for role owner)
    role: Literal["owner","seat","system"]; text; created_at; mentions: list[str]  # seat ids
    cost_usd: float | None; meta: dict  # bid_score, model, latency_ms, woken_by ("mention"|"all"|"wake"|"bid"|"roundrobin")

class Bid: seat: str (seat id); score: float; confidence: float
class SeatReply: unchanged

class Harness:
    program: str; found: bool; path: str | None; version: str | None
    supports_thinking: bool; model_suggestions: list[str]; login_hint: str
    default_model: str | None; default_thinking: Thinking; enabled: bool

class PersonaSummary: slug: str; name: str; role: str; stance: str; color: str; note_count: int
```

## 3. Personas (personas/<slug>/) — library

`persona.md` frontmatter: `name`, `role`, `stance`, `color` (default colour for seats using it).
`program` and `model` are **ignored** if present (v0.1 leftovers). Body = the voice.
Memory folder and notes are unchanged. Memory belongs to the persona: two seats on the same
persona share it. Index lines are `- [title](NNN-slug.md)`.

Rename the four shipped folders to slugs by role: `architecture`, `implementation`,
`users-view`, `tests` (names Ravi, Tuli, Pi, Codex stay inside as `name`).

## 4. Harnesses and seats

`harnesses.py`: `detect_harnesses(settings) -> list[Harness]`. For each program: `shutil.which`,
version via `<bin> --version` (claude prints "x.y.z (Claude Code)"; opencode and pi print a
version; codex per its help; 5 s timeout, failure -> None). `model_suggestions`: claude
`["sonnet","opus","haiku"]` + settings extras; opencode from `opencode models` (cached 10 min,
timeout 15 s, empty on failure); pi `["z-ai/glm-5.3-flash"]` + extras; codex `["gpt-5-codex"]`.
`supports_thinking`: claude `--effort <level>`, pi `--thinking <level>`, opencode
`--variant <level>`, codex `-c model_reasoning_effort=<level>`. Verify each flag's accepted values
with `--help` and record them in the adapter header; map our four levels onto what each accepts
(claude and codex: low/medium/high; pi: low/medium/high; opencode: pass through; "default" sends
nothing).

Seat adapters (`seats/*`) are built per room from `SeatConfig` by `registry.build_adapter(
config, harness_defaults, working_directory=room.folder)`. Working directory is **the room
folder** (sessions are per cwd for claude and pi, so one folder per room keeps resume working).
Other runner rules unchanged: no shell, `ANTHROPIC_API_KEY` stripped, timeout, cancellation
kills the child, `PASS` means silence, nested error messages extracted.

System prompt for a seat = owner line (settings) + persona body (if any) + custom instruction
(if any) + memory index (if persona) + room rules (names of the other seats with handles, how
to mention, PASS). Turn prompt unchanged.

## 5. Turn-taking (adda/scheduler.py)

Per room. On every new message:

1. Owner message cancels the current speaker.
2. **Mentions.** Tokens `@<handle>` matched case-insensitively against enabled seats' handles,
   not preceded by a word character. `@all` = every enabled, available seat in position order.
   `@<prefix>*` = every enabled seat whose handle starts with prefix. Mentioned seats are queued
   in order and bypass bidding. The author is never queued by its own mention.
3. **Budget.** If `budget_usd > 0` and `spent_usd >= budget_usd`: post one system message
   "Budget of $X reached; raise it to continue." and go quiet. Mentions and wakes are also
   refused while over budget (error frame).
4. **Liveliness** L (0..10) replaces `max_auto_turns`:
   - L = 0: paused. No bidding, no round-robin. Only mentions and wake.
   - threshold(L, k) = base(L) + ramp(L) * k, where k = automatic turns since the last owner
     message or wake, base(L) = 9.5 - 0.65 * L, ramp(L) = 0.6 - 0.05 * L.
     L=10: base 3.0, ramp 0.1. L=6: base 5.6, ramp 0.3. L=1: base 8.85, ramp 0.55.
   - bidding: top eligible bid >= threshold speaks; else quiet. Cooldown unchanged.
   - roundrobin: one pass over enabled seats after an owner message, then quiet (L only gates
     whether the pass runs: L=0 disables).
   - A hard safety stop remains at 40 automatic turns per owner message, configurable
     (`ADDA_MAX_AUTO_TURNS`, default 40), to protect against runaway rooms when budget is 0.
5. Broadcast `bids` (bidding only), `typing`, run the seat, post, add cost to `spent_usd`,
   broadcast `room` (spend changed), loop.

Renaming a seat mid-room: `store.rename_seat(seat_id, new_name)` updates `room_seats.name`
and `messages.author` for that seat's messages in that room; sessions key on seat id so resume
continues. Changing model, thinking, persona or instruction takes effect on the seat's next
turn (next prompt; same session unless program changes, which starts a fresh session).

End of room: unchanged (memory notes per persona for seats that spoke; seats without a
persona write no notes).

## 6. Server (adda/server.py)

Pages: `GET /` -> `static/index.html`; `GET /room/{room_id}` -> `static/room.html`;
`GET /settings` -> `static/settings.html`; `/static/*`.

REST (JSON; errors `{"detail": str}` with 4xx):
```
GET  /api/harnesses                       -> [Harness]
GET  /api/personas                        -> [PersonaSummary]
GET  /api/settings                        -> Settings view (section 7), keys masked as booleans
PUT  /api/settings  body = same shape      -> saves config.toml; keys: {"openrouter": "sk-..."} only when present; never echoed
GET  /api/rooms                           -> [Room] newest first (seats included)
POST /api/rooms   body {title, folder?, mode?, liveliness?, budget_usd?, seats: [NewSeat]}
       NewSeat = {name, program, model?, thinking?, persona?, instruction?, color?, copies?: int}
       copies>1 expands to "<name>1".."<name>N" sharing persona/instruction; folder defaults to
       <rooms_dir>/<slug-of-title>-<id>; folder is created (parents ok) -> Room
GET  /api/rooms/{id}                      -> {"room": Room, "messages": [Message]}
PATCH /api/rooms/{id}  body any of {title, mode, liveliness, budget_usd}  -> Room
POST /api/rooms/{id}/seats  body NewSeat   -> Room
PATCH /api/rooms/{id}/seats/{seat_id} body any of {name, model, thinking, persona, instruction, color, enabled, position} -> Room
DELETE /api/rooms/{id}/seats/{seat_id}     -> Room  (messages stay; seat_sessions row removed)
POST /api/rooms/{id}/end                  -> {"room": Room, "notes": {seat_id: [str]}}
GET  /api/search?q=                       -> [{"message": Message, "room_title": str, "snippet": str}]
GET  /api/rooms/last-seats                -> [NewSeat]  (seats of the newest room, for "same as last room")
```
The v0.1 `/api/threads*` and `/api/seats` routes are removed.

WebSocket `WS /ws/{room_id}`:
```
server -> client
{"type":"hello","room":Room,"messages":[Message],"owner":{"name":str},"router":"jev"|"heuristic"}
{"type":"message","message":Message}
{"type":"typing","seat":<seat id>,"state":"start"|"stop"}
{"type":"bids","bids":[Bid],"chosen":<seat id>|null,"threshold":float,"router":str}
{"type":"room","room":Room}            # any change: liveliness, budget, spend, seats, mode, title
{"type":"quiet","reason":"nobody"|"budget"|"paused"|"safety"}
{"type":"ended","notes":{seat_id:[str]}}
{"type":"error","text":str}
client -> server
{"type":"say","text":str}
{"type":"wake","seat":<seat id>}         {"type":"wake_all"}
{"type":"stop"}                          {"type":"end"}
{"type":"room","patch":{liveliness?|budget_usd?|mode?|title?}}      # same as PATCH
{"type":"seat","seat":<seat id>,"patch":{...}}                       # same as PATCH seat
```
All frames go to every tab on the room.

## 7. Configuration (adda/config.py)

Precedence: built-in defaults < `~/.config/adda/config.toml` < environment variables. The
settings view returned by the API marks each field that an env var is overriding.

```toml
[owner]
name = "Riad"
line = "Owner of Adda. Prefers short answers and a decision at the end."

[folders]
rooms_dir = "~/Adda-Rooms"        # each room gets a folder here unless one is given
data_dir = "~/.adda"              # adda.db
personas_dir = "<repo>/personas"

[router]
kind = "jev"                      # jev | heuristic
provider = "openrouter"           # openrouter | typesafe
model = ""                        # empty = provider default
threshold_floor = 3.0             # never below this whatever liveliness says
window = 20

[room_defaults]
liveliness = 6
budget_usd = 1.0
mode = "bidding"
cooldown_s = 20
seat_timeout_s = 180
max_auto_turns = 40

[harness.claude]
model = "sonnet"
thinking = "medium"
enabled = true
extra_models = []
[harness.opencode]   model = "deepseek/deepseek-flash"   thinking = "default"  enabled = true
[harness.pi]         model = ""                          thinking = "default"  enabled = true
[harness.codex]      model = "gpt-5-codex"               thinking = "default"  enabled = false
```
Keys: `~/.config/adda/keys.env` (mode 600, `NAME=value` lines): `OPENROUTER_API_KEY`,
`TYPESAFE_API_KEY`. Lookup order for each: env var, keys.env, then for
OpenRouter the legacy `~/.config/jev/openrouter.env`. The API never returns key values.
`ANTHROPIC_API_KEY` is never read, never stored, and is stripped from seat environments.
Env var names stay as in v0.1 (`ADDA_PORT`, `ADDA_DATA_DIR`, `ADDA_ROUTER`, ...) plus
`ADDA_ROOMS_DIR`, `ADDA_OWNER_NAME`, `ADDA_CONFIG` (path of the toml).

## 8. Pages (static/)

Vanilla, no build, no CDN. Shared `style.css` keeps the v0.1 direction (Imbue + Albert Sans,
graphite ground, taxi-yellow accent, seat colours, light and dark). Reference mockups:
`.taste-skill/mockups/v2/*.html` (11 rooms, 12 new room, 13 room, 14 settings). Match them.

**Rooms page `/`:** list of rooms newest first (title, live/ended/paused tag, seats with colours,
folder, spend/budget, last line); "New room" opens the new-room card on the same page.
Card: title; folder (text field prefilled with `<rooms_dir>/<slug>`, editable); budget (0 =
unlimited, say so); mode; liveliness slider; seats table with rows: colour, name, harness
(installed only, version shown), model (text with datalist from harness suggestions), thinking
(disabled when unsupported), persona (dropdown: none + library), access column **omitted in
v0.2**, copies; a **custom instruction card** under a row appears when the user clicks
"Add instruction" on that row (a textarea in a card; shown automatically when persona is none);
"Same as last room" button fills the seats table from `/api/rooms/last-seats`; a summary line
(seat count, harness count); "Open the room". Right column: harnesses on this machine (from
`/api/harnesses`) with login hints.

**Room page `/room/{id}`:** as v0.1 plus: Markdown rendering of every message (section 9);
Copy on every message (copies the raw Markdown), Copy on every code block, Quote (prefills the
composer with `> ...`), "Copy thread as Markdown"; the owner's messages show the owner's name;
`@` menu lists `@all`, each seat with role and state, and `@<prefix>*` groups when two or more
seats share a prefix; a line under a message that woke several seats ("@all woke Ravi, Tuli, Pi");
"woken by" in the meta line. Right panel: liveliness slider with a one-sentence explanation of
the current threshold, budget field with spend, Pause (sets liveliness 0 and remembers the old
value), Wake all, seat list where each seat card expands to an editor: name, model (datalist),
thinking, persona, instruction (card textarea), colour, enabled, remove; "Add a seat". Edits
send `seat` frames and take effect next turn.

**Settings page `/settings`:** You (name, line); Harnesses table (found, path, version, default
model, thinking, login hint, enabled, re-scan); Personas (list with note counts, link to folder);
Keys and router (router kind/provider/model, three key fields that accept a paste and show only
"set"/"not set"; threshold floor; window); Room defaults; Folders (rooms_dir, data_dir,
personas_dir; env-override badge). Save button; `PUT /api/settings`.

## 9. Markdown (static/markdown.js)

A small subset, modelled on jonaki's parser: paragraphs, headings 1–6 (rendered no larger than
the message's bold line), fenced code with language (`<pre><code class="language-x">`), bullet
and ordered lists, thematic break, GitHub tables with alignment, inline strong, emphasis,
inline code, links (open in new tab, `rel="noopener"`), and `@mentions` highlighted in the seat's
colour. Escape HTML first; never pass raw HTML through. Unknown syntax stays as text. Must not
throw on any input. `renderMarkdown(text, seatsByHandle) -> DocumentFragment`;
`copyText(text)` uses the Clipboard API with a textarea fallback. Tables keep their source for
copy. Unit-test it in the browser with a `tests/markdown.test.html` page that asserts a dozen
cases and prints pass/fail (run by hand in Playwright).

## 10. Testing and verification — **unchanged** plus

Unit tests for: config precedence (defaults, toml, env), key lookup order and masking;
harness detection with faked `which`/subprocess; room creation with copies and folder;
seat rename rewriting authors; mentions (`@all`, prefix `*`, case, handles); liveliness
thresholds and ramp; budget stop; settings PUT round trip; markdown page in Playwright.

## 11. What changes from v0.1 (for whoever edits existing code)

- `Thread` -> `Room` with new columns; `seats` move from persona folders to `room_seats`.
  No data migration needed beyond `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ... ADD COLUMN`
  guarded by `PRAGMA table_info`; v0.1 threads get `folder=""`, `liveliness=6`, `budget_usd=0`.
- `Settings` dataclass gains the toml layer and the new fields; `load_settings` signature kept.
- `Persona` loses `program`/`model` (ignored if present).
- `Scheduler` takes `room` + adapters built from `room.seats`; `max_auto_turns` becomes the
  safety stop; threshold from liveliness; budget; `@all`.
- Scheduler, store and WS frames identify seats by **id**, UI shows names.
- `static/app.js` is split into the files in section 1; v0.1's single page is removed.

## 12. Ownership during the build

Wave 1 (parallel, disjoint files):
- **S1 backend data:** `adda/config.py`, `adda/models.py`, `adda/store.py`, `adda/personas.py`,
  `adda/harnesses.py`, the four `personas/*` folders (rename + frontmatter), and their tests.
- **S3 markdown + logos:** `static/markdown.js`, `tests/markdown.test.html`,
  `.taste-skill/mockups/logos/index.html` (five logo options, inline SVG; not part of the app).
- **S4 pages + mock:** `static/index.html|room.html|settings.html`, `static/rooms.js|room.js|
  settings.js|common.js`, `static/style.css`, `scripts/mock_server.py`. Builds against the mock.
Wave 2 (after S1 lands):
- **S2 backend behaviour:** `adda/scheduler.py`, `adda/server.py`, `adda/prompts.py`,
  `adda/seats/*`, `adda/cli.py`, `README.md`, and their tests.
Main session: integrates, runs the real server, Playwright, live seats, commits.

---

# v0.3 delta (2026-10-14)

Everything above stays unless this section says otherwise.

## D1. Turn-taking calibration (scheduler.py, config.py)

Observed Jev scores in real rooms sit between 2 and 7; the v0.2 curve (base 9.5 - 0.65 L) was
tuned for a 1..10 spread and stopped rooms at liveliness 8 after a few turns. New rules:

- base(L) = 7.5 - 0.5 L, ramp(L) = 0.3 - 0.025 L. L=10: 2.5 + 0.05k. L=8: 3.5 + 0.1k.
  L=6: 4.5 + 0.15k. L=1: 7.0 + 0.275k. Floor default 2.5 (`router.threshold_floor`).
- k counts **bid-driven turns only** since the last owner message or wake. Mention, @all,
  wake and round-robin turns do not raise k.
- Cooldown (superseded 2026-10-19): it was a `-1.5` penalty; it is now a per-room exclusion,
  see H1. The author of the latest message is still excluded from the next round.
- Per-seat **eagerness** (D2) adjusts every bid: `adjusted = score + 0.4 * (eagerness - 5)`,
  clamped to 0..10. Eagerness 0 means the seat never bids (mentions and wake only).
- `bids` frame: each bid carries `score` (adjusted, what the threshold compares against) and
  `raw_score` (Jev's number).
- A PASS reply is broadcast as `{"type":"passed","seat":<id>}` and logged at INFO
  ("seat %s passed"); every quiet decision is logged at INFO with reason, threshold, top seat
  and top adjusted score. Nothing is stored for a pass.

## D2. Eagerness per seat

`SeatConfig.eagerness: int` (0..10, default 5), column `room_seats.eagerness` added with
`ALTER TABLE` guarded by `PRAGMA table_info`. Accepted in NewSeat and seat PATCH. Shown as a
slider labelled "Eagerness" on every seat row of the new-room card and in the room's seat
editor, with the live sentence "bids +0.8" / "-2.0" / "never bids" next to it.

## D3. Folder browser

`GET /api/folders?path=<absolute path or empty>` -> `{"path": str, "parent": str | null,
"folders": [{"name": str, "path": str}]}`. Empty path means `rooms_dir`. Only paths inside the
owner's home directory are allowed (400 otherwise); hidden folders are skipped; sorted by name.
`POST /api/folders` body `{"path": str, "name": str}` creates a subfolder (same restriction;
409 if it exists) and returns the new entry. UI: a "Browse" button next to the room folder
field opens a dialog listing folders with Up, Open (double-click or button), "New folder here"
and "Choose this folder". The dialog is a real overlay (shadow allowed).

## D4. Default room folder name

`<rooms_dir>/<YYYYMMDD>-<slug-of-title>` using the local date. No id suffix. If the folder
exists, append `-2`, `-3`, … The rooms page prefills the field the same way.

## D5. Model suggestions (harnesses.py)

- claude: `fable, opus, sonnet, haiku, claude-fable-5-1, claude-opus-5-5, claude-sonnet-5-5,
  claude-haiku-5-5` + extras.
- pi: `enabledModels` plus `defaultModel` from `~/.pi/agent/settings.json` (JSON; path
  injectable; missing file -> fall back to `z-ai/glm-5.3-flash`) + extras. Keep the
  `provider/` prefix exactly as the file has it.
- opencode: unchanged (`opencode models`). codex: `gpt-5-codex` + extras.
The datalist on every model field lists all suggestions; typing anything else still passes
through.

## D6. Named copies

The new-room card drops the Copies field. Each seat row gets a "Duplicate" button that clones
the row (harness, model, thinking, persona, instruction, eagerness, colour) with the name
suffixed by the next free number (Scout -> Scout2 -> Scout3), and every row's name stays
editable. The API keeps `copies` for compatibility; the UI no longer sends it.

## D7. Mode switch and cache

The room panel's "Who speaks next" radios must send `{"type":"room","patch":{"mode":…}}` and
reflect `room` frames. All three pages load `style.css` and every script with `?v=0.3`
(bump on each release) so a plain reload never shows a stale page.

## D8. Logo

Option 5 ("Circle and owner") from `.taste-skill/mockups/logos/index.html` (symbols `full5`
and `fav5` in that file) becomes `static/logo.svg` (full mark, square viewBox) and
`static/favicon.svg` (the 16 px form). All pages: `<link rel="icon" type="image/svg+xml"
href="/static/favicon.svg?v=0.3">`, and the header wordmark shows the mark at 28 px before
"ADDA". README shows it.

## D9. Ownership

- **S5 backend:** `adda/**`, `tests/**`, `README.md` (D1, D2, D3 API, D4, D5, tests).
- **S6 frontend:** `static/**`, `scripts/mock_server.py` (D2 UI, D3 UI, D4 prefill, D5
  datalists, D6, D7, D8, `passed` line, bids `raw_score` tooltip).

---

# v0.4 delta (2026-10-14): a room, not a turn loop

Owner decision: Adda is not turn-based. Several seats may compose at once, a seat may finish
its own thought, and seats come back with afterthoughts while the owner is away. Replaces
SPEC §5 and v0.3 D1 where they conflict.

## E1. Scheduler model (adda/scheduler.py)

Event-driven, no single driver loop. Per room:

- `speaking: dict[seat_id, asyncio.Task]` (a seat composes at most one reply at a time);
  `follow_up: set[seat_id]` (a mention arrived while the seat was speaking: run it once more
  when it finishes, with the new messages); `bid_turns_since_owner: int`; `more_streak:
  dict[seat_id, int]`; `afterthought: asyncio.Task | None`; `afterthoughts_this_spell: int`.
- `on_message(message)` runs after every posted message (owner or seat):
  1. Budget and ended checks (unchanged). Safety stop: `max_auto_turns` automatic starts since
     the last owner message.
  2. Mentions (`@handle`, `@all`, `@prefix*`): start each mentioned seat now if idle, else mark
     `follow_up`. Never the author itself.
  3. `[more]`: if a seat's reply ends with a line that is exactly `[more]` (case-insensitive),
     strip it before storing/broadcasting, and start the same seat again immediately without a
     bid (woken_by "more"), at most `more_limit` (default 2) times in a row.
  4. Bidding (mode bidding, liveliness > 0, and the message is not itself a `[more]`
     continuation): one Jev round over enabled, available seats that are **not speaking**. The
     author is included with a `self_reply_penalty` (default 1.5). Adjusted score as v0.3
     (eagerness, optional cooldown). Start every seat whose adjusted score >= threshold, highest
     first, until `room.max_concurrent` seats are speaking (default 2; 1 reproduces v0.3).
     Each start raises `bid_turns_since_owner`.
  5. Round-robin: unchanged pass, one at a time. Manual: mentions only.
  6. If after all this nobody is speaking: schedule an afterthought (E2) and broadcast
     `quiet` with `next_tick_in_s`.
- A seat's prompt holds the messages it has not seen at the moment it **starts**. Replies may
  cross; that is intended. The system prompt tells seats so, and tells them about `[more]`.
- Owner messages no longer cancel speakers. `stop` cancels every speaking seat and the pending
  afterthought. `wake`/`wake_all` start seats directly (mark follow_up if speaking).
- `end` cancels everything first, then collects notes as before.
- Seat patch that changes program while speaking cancels that seat's task.

## E2. Afterthoughts

When the room is quiet (nobody speaking, no pending start) and liveliness > 0 and mode is
bidding: schedule one tick after `delay = (11 - L) * 20 s * uniform(0.7, 1.3)`, scaled per
room by `afterthought_scale` (default 1.0). At the tick run a Jev round with the afterthought
question ("Does @name have something worth adding that nobody has said, an afterthought
the room would want to hear?") and start seats >= threshold as in E1.4 (woken_by
"afterthought"). At most `afterthought_limit` (default 3) ticks per quiet spell; the spell
resets on an owner message or wake. Each tick costs one Jev round; a seat is only paid for
when it speaks. Eagerness scales the per-seat delay: the first tick's delay is multiplied by
`1 - 0.05 * (max_eagerness_in_room - 5)`.

## E3. Config and models

`Room.max_concurrent: int` (1..4, default from `room_defaults.max_concurrent` = 2), column
added with a guarded ALTER; PATCH/`room` frame accept it. Settings `[room_defaults]` gains
`max_concurrent = 2`, `self_reply_penalty = 1.5`, `more_limit = 2`, `afterthought_limit = 3`,
`afterthought_scale = 1.0`. Message.meta.woken_by adds "more" and "afterthought".

## E4. Frames

```
{"type":"bids","bids":[Bid],"chosen":[<seat id>, ...],"threshold":float,"router":str,"kind":"message"|"afterthought"}
{"type":"typing","seat":id,"state":"start"|"stop"}     # several seats may be "start" at once
{"type":"quiet","reason":"nobody"|"paused"|"safety"|"budget","next_tick_in_s":float|null,"ticks_left":int}
{"type":"room","room":Room}                           # Room now has max_concurrent
```
`chosen` is a list (was a single id). REST and WS room patches accept `max_concurrent`.

## E5. UI (static/room.js, room.html, common.js, mock)

- Several typing indicators at once; seat cards show "speaking" for each.
- Room panel: "Voices at once" select 1..4 (sends `max_concurrent`).
- Quiet notice reads "Quiet. Someone may come back in about 40 s (2 more chances)." and counts
  down; when `next_tick_in_s` is null it reads as before.
- Bids tooltip and meters handle a `chosen` list. Meta line shows "afterthought" and
  "continued" (for "more").
- Mock: emit two typing starts, crossed replies, an afterthought after a delay, a `[more]`
  continuation.

## E6. Tests

Concurrent starts capped by max_concurrent; author included with penalty; `[more]` stripped
and continued at most more_limit; follow_up after a mention during speaking; afterthought
scheduled with the delay formula (fake clock/sleep), cap per spell, reset on owner message;
owner message does not cancel; stop cancels all; budget and safety still hold; round-robin
still sequential; frames shapes.

## E7. Ownership

- **S7 backend:** `adda/**`, `tests/**`, `README.md`.
- **S8 frontend:** `static/**`, `scripts/mock_server.py`.

---

# v0.5 delta (2026-10-14): threads inside rooms, memory review, open folder, fonts

## F1. Fonts

Display (wordmark, h1, h2, panel titles): **Instrument Sans**, weight 650–750, tracking -0.02em.
Body and interface (messages, meta, forms, buttons, seat cards): **Manrope**, 17 px / 1.55,
`font-variant-numeric: tabular-nums` on meta, seat cards and forms. Code: **IBM Plex Mono**.
Bengali wordmark: system "Noto Sans Bengali" fallback, not bundled. Font files: copy
`.taste-skill/mockups/fonts/files/{instrumentsans,manrope,plexmono}.ttf` to `static/fonts/`
and delete `imbue-latin.woff2` and `albert-sans-latin.woff2`. All pages and the mock use them.

## F2. Threads inside a room (a room is a project)

- New table `threads(id, room_id, title, created_at, ended_at)`. `messages.thread_id` (guarded
  ALTER). Migration: every existing room gets one thread titled after the room, all its
  messages assigned to it. `seat_sessions` keyed by `(thread_id, seat_id)` (a tool session
  lasts one thread; old rows dropped as in v0.2).
- Room keeps seats, folder, budget, spend, liveliness, mode, max_concurrent. `Room.ended_at`
  means archived. Threads end individually; ending a thread writes memory notes.
- `ThreadSummary = {id, room_id, title, created_at, ended_at, message_count, last_author, last_line}`;
  `Room.threads: list[ThreadSummary]` newest first.
- REST: `GET /api/rooms/{id}/threads`, `POST /api/rooms/{id}/threads {title}` -> Thread,
  `GET /api/threads/{tid}` -> `{"thread", "room", "messages"}`, `PATCH /api/threads/{tid}
  {title}`, `POST /api/threads/{tid}/end` -> `{"thread", "notes"}`. `GET /api/rooms/{id}` keeps
  returning the room (with `threads`) and the messages of the newest open thread for
  compatibility. `POST /api/rooms` creates a first thread titled like the room.
- WebSocket: `WS /ws/threads/{thread_id}`; `hello` carries `room`, `thread`, `messages`,
  `owner`, `router`. Frames unchanged otherwise; `room` frames still broadcast to every
  thread of that room (spend, seats). New `{"type":"thread","thread":ThreadSummary}` on
  title change or end. `end` over WS ends the thread.
- Scheduler instance per thread; budget and spend are read from the room. Afterthought
  ticks per thread.
- Search hits carry `thread_id` and `thread_title`.
- Pages: `/room/{room_id}` opens the newest open thread; `/room/{room_id}/thread/{thread_id}`
  opens a specific one. The room page gets a left column: thread list (title, last line,
  message count, ended tag), "New thread" with a title field, rename on double-click.

## F3. Memory review

- `write_memory_note(persona, title, body, source=None)` writes frontmatter
  `room`, `room_title`, `thread`, `thread_title`, `written` (ISO) when `source` is given.
  `list_memory_notes(persona) -> [MemoryNote(file, title, body, room_id, room_title,
  thread_id, thread_title, written)]` (legacy notes have None fields). `update_memory_note`,
  `delete_memory_note`, and `rebuild_memory_index(persona)` regenerating `index.md` from files.
- REST: `GET /api/personas/{slug}/memory` -> [MemoryNote]; `PUT /api/personas/{slug}/memory/{file}
  {title?, body?}`; `DELETE /api/personas/{slug}/memory/{file}` (file names validated against
  `^\d{3}-[a-z0-9-]+\.md$`); `GET /api/rooms/{id}/memory` -> `{persona_slug: [MemoryNote]}` for
  notes whose `room` is this room.
- UI: a "Memory" button in the room header opens a drawer with tabs: one per seat (its
  persona's notes, newest first, with inline edit and delete; seats without a persona show
  "no persona, so no memory") and "This room" (notes written from this room, grouped by seat).

## F4. Open folder

`POST /api/rooms/{id}/open-folder` runs the platform opener detached (`xdg-open` on Linux,
`open` on macOS, `explorer` on Windows; injectable for tests) on the room folder, creating
it if missing; returns `{"opened": path}`; 400 if the folder is outside home. UI: an
"Open folder" button beside the folder path on the room page and on each room card.

## F5. Ownership

- **Opus A backend:** `adda/**`, `tests/**`, `README.md`.
- **Opus B frontend:** `static/**`, `scripts/mock_server.py`.

---

# v0.6 delta (2026-10-14): access levels

## G1. Model

`Access = Literal["talk", "look", "work"]`. `Room.access: Access` (default "talk", guarded ALTER)
and `SeatConfig.access: Access | None` (None = room level, guarded ALTER). Effective access for
a seat = seat.access or room.access. Settings `[room_defaults] access = "talk"`.
`POST /api/rooms`, room PATCH / `room` frame accept `access`; NewSeat and seat PATCH / `seat`
frame accept `access` (null clears). Seat JSON carries `access` and `effective_access`.

What each level means (the folder is the room folder; seats run with cwd = room folder):
- **talk**: no tools. Today's behaviour.
- **look**: read-only tools inside the room folder: read files, list, search. No shell, no
  writes, no network, no MCP.
- **work**: read, edit, write and run shell commands inside the room folder. No approval
  prompts in v0.6: this is full trust inside that folder. No worktree yet. The UI says so
  in red next to the control and in the new-room summary line.

## G2. Harness flags (verify every flag with `--help`; record what was verified in each
adapter's header; anything unverifiable is marked and falls back to talk)

- claude: talk `--tools ""`; look `--tools Read,Grep,Glob,LS --add-dir <folder>
  --permission-prompts none`; work `--allowedTools Read,Grep,Glob,LS,Edit,Write,MultiEdit,Bash
  --permission-mode acceptEdits --permission-prompts none --add-dir <folder>`. Check the
  exact tool names and whether `--tools` accepts a comma list or repeated values.
- pi: talk `--no-tools --no-mcp --no-context-files --no-skills`; look `--tools read,grep,find,ls
  --no-mcp`; work `--no-mcp` (all built-in tools). Check tool names via `pi --help`.
- opencode: talk/look/work via a per-run config file passed with the `OPENCODE_CONFIG`
  environment variable (check `opencode --help` and the installed docs for the variable name
  and the `permission` block shape: edit/bash/webfetch allow|ask|deny). "ask" is impossible
  headless, so look = deny edit/bash/webfetch; work = allow edit/bash, deny webfetch.
- codex (not installed, unverified): look `--sandbox read-only --ask-for-approval never`;
  work `--sandbox workspace-write --ask-for-approval never`.
Each adapter's `build_command(prompt, system_prompt, session_id, access)` gains the access
argument. The system prompt tells the seat its level in one sentence ("You may read files in
<folder>" / "You may read, edit and run commands in <folder>" / "You have no tools").

## G3. UI

- Room panel: an "Access" select (Talk only / Look / Work) under "Voices at once", with a
  one-line description and, for Work, a red warning "Seats may edit files and run commands in
  <folder> without asking." Sends `room` patch.
- New-room card: the same select next to Liveliness; per-seat rows get an Access select
  (room level / Talk only / Look / Work); the summary line names the level and the folder.
- Seat editor in the room: the same per-seat select. Seat cards show a small "look"/"work"
  marker when the effective level is not talk.
- Room cards on the rooms page show the level when not talk.

## G4. Ownership

- **S9 backend:** `adda/**`, `tests/**`, `README.md`.
- **S10 frontend:** `static/**`, `scripts/mock_server.py`.

# v0.7 delta (2026-10-19): cooldown as the owner's pace control

## H1. Cooldown per room
- `Room.cooldown_s` (float, >= 0, default from `room_defaults.cooldown_s`, 0 = off) replaces
  the server-wide penalty. Set in the New room form, patched from the room panel
  (`cooldown_s` in the room patch), stored in `rooms.cooldown_s` (added in place).
- A seat that posted a reply is left out of bidding until `cooldown_s` seconds after the reply
  landed. A PASS starts no cooldown. Mentions, @all, wake and `[more]` ignore it.
- An owner message, wake or wake-all clears every cooldown.
- When the room is idle and a seat is cooling, the scheduler broadcasts `quiet` with reason
  `cooldown` and `next_tick_in_s`, sleeps until the earliest wait ends, and runs one more
  bidding round. This replaces the v0.2 silence from exclusion with no re-trigger.
