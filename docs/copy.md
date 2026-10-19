# Adda interface copy

`tests/test_ui_copy.py` fails when a visible string uses a banned word; this file holds the vocabulary, the rules and the record of what changed.

## Vocabulary

Product nouns allowed in labels, buttons, hints and status lines: room, thread, seat, agent (the installed program a seat runs: claude, opencode, pi, codex), model, persona, memory, note, budget, liveliness, eagerness, mention, wake, pause, end, folder, access, key, owner.

Banned in visible strings (whole word, any case, plurals included): harness, router, jev, bid, score, session, system prompt, PATH, mode 600, shell variable, provider default, heuristic, frame, patch, slug, threshold, automatic turn. Two texts are allowed on purpose: the settings path `~/.config/adda/config.toml` and the option "Automatic (Jev)". Bid numbers stay only in the per-message meta line ("bid 7.5") and the seat meter tooltip.

## Rules

1. A label is a noun phrase of at most three words, never a question or a sentence.
2. A button is a verb plus its object: "Create room", "Add seat", "End thread".
3. A hint is one line of at most twelve words that states the effect, and exists only when the label alone is not enough.
4. A status line says what happened, then what the owner can do.
5. An error names what failed and offers one action, with no internal names. Toasts show the server's text without the API path or HTTP status; those go to the console.
6. The best hint is none: if the owner loses nothing without a string, it is deleted.

Voice: calm, plain, second person where a person is addressed, no exclamation marks, no jokes.

## Changed strings (old -> new: reason)

Rooms page
- Intro sentence -> deleted: rule 6
- "Harnesses on this machine" -> "Agents on this machine"; "Change harnesses, models and keys" -> "Agents, models and keys": vocabulary
- Pills "found"/"not installed" -> "Installed"/"Not installed"; login instruction shown only for agents that are not installed, without backticks: rule 6
- "Open the room" -> "Create room"; "Before you open it" -> deleted (summary line kept): rules 2, 6
- Room folder hint -> "Created if it does not exist.": rule 3
- Budget hint -> "In dollars. 0 means no limit.": plain words
- "Who speaks next" -> "Speaking order"; options -> "Automatic", "Take turns", "Only when mentioned": vocabulary
- "Voices at once" -> "Seats replying at once", hint deleted: rule 6
- Liveliness sentence with bid numbers -> "How readily seats join in.": numbers the owner cannot act on
- Seats intro -> "Two seats with one persona share its memory.": rule 3
- New field "Cooldown (seconds)" + "A seat stays quiet this long after replying. 0 turns it off.": the owner's pace control, also in the room panel
- "Add a seat" -> "Add seat"; "Copy seats from last room" -> "Same seats as last room": rule 2
- Folder dialog "Go up" / "New folder here" / "Choose this folder" -> "Up" / "New folder" / "Choose": rule 2
- "No rooms yet. Start one with New room." -> nothing: the New room button is beside it
- "There is no earlier room to copy seats from." -> "No earlier room to copy from.": shorter
- Seat field "Harness" -> "Agent"; model placeholder "harness default" -> "Default"; summary "2 harnesses" -> "2 agents": vocabulary
- Toast "Could not open the room" -> "Could not create the room": matches the button

Room page
- Composer placeholder -> "Write a message. @name or @all calls a seat."; hint -> "Enter to send, Shift+Enter for a new line": rule 3
- "End discussion" -> "End thread"; "Copy thread as Markdown" -> "Copy thread"; toast -> "Thread copied.": rule 2
- "This thread" -> "Thread"; memory tab "This room" -> "Room"; new-thread placeholder -> "New thread title": rule 1
- "Double-click a title to rename it." -> deleted: the thread link's tooltip says it
- "Mode" -> "Speaking order" with the three options above; room meta shows the same names
- Access descriptions -> "Seats can only talk." / "Seats can read files in the room folder." / "Seats can edit files and run commands in the room folder."; warning -> "Seats can change files in X without asking.": rule 3
- Eagerness "bids +2.0"/"never bids"/"bids unchanged" -> "speaks more"/"speaks less"/"never speaks on its own"/"normal": banned word
- Seat editor "Harness: claude 2.1.302. Edits apply on the next turn." -> "Agent: claude 2.1.302. Edits apply from the next reply.": vocabulary
- Seat field "Custom instruction" -> "Instruction"; placeholder -> "Extra instruction for this seat": rule 3
- Meter tooltip "Jev scored 6.0; with eagerness 7 the bid is 6.8" -> "Urge to speak 6.8, 6.0 before eagerness 7"; "No bid yet" -> "Quiet"; meter label "last bid" -> "urge to speak": banned words
- "Router: Jev" / "Router: heuristic fallback" -> nothing, or "Simple turn-taking (no network)" when the fallback runs: rule 6
- Quiet notices -> "Nobody has anything to add. Say something or wake a seat." / "Quiet. A seat may add a thought in about 40 s." / "Paused. Mention a seat or press Wake all." / "Paused after 40 replies without you. Say something to continue." / "Cooling down. A seat may reply in 12 s. Say something to skip the wait.": rule 4
- Meta line "round-robin" -> "in turn"; @all menu "every seat answers, in roster order · 3 turns" -> "every seat replies, in order · 3 replies": plain words
- Memory drawer intro -> deleted: rule 6. Empty states -> "No notes yet." / "No notes from this room yet." / "X has no persona."; header -> "X as P · 3 notes"; delete confirm -> "Delete the note “T”?": rule 6
- "This room has no thread yet. Start one with New thread." -> "No thread yet."; ended and archived hints lose their second sentence: rule 6
- "That thread is not in this room; opening the newest one." -> "Opened the newest thread." (no longer an error toast): rule 4
- "Not connected. Try again when the room is live." toast and "Not connected. Messages can't be sent." hint -> deleted: the Live pill reads Offline
- "Could not load harnesses and personas" -> "Could not load agents and personas": vocabulary

Settings page
- Intro -> "Saved to ~/.config/adda/config.toml."; "env" pill -> hint "Set by your shell" under the field: rule 6
- Section names "Harnesses" -> "Agents", "Keys and router" -> "Turn-taking and keys": vocabulary
- "Your name in the room" hint -> deleted: rule 6. "Your line for the seats" -> "About you", hint -> "Shown to every seat.": banned word
- Agents note -> "Defaults for seats that do not set their own."; columns "Harness"/"Default model" -> "Agent"/"Model"; "not on PATH" -> "Not installed"; login line only when not installed; "Re-scan PATH" -> "Look again"; toast -> "Agents checked."; error -> "Could not check the agents"
- Personas note -> "A voice and a memory a seat can take."
- "Who decides who speaks" -> "Turn-taking", options "Automatic (Jev)" / "Simple (no network)"; model placeholder -> "Default"; key pills "Set"/"Not set"; key placeholders -> "Paste a new key to replace it" / "Paste a key"; key note -> "Saved with Save and never shown again."
- "Lowest score that may speak" -> "Minimum urge to speak"; "Messages the router sees" -> "Messages considered"
- Room defaults: "Budget (USD)" -> "Budget" + dollars hint; "Cooldown (s, 0 = off)" -> "Cooldown (seconds)" + "0 turns it off."; "Seat timeout (s)" -> "Reply timeout (seconds)"; "Safety stop (turns)" -> "Pause after (replies)" + "Replies without you before the room pauses."; "Who speaks next" -> "Speaking order"
- Folder hints -> "New rooms get a folder here." / "Adda's own data."
- Agents: "Base URL" + hint "Empty uses your Claude login." and key "Claude provider key" (2026-10-19): a Claude seat on OpenRouter or a proxy instead of the Max login

Room page, billing and limits (2026-10-19)
- Meta line: "$0.012 at API prices" for a subscription seat (nothing was charged), "3.1k cached · 0.4k new · 0.2k out" when the agent reports tokens
- Seat card state "Limit reached · back 14:00"; system messages "X has reached its plan's limit. It will be tried again at 14:00." / "... in 30 min." / "X is at its plan's limit until 14:00.": rule 4

Server messages
- "Seat X: program claude not found." -> "X cannot reply: claude is not installed."; "Seat X failed: D" -> "X could not reply. D"; "Seat X: E" -> "X cannot reply: E"
- "turn-taking failed: E" -> "Could not pick the next speaker. E"; "Memory notes for X not written: E" -> "X's notes were not written. E"
- "Router jev failed (E); using heuristic." -> "The turn-taking service is unavailable (E). Using simple turn-taking."
- "title must not be empty" / "seat name must not be empty" / "a seat named 'X' already exists in this room" / "seat X is disabled" / "no enabled seat is available to wake" -> "The title is empty." / "The seat needs a name." / "A seat named X already exists." / "X is disabled." / "No seat can be woken."
