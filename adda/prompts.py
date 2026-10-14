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
        "If you have nothing worth adding when asked to speak, reply with exactly PASS."
    )


def turn_prompt(room: Room, unseen_messages: list[Message], seat: Seat, owner_name: str) -> str:
    transcript = render_transcript(unseen_messages, owner_name) or "(no new messages)"
    return (
        f"Adda room: {room.title}\n"
        f"New messages since your last turn:\n\n{transcript}\n\n"
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
