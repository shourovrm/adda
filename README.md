# Adda

Adda (আড্ডা, Bengali for a relaxed group chat) is a local chat room where several AI coding agents installed on this machine, called seats, and one human, the owner, discuss one issue in one shared thread. A Python server with FastAPI and WebSockets runs the rooms, three static pages are the UI, and one subprocess adapter drives each agent program. A decision model called Jev scores who speaks next.

![The rooms page: a list of rooms and the new-room card](static/screenshot-rooms.png)

## Requirements

- Python 3.12 or newer.
- [uv](https://docs.astral.sh/uv/).
- The agent programs on `PATH`: `claude`, `opencode`, `pi`; `codex` is optional and off by default. A seat whose program is missing is shown as unavailable.

## Install

```
uv sync
```

## Run

```
uv run adda serve
```

Open http://127.0.0.1:8765. Other commands:

- `uv run adda rooms` lists rooms with status, mode, liveliness and spend.
- `uv run adda search <words>` searches every message of every room.
- `uv run adda harnesses` prints which agent programs are installed, with versions and default models.

## Test

```
uv run pytest
uv run ruff check .
uv run ruff format .
```

`uv run pytest` runs unit tests only. `uv run pytest -m integration` runs a real `claude -p` round trip, spends rate limit, and is run by hand only.

## Pages

- `/` is the rooms page. It lists rooms newest first and holds the new-room card: title, folder, budget, mode, liveliness, and a table of seats (harness, model, thinking, persona, copies, custom instruction). "Same as last room" fills the table from the newest room.
- `/room/<id>` is the chat room. Messages render Markdown; every message and code block has Copy; Quote prefills the composer. The right panel holds the liveliness slider, the budget and spend, Pause, Wake all, and one editor per seat.
- `/settings` edits the settings file, pastes keys, and shows which harnesses were found.

![The room page](static/screenshot-room.png)

![The settings page](static/screenshot-settings.png)

![The room page on a phone](static/screenshot-mobile.png)

## Rooms and folders

Each room has a folder. It is the working directory of every seat in that room, so the sessions of `claude` and `pi` (which are stored per directory) resume correctly. If the new-room card leaves the folder empty, Adda creates `<rooms_dir>/<title-slug>-<room id>`, by default under `~/Adda-Rooms`. A folder you type is created, with parents, if it does not exist. Seats never get file or shell tools; they only talk, so the folder is context for the program and a place for you to keep files about the discussion.

A seat row with "copies" 3 and the name `Scout` becomes three seats `Scout1`, `Scout2`, `Scout3` that share the persona and the custom instruction.

## Turn-taking

After every message Jev scores each seat's urge to speak from 0 to 10, and the top score speaks if it reaches the threshold. Three controls shape that:

- **Liveliness** (0 to 10, per room) sets the threshold. With `k` automatic turns since the last owner message or wake, the threshold is `max(floor, base + ramp * k)` where `base = 9.5 - 0.65 * liveliness` and `ramp = 0.6 - 0.05 * liveliness`. At liveliness 10 the first seat speaks at a score of 3.0 and each further turn raises the bar by 0.1; at liveliness 1 it needs 8.85 and the bar rises by 0.55 per turn. The floor is the router setting `threshold_floor` (3.0). Liveliness 0 pauses the room: no bidding and no round-robin, only mentions and Wake.
- **Budget** (USD, per room, 0 means unlimited). Seats report their cost; when the spend reaches the budget the room posts "Budget of $X reached; raise it to continue." and goes quiet. Mentions and wakes are refused until you raise the budget.
- **Safety stop**: at most `max_auto_turns` (40) automatic turns follow one owner message, whatever the budget.

Mentions bypass bidding. `@ravi` wakes one seat (case-insensitive; a handle is the seat name without spaces, lowercased). `@all` wakes every enabled, installed seat in roster order. `@scout*` wakes every seat whose handle starts with `scout`. A seat is never woken by its own message. An owner message cancels the seat that is currently speaking. A seat that just spoke is on cooldown (`cooldown_s`, 20 s) for bidding but not for mentions.

The room mode sets who is scored: `bidding` (default), `roundrobin` (one pass over the enabled seats after an owner message, then quiet), or `manual` (mentions and the wake buttons only).

## Settings file and keys

Settings come from three layers, each overriding the one before: built-in defaults, `~/.config/adda/config.toml` (path in `ADDA_CONFIG`), and environment variables. The settings page writes the file; a field that an environment variable is setting is marked as such and is not written back.

```toml
[owner]
name = "Riad"
line = "Owner of Adda. Prefers short answers and a decision at the end."

[folders]
rooms_dir = "~/Adda-Rooms"
data_dir = "~/.adda"            # adda.db
personas_dir = "<repo>/personas"

[router]
kind = "jev"                    # jev | heuristic
provider = "openrouter"         # openrouter | typesafe
model = ""                      # empty = provider default
threshold_floor = 3.0
window = 20                     # messages the router sees

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
```

Environment variables: `ADDA_HOST`, `ADDA_PORT`, `ADDA_CONFIG`, `ADDA_OWNER_NAME`, `ADDA_OWNER_LINE`, `ADDA_ROOMS_DIR`, `ADDA_DATA_DIR`, `ADDA_PERSONAS_DIR`, `ADDA_ROUTER`, `ADDA_JEV_PROVIDER`, `ADDA_JEV_MODEL`, `ADDA_THRESHOLD_FLOOR`, `ADDA_ROUTER_WINDOW`, `ADDA_LIVELINESS`, `ADDA_BUDGET_USD`, `ADDA_MODE`, `ADDA_COOLDOWN_S`, `ADDA_SEAT_TIMEOUT_S`, `ADDA_MAX_AUTO_TURNS`.

Keys live in `~/.config/adda/keys.env` (mode 600, `NAME=value` lines; path in `ADDA_KEYS_FILE`): `OPENROUTER_API_KEY`, `TYPESAFE_API_KEY`, `DEEPSEEK_API_KEY`. For each key Adda looks at the environment variable first, then `keys.env`, and for OpenRouter finally the older `~/.config/jev/openrouter.env`. The settings page accepts a pasted key and afterwards shows only "set" or "not set"; the API never returns a key. Without a key for the chosen Jev provider the server starts anyway and uses the heuristic router (mentioned seats score 10, the seat silent longest scores 6). If a Jev request fails, the server uses the heuristic for that turn and posts one system message per room.

## Personas library

A persona is a folder `personas/<slug>/` holding a voice and a memory. Seats in any room can pick one; two seats on the same persona share its memory.

```
personas/<slug>/
  persona.md          frontmatter + the voice, in second person
  memory/index.md     one line per note: - [title](NNN-slug.md)
  memory/NNN-slug.md  one fact per note
```

`persona.md` starts with YAML frontmatter with `name`, `role`, `stance` and `color` (the default colour of seats using it). `program` and `model` are ignored; the harness is chosen per seat. Pick a mid-luminance colour that reads on both light and dark themes. Memory notes are never written by hand: when you end a room, the server asks each seat that spoke and has a persona what to remember (at most 3 bullets) and appends each bullet as a note. Seats without a persona write no notes. A tool session lasts one room.

A seat can also carry a custom instruction, alone or on top of a persona. The system prompt of a seat is: your line from the settings, the persona voice, the custom instruction, the persona's memory index, then the room rules (the other seats and their `@handles`, `@all`, PASS, and that seats only talk).

## Adding a harness default

The four programs are `claude`, `opencode`, `pi` and `codex`. Each has a `[harness.<program>]` table in the settings file with the model and thinking level used when a seat leaves them blank, an `enabled` flag that hides the program from the new-room card, and `extra_models`, extra names offered in the model drop-down. Example for `pi`:

```toml
[harness.pi]
model = "z-ai/glm-5.3-flash"
thinking = "low"
enabled = true
extra_models = ["some/other-model"]
```

Thinking `low`, `medium` and `high` are sent as `claude --effort`, `pi --thinking`, `opencode run --variant` and `codex -c model_reasoning_effort=`; `default` sends nothing. The codex flags are unverified because `codex` is not installed on the development machine. A new agent program needs an adapter in `adda/seats/` (see `claude.py`) and an entry in `adda/seats/registry.py`.

## Claude seat and the Max login

The Claude seat runs the unmodified `claude` binary under the owner's Max login. Do not set `ANTHROPIC_API_KEY` in the server environment: headless `claude` prefers it and would bill the API. The server also deletes it from every seat subprocess environment and never reads, stores or forwards it. The server never reads, copies or forwards the login token. The room is for one owner on one machine; serving the Claude seat to other people makes it a product and requires an API key. The seat shares the Max rate limit with the owner's own interactive Claude sessions.

## Frontend development without seats

`scripts/mock_server.py` speaks the REST and WebSocket protocol with canned data and binds port 8766, so the pages can be worked on without real seats.

```
uv run python scripts/mock_server.py
```

Open http://127.0.0.1:8766.

## Licence

MIT, see [LICENSE](LICENSE).
