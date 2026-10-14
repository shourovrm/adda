"""Settings loaded from environment variables (SPEC section 7).

Every variable is optional. ANTHROPIC_API_KEY is deliberately never read here:
headless `claude -p` prefers it over the owner's Max login and would bill the API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

JEV_DEFAULT_MODELS = {
    "openrouter": "typesafe/jev-1.13",
    "typesafe": "jev-latest",
}

JEV_KEY_VARIABLES = {
    "openrouter": "OPENROUTER_API_KEY",
    "typesafe": "TYPESAFE_API_KEY",
}


class ConfigError(ValueError):
    """An environment variable holds a value Adda cannot use."""


def _read_float(environment: dict[str, str], name: str, default: float) -> float:
    raw_value = environment.get(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        return float(raw_value)
    except ValueError as error:
        raise ConfigError(f"{name} must be a number, got {raw_value!r}") from error


def _read_int(environment: dict[str, str], name: str, default: int) -> int:
    raw_value = environment.get(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        return int(raw_value)
    except ValueError as error:
        raise ConfigError(f"{name} must be an integer, got {raw_value!r}") from error


def _read_choice(environment: dict[str, str], name: str, default: str, choices: set[str]) -> str:
    value = environment.get(name) or default
    if value not in choices:
        raise ConfigError(f"{name} must be one of {sorted(choices)}, got {value!r}")
    return value


def read_key_file(path: Path) -> dict[str, str]:
    """Parse `KEY=value` lines. Comments, blank lines and an `export ` prefix are allowed.

    The values are secrets: callers must never log or print them.
    """
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :]
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: Path = Path("data")
    personas_dir: Path = Path("personas")
    router: str = "jev"
    jev_provider: str = "openrouter"
    jev_model: str = JEV_DEFAULT_MODELS["openrouter"]
    jev_api_key: str | None = field(default=None, repr=False)
    bid_threshold: float = 5.0
    cooldown_s: float = 20.0
    max_auto_turns: int = 6
    seat_timeout_s: float = 180.0
    router_window: int = 20

    @property
    def database_path(self) -> Path:
        return self.data_dir / "adda.db"

    @property
    def seat_workdir(self) -> Path:
        """Fixed working directory for seat subprocesses.

        claude and pi store sessions per working directory, so resume only works when every
        turn runs in the same directory. A directory outside the repo also keeps the repo's
        own CLAUDE.md out of the seats' context.
        """
        return self.data_dir / "seat-workdir"


def load_settings(environment: dict[str, str] | None = None) -> Settings:
    if environment is None:
        environment = dict(os.environ)

    jev_provider = _read_choice(
        environment, "ADDA_JEV_PROVIDER", "openrouter", set(JEV_DEFAULT_MODELS)
    )
    key_variable = JEV_KEY_VARIABLES[jev_provider]
    jev_api_key = environment.get(key_variable) or None
    if jev_api_key is None:
        key_file = Path(
            environment.get("ADDA_JEV_KEY_FILE") or "~/.config/jev/openrouter.env"
        ).expanduser()
        jev_api_key = read_key_file(key_file).get(key_variable) or None

    return Settings(
        host=environment.get("ADDA_HOST") or "127.0.0.1",
        port=_read_int(environment, "ADDA_PORT", 8765),
        data_dir=Path(environment.get("ADDA_DATA_DIR") or "data"),
        personas_dir=Path(environment.get("ADDA_PERSONAS_DIR") or "personas"),
        router=_read_choice(environment, "ADDA_ROUTER", "jev", {"jev", "heuristic"}),
        jev_provider=jev_provider,
        jev_model=environment.get("ADDA_JEV_MODEL") or JEV_DEFAULT_MODELS[jev_provider],
        jev_api_key=jev_api_key,
        bid_threshold=_read_float(environment, "ADDA_BID_THRESHOLD", 5.0),
        cooldown_s=_read_float(environment, "ADDA_COOLDOWN_S", 20.0),
        max_auto_turns=_read_int(environment, "ADDA_MAX_AUTO_TURNS", 6),
        seat_timeout_s=_read_float(environment, "ADDA_SEAT_TIMEOUT_S", 180.0),
        router_window=_read_int(environment, "ADDA_ROUTER_WINDOW", 20),
    )
