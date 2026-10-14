"""Map a persona's `program` to its adapter class."""

from __future__ import annotations

from pathlib import Path

from adda.personas import Persona
from adda.seats.base import CommandSeat, SeatAdapter
from adda.seats.claude import ClaudeSeat
from adda.seats.codex import CodexSeat
from adda.seats.opencode import OpenCodeSeat
from adda.seats.pi import PiSeat

ADAPTER_CLASSES: dict[str, type[CommandSeat]] = {
    "claude": ClaudeSeat,
    "opencode": OpenCodeSeat,
    "pi": PiSeat,
    "codex": CodexSeat,
}


def build_adapter(persona: Persona, working_directory: Path) -> SeatAdapter:
    adapter_class = ADAPTER_CLASSES.get(persona.program)
    if adapter_class is None:
        raise ValueError(
            f"persona {persona.name!r} has unknown program {persona.program!r}; "
            f"expected one of {sorted(ADAPTER_CLASSES)}"
        )
    return adapter_class(
        name=persona.name, model=persona.model, working_directory=working_directory
    )


def build_adapters(personas: dict[str, Persona], working_directory: Path) -> dict[str, SeatAdapter]:
    return {name: build_adapter(persona, working_directory) for name, persona in personas.items()}
