# Adda design system

The rules follow the GOV.UK Design System (https://design-system.service.gov.uk) for controls, forms and
spacing, and Slack and Discord for the shape of the chat page. Every number here is a CSS custom property in
the `:root` block of `static/style.css`. `tests/test_style_tokens.py` fails when a rule or a stylesheet value
is outside this spec, so a change to this file and a change to the tokens land in the same commit.

## Type

Body and every control use Manrope at 16px with line-height 1.5. Instrument Sans sets headings only. IBM
Plex Mono sets code and file paths only. Four sizes exist and nothing else:

| token           | px | used for                                                  |
| --------------- | -- | --------------------------------------------------------- |
| `--text-small`  | 13 | timestamps, hints under fields, meta lines, counts        |
| `--text-body`   | 16 | messages, labels, inputs, buttons, links, navigation      |
| `--text-title`  | 20 | panel titles (h2, h3), the room title in the chat header  |
| `--text-page`   | 28 | one h1 per page (Rooms, Settings)                         |

Weights: 400 for text, 600 for names, labels and buttons, 700 for headings. No other weight. Letter-spacing
only on headings, as `--display-tracking`.

## Space

The spacing scale is GOV.UK's: 5, 10, 15, 20, 30, 40px, as `--space-1` to `--space-6`. Padding, margin and
gap take a token or 0. Inside a component the gap between a label and its control is `--space-1`; between
fields `--space-4`; between sections `--space-6`. Page gutter is `--space-5`.

Adda is a desktop app. Every page fills the window and only the gutter is fixed; no page centres a
narrow column, and no text block caps its line length. Phone widths only have to stay usable.

## Controls

One control height, `--control-height: 40px`, for buttons, text inputs, selects, the search box and the
composer's single-line state. Controls use `--text-body`, horizontal padding `--space-3`, border-radius 0,
no shadow. Text inputs, selects and textareas have a 2px solid `--ink` border (GOV.UK). Focus is a 3px solid
`--ink` outline with 0 offset on every focusable element; no custom focus per component.

A control is as wide as its longest expected answer, never as wide as its column (GOV.UK):
`.input--10` for numbers, `.input--20` for names and short selects, `.input--30` for model names,
keys and long selects. Folder paths and the composer take the full width.

Three button kinds, and no size variants:

- **Primary**, `.btn--primary`: `--accent` fill, `--accent-ink` text. At most one per page: Send in a room,
  New room on the rooms page, Save on settings.
- **Secondary**, `.btn`: `--panel` fill, `--ink` text. Everything else.
- **Warning**, `.btn--warning`: `--stop` fill, `--stop-ink` text. Destructive and hard to undo only: End
  discussion, Delete seat. Each warning action asks for confirmation first (GOV.UK rule).

Labels are sentence case and start with a verb: "Open folder", "Add a seat", "Wake all". The one
icon-only control is the eye inside a secret field that shows or hides what is typed; it carries an
`aria-label` and `aria-pressed`. Links are `--ink`, underlined, and become links only when they navigate; an action is a button.

## Colour

Colour tokens stay as they are: `--ground`, `--panel`, `--card`, `--ink`, `--mute`, `--accent`,
`--good`, `--bad`, `--stop`, and the per-seat `--seat`. Rules on their use:

- `--good` and `--bad` colour a status word or a pill only ("Live", "not installed"). Instructions and
  body text are never green or red.
- Every seat in a room has a different colour. The server picks the first unused colour of the
  eight-hue palette (red, orange, gold, green, teal, blue, violet, magenta, all at one lightness).
- A speaker's name is a filled block in the raw seat colour with `--card` text, in messages and in
  the thread list; the owner's block is `--ink`. A block only needs 3:1, so it keeps the hue that
  coloured text would lose. Room cards show a 10px swatch before each seat name. The 3px left bar on
  a message stays. Message bodies sit on `--ground` with no tint (Slack and Discord show messages
  flush, not in bubbles).
- Surfaces step by tone: page `--ground`, columns `--panel`, cards and fields `--card`. No borders between
  surfaces, no gradients, no shadow except the floating menu.

## Layout of the room page

The shape is Slack's: a left column of threads 240px wide, the chat in the middle, and a room panel on the
right that is closed by default and opens from one "Room panel" button. The header holds the room title,
the Live pill, the search box and that one button. Nothing else.

The chat footer holds the seat strip, the textarea and one Primary button, Send. Stop is a Secondary button
that is present only while a seat is composing. End discussion lives in the room panel as a Warning button
with a confirm step. Copy thread as Markdown and Memory live in the room panel as Secondary buttons.

The room panel shows, in order: Seats (list, Add a seat), Mode (the three radios), Liveliness (slider),
Pause and Wake all as two Secondary buttons, then a `<details>` named Settings that holds Voices at once,
Access and Budget, and last the thread actions (Copy thread as Markdown, Memory, End discussion). Folder
path and Open folder go under Settings too; they leave the meta line under the title.

Messages: name block in the seat colour at weight 700, harness name as a small mono tag, time in
`--text-small` and `--mute` on the same line, body
below, `--space-4` between messages, no avatar column, full width of the chat column. The owner's
messages use the same shape.

## Layout of the rooms page

A grid of room cards, as many columns as fit at 440px minimum, each with the title, a status pill, the
seat names, and one meta line in
`--text-small` (threads, messages, spent of budget). Folder paths and Open folder are not on the card. The
"Harnesses on this machine" panel lists each program with a status pill; its instruction text is `--ink`.

## Layout of the settings page

A 240px list of the six sections on the left, sticky, with the current section marked by the accent
bar; the panels in one column on the right, each with an id the list links to. Save and the save status
sit in a bar stuck to the foot of the window, so they are on screen whichever section is being edited;
the status reads "Unsaved changes" after any edit and the page asks before it is left with edits
unsaved. Short fields sit side by side in a wrapping row, each as wide as its control.

## Gates

- `uv run pytest tests/test_style_tokens.py` passes: outside `:root` and `@media` preludes the stylesheet
  has no `rem` and no `px` value above 3 (1 to 3px are borders and outlines).
- A screenshot at 1280x800 and at 390x844 of each page shows no clipped control and no wrapped label.
- Every page has exactly one Primary button.
