"""Text sent to a seat: its system prompt, its turn prompt and the end-of-discussion prompt.

Also the mention parser, because mentions decide who is woken and the prompt tells seats how
to write them.
"""

from __future__ import annotations

import re

from adda.models import Message, Room, Seat
from adda.personas import Persona, memory_index

END_OF_DISCUSSION_PROMPT = (
    "In at most 3 bullets, what should you remember from this discussion for future ones? "
    "Reply NONE if nothing."
)
MAX_MEMORY_BULLETS = 3
BULLET_PREFIX = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")

# `@name`, `@all` or `@prefix*`, not preceded by a word character (so e-mail addresses do not
# count). \w is Unicode-aware, so handles in other scripts work.
MENTION_PATTERN = re.compile(r"(?<!\w)@([\w-]+)(\*)?")
ALL_HANDLE = "all"
MENTION_REASON = "mention"
ALL_REASON = "all"

# A reply whose last line is exactly this keeps the floor: the seat is started again at once.
MORE_MARKER = "[more]"


def render_transcript(messages: list[Message], owner_name: str = "owner") -> str:
    """One `author: text` line per message, as the router and the seats read it.

    Owner messages are stored with the author "owner"; the reader sees the owner's real name.
    """
    lines = []
    for message in messages:
        author = owner_name if message.role == "owner" else message.author
        lines.append(f"{author}: {message.text}")
    return "\n".join(lines)


def resolve_mentions(text: str, seats: list[Seat], author_seat_id: str | None) -> dict[str, str]:
    """Seat ids woken by `text`, in order, each with the reason "mention" or "all".

    Only enabled seats can be woken. `@all` means every enabled seat whose program is installed,
    in position order; `@prefix*` means every enabled seat whose handle starts with the prefix.
    The author is never woken by its own message. A seat named explicitly keeps the reason
    "mention" even when `@all` also covers it.
    """
    wakeable = sorted((seat for seat in seats if seat.enabled), key=lambda seat: seat.position)
    resolved: dict[str, str] = {}

    def add(seat: Seat, reason: str) -> None:
        if seat.id == author_seat_id:
            return
        if seat.id not in resolved or (
            reason == MENTION_REASON and resolved[seat.id] == ALL_REASON
        ):
            resolved[seat.id] = reason

    for match in MENTION_PATTERN.finditer(text):
        token = match.group(1).rstrip("-").lower()
        is_prefix = match.group(2) is not None
        if not token:
            continue
        if is_prefix:
            for seat in wakeable:
                if seat.handle.startswith(token):
                    add(seat, MENTION_REASON)
        elif token == ALL_HANDLE:
            for seat in wakeable:
                if seat.available:
                    add(seat, ALL_REASON)
        else:
            for seat in wakeable:
                if seat.handle == token:
                    add(seat, MENTION_REASON)
    return resolved


def split_more_marker(text: str) -> tuple[str, bool]:
    """Reply text without a trailing `[more]` line, and whether that line was there.

    Only a last line that is exactly the marker (case-insensitive, surrounding blanks ignored)
    counts, so a reply that merely mentions "[more]" in a sentence is left alone.
    """
    stripped = text.rstrip()
    last_line_start = stripped.rfind("\n") + 1
    if stripped[last_line_start:].strip().lower() != MORE_MARKER:
        return text, False
    return stripped[:last_line_start].rstrip(), True


def find_mentions(text: str, seats: list[Seat], author_seat_id: str | None = None) -> list[str]:
    """Seat ids woken by `text`, in order, without duplicates (see resolve_mentions)."""
    return list(resolve_mentions(text, seats, author_seat_id))


def system_prompt(
    seat: Seat,
    persona: Persona | None,
    other_seats: list[Seat],
    owner_name: str,
    owner_line: str,
) -> str:
    """Owner line, persona, custom instruction, memory index, then the room rules."""
    sections: list[str] = []
    if owner_line.strip():
        sections.append(f"About the owner, {owner_name}: {owner_line.strip()}")
    if persona is not None:
        sections.append(persona.body)
        if persona.role:
            sections.append(f"Your role: {persona.role}")
        if persona.stance:
            sections.append(f"Your stance: {persona.stance}")
    if seat.instruction and seat.instruction.strip():
        sections.append(seat.instruction.strip())
    if persona is not None:
        memory = memory_index(persona)
        if memory:
            sections.append(f"What you remember from earlier discussions:\n{memory}")
    sections.append(_room_rules(seat, other_seats, owner_name))
    return "\n\n".join(section for section in sections if section)


def _room_rules(seat: Seat, other_seats: list[Seat], owner_name: str) -> str:
    other_handles = ", ".join(f"@{other.handle} ({other.name})" for other in other_seats)
    return (
        f"You are the seat {seat.name} (@{seat.handle}) in Adda, a chat room where AI agents and "
        f"one human, the owner {owner_name}, discuss one issue in one shared thread.\n"
        f"Other seats: {other_handles or 'none'}.\n"
        "You only talk: no files, no shell, no tools. Address a seat with @handle, everyone with "
        "@all, or a group with @prefix*. "
        "Keep messages short and conversational. "
        "Other seats may be composing a reply at the same moment as you, so replies can cross: "
        "a message may appear that you have not seen when you started. "
        "If you have more to say and want to keep the floor, end your reply with a last line "
        f"that is exactly {MORE_MARKER} and you will be asked to continue at once; use it "
        "sparingly, it works only a couple of times in a row. "
        "If you have nothing worth adding when asked to speak, reply with exactly PASS."
    )


# Extra sentence in the turn prompt for the two ways a seat is started without a new message.
REASON_HINTS = {
    "afterthought": (
        "The room has been quiet for a while. Speak only if you have an afterthought that "
        "nobody has said and the room would want to hear."
    ),
    "more": f"You ended your last reply with {MORE_MARKER}; carry on from where you stopped.",
}


def turn_prompt(
    room: Room,
    unseen_messages: list[Message],
    seat: Seat,
    owner_name: str,
    woken_by: str | None = None,
    thread_title: str | None = None,
) -> str:
    transcript = render_transcript(unseen_messages, owner_name) or "(no new messages)"
    hint = REASON_HINTS.get(woken_by or "")
    hint_line = f"{hint}\n" if hint else ""
    # A room is a project; the thread title names the issue this discussion is about.
    thread_line = f"Thread: {thread_title}\n" if thread_title and thread_title != room.title else ""
    return (
        f"Adda room: {room.title}\n"
        f"{thread_line}"
        f"New messages since your last turn:\n\n{transcript}\n\n"
        f"{hint_line}"
        f"It is your turn to speak as @{seat.handle}. Reply with your message text only. "
        "If you have nothing to add, reply with exactly PASS."
    )


def parse_memory_bullets(reply_text: str) -> list[str]:
    """Turn the end-of-discussion reply into at most three note texts; NONE means none."""
    stripped = reply_text.strip()
    if not stripped or stripped.upper().rstrip(".") == "NONE":
        return []
    bullets: list[str] = []
    for line in stripped.splitlines():
        if BULLET_PREFIX.match(line):
            bullets.append(BULLET_PREFIX.sub("", line).strip())
        elif bullets and line.strip():
            # A wrapped continuation line belongs to the previous bullet.
            bullets[-1] = f"{bullets[-1]} {line.strip()}"
    if not bullets:
        bullets = [stripped]
    return [bullet for bullet in bullets if bullet][:MAX_MEMORY_BULLETS]
