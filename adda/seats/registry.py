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
    config: SeatConfig,
    harness_settings: HarnessSettings | None,
    working_directory: Path,
    data_dir: Path | None = None,
) -> SeatAdapter:
    """Adapter for `config`. A seat that leaves model or thinking unset gets the harness default.

    "default" thinking on the seat means "use the harness default" (which itself may be
    "default", meaning no flag at all).

    The seat's access is its effective level when `config` is a Seat (room level applied), else
    its own level, else talk. `working_directory` is the room folder the tools may touch.
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
    access = getattr(config, "effective_access", None) or config.access or "talk"
    extra_arguments = {}
    if config.program == "claude" and harness_settings is not None:
        extra_arguments = {
            "base_url": harness_settings.base_url,
            "auth_token": harness_settings.auth_token,
        }
    return adapter_class(
        name=config.name,
        model=model,
        working_directory=working_directory,
        thinking_arguments=thinking_arguments(config.program, thinking),
        access=access,
        data_dir=data_dir,
        **extra_arguments,
    )
