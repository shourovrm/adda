"""Text sent to a seat: its system prompt, its turn prompt and the end-of-discussion prompt."""

from __future__ import annotations

import re

from adda.models import Message, Thread
from adda.personas import Persona, memory_index

END_OF_DISCUSSION_PROMPT = (
    "In at most 3 bullets, what should you remember from this discussion for future ones? "
    "Reply NONE if nothing."
)
MAX_MEMORY_BULLETS = 3
BULLET_PREFIX = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def render_transcript(messages: list[Message]) -> str:
    """One `author: text` line per message, as the router and the seats read it."""
    return "\n".join(f"{message.author}: {message.text}" for message in messages)


def system_prompt(persona: Persona, seat_names: list[str]) -> str:
    other_seats = [name for name in seat_names if name != persona.name]
    room_rules = (
        f"You are the seat @{persona.name} in Adda, a chat room where AI agents and one human, "
        "the owner, discuss one issue in one shared thread.\n"
        f"Other seats: {', '.join('@' + name for name in other_seats) or 'none'}.\n"
        "You only talk: no files, no shell, no tools. Address a seat with @name. "
        "Keep messages short and conversational. "
        "If you have nothing worth adding when asked to speak, reply with exactly PASS."
    )
    sections = [persona.body, room_rules]
    if persona.role:
        sections.append(f"Your role: {persona.role}")
    if persona.stance:
        sections.append(f"Your stance: {persona.stance}")
    memory = memory_index(persona)
    if memory:
        sections.append(f"What you remember from earlier discussions:\n{memory}")
    return "\n\n".join(section for section in sections if section)


def turn_prompt(thread: Thread, unseen_messages: list[Message], seat_name: str) -> str:
    transcript = render_transcript(unseen_messages) or "(no new messages)"
    return (
        f"Adda thread: {thread.title}\n"
        f"New messages since your last turn:\n\n{transcript}\n\n"
        f"It is your turn to speak as @{seat_name}. Reply with your message text only. "
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


MENTION_PATTERN = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)")


def find_mentions(text: str, seat_names: list[str]) -> list[str]:
    """Seat names mentioned as @name, in order of first appearance, without duplicates."""
    mentioned: list[str] = []
    for match in MENTION_PATTERN.finditer(text):
        name = match.group(1)
        if name in seat_names and name not in mentioned:
            mentioned.append(name)
    return mentioned
