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
`TYPESAFE_API_KEY`, `DEEPSEEK_API_KEY`. Lookup order for each: env var, keys.env, then for
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
- Cooldown is a penalty, not an exclusion: a seat still cooling bids at `score - 1.5`. The
  author of the latest message is still excluded from the next round.
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
