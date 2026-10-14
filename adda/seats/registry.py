"""Build the adapter for one seat from its room configuration (SPEC section 4)."""

from __future__ import annotations

from pathlib import Path

from adda.config import HarnessSettings
from adda.harnesses import thinking_arguments
from adda.models import SeatConfig
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


def build_adapter(
    config: SeatConfig, harness_settings: HarnessSettings | None, working_directory: Path
) -> SeatAdapter:
    """Adapter for `config`. A seat that leaves model or thinking unset gets the harness default.

    "default" thinking on the seat means "use the harness default" (which itself may be
    "default", meaning no flag at all).
    """
    adapter_class = ADAPTER_CLASSES.get(config.program)
    if adapter_class is None:
        raise ValueError(
            f"seat {config.name!r} has unknown program {config.program!r}; "
            f"expected one of {sorted(ADAPTER_CLASSES)}"
        )
    model = config.model or (harness_settings.model if harness_settings else "") or None
    thinking = config.thinking
    if thinking == "default" and harness_settings is not None:
        thinking = harness_settings.thinking
    return adapter_class(
        name=config.name,
        model=model,
        working_directory=working_directory,
        thinking_arguments=thinking_arguments(config.program, thinking),
    )
