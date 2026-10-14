"""Settings (SPEC section 7): built-in defaults < config.toml < environment variables.

The TOML file lives at `~/.config/adda/config.toml` (or the path in `ADDA_CONFIG`). The same
nested shape is used for the file, for `settings_view()` (what `GET /api/settings` returns) and
for `save_settings()` (what `PUT /api/settings` sends back), so the three never drift apart.

View shape (key values never appear, only booleans):

    {"owner": {"name", "line"},
     "folders": {"rooms_dir", "data_dir", "personas_dir"},
     "router": {"kind", "provider", "model", "threshold_floor", "window"},
     "room_defaults": {"liveliness", "budget_usd", "mode", "cooldown_s",
                       "seat_timeout_s", "max_auto_turns", "max_concurrent",
                       "self_reply_penalty", "more_limit", "afterthought_limit",
                       "afterthought_scale"},
     "harness": {"claude": {"model", "thinking", "enabled", "extra_models"}, ...},
     "keys": {"openrouter": bool, "typesafe": bool, "deepseek": bool},
     "env_overrides": ["router.kind", ...]}   # dotted paths of fields an env var is setting

Keys live in `~/.config/adda/keys.env` (mode 600). ANTHROPIC_API_KEY is deliberately never
read: headless `claude -p` prefers it over the owner's Max login and would bill the API.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

JEV_DEFAULT_MODELS = {
    "openrouter": "typesafe/jev-1.13",
    "typesafe": "jev-latest",
}

JEV_KEY_VARIABLES = {
    "openrouter": "OPENROUTER_API_KEY",
    "typesafe": "TYPESAFE_API_KEY",
}

# Variable names that keys.env may hold, keyed by the short name the settings view uses.
KEY_VARIABLES = {
    "openrouter": "OPENROUTER_API_KEY",
    "typesafe": "TYPESAFE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}

PROGRAMS = ("claude", "opencode", "pi", "codex")
MODES = ("bidding", "roundrobin", "manual")
THINKING_LEVELS = ("default", "low", "medium", "high")
ROUTER_KINDS = ("jev", "heuristic")

DEFAULT_CONFIG_PATH = Path("~/.config/adda/config.toml")
DEFAULT_KEYS_PATH = Path("~/.config/adda/keys.env")
LEGACY_OPENROUTER_KEY_PATH = Path("~/.config/jev/openrouter.env")
REPOSITORY_PERSONAS_DIR = Path(__file__).resolve().parent.parent / "personas"

DEFAULT_VALUES: dict[str, Any] = {
    "owner": {"name": "Owner", "line": ""},
    "folders": {
        "rooms_dir": "~/Adda-Rooms",
        "data_dir": "~/.adda",
        "personas_dir": str(REPOSITORY_PERSONAS_DIR),
    },
    "router": {
        "kind": "jev",
        "provider": "openrouter",
        "model": "",
        "threshold_floor": 2.5,
        "window": 20,
    },
    "room_defaults": {
        "liveliness": 6,
        "budget_usd": 1.0,
        "mode": "bidding",
        "cooldown_s": 0.0,
        "seat_timeout_s": 180.0,
        "max_auto_turns": 40,
        "max_concurrent": 2,
        "self_reply_penalty": 1.5,
        "more_limit": 2,
        "afterthought_limit": 3,
        "afterthought_scale": 1.0,
    },
    "harness": {
        "claude": {"model": "sonnet", "thinking": "medium", "enabled": True, "extra_models": []},
        "opencode": {
            "model": "deepseek/deepseek-flash",
            "thinking": "default",
            "enabled": True,
            "extra_models": [],
        },
        "pi": {"model": "", "thinking": "default", "enabled": True, "extra_models": []},
        "codex": {
            "model": "gpt-5-codex",
            "thinking": "default",
            "enabled": False,
            "extra_models": [],
        },
    },
}

# Environment variable -> (section, field, type). Names match v0.1 plus the v0.2 additions.
ENVIRONMENT_FIELDS: dict[str, tuple[str, str, str]] = {
    "ADDA_OWNER_NAME": ("owner", "name", "str"),
    "ADDA_OWNER_LINE": ("owner", "line", "str"),
    "ADDA_ROOMS_DIR": ("folders", "rooms_dir", "str"),
    "ADDA_DATA_DIR": ("folders", "data_dir", "str"),
    "ADDA_PERSONAS_DIR": ("folders", "personas_dir", "str"),
    "ADDA_ROUTER": ("router", "kind", "str"),
    "ADDA_JEV_PROVIDER": ("router", "provider", "str"),
    "ADDA_JEV_MODEL": ("router", "model", "str"),
    "ADDA_THRESHOLD_FLOOR": ("router", "threshold_floor", "float"),
    "ADDA_ROUTER_WINDOW": ("router", "window", "int"),
    "ADDA_LIVELINESS": ("room_defaults", "liveliness", "int"),
    "ADDA_BUDGET_USD": ("room_defaults", "budget_usd", "float"),
    "ADDA_MODE": ("room_defaults", "mode", "str"),
    "ADDA_COOLDOWN_S": ("room_defaults", "cooldown_s", "float"),
    "ADDA_SEAT_TIMEOUT_S": ("room_defaults", "seat_timeout_s", "float"),
    "ADDA_MAX_AUTO_TURNS": ("room_defaults", "max_auto_turns", "int"),
    "ADDA_MAX_CONCURRENT": ("room_defaults", "max_concurrent", "int"),
}


class ConfigError(ValueError):
    """A setting (in the file, the environment or an API payload) that Adda cannot use."""


@dataclass(frozen=True)
class HarnessSettings:
    """Per-harness defaults used when a seat leaves model or thinking unset."""

    model: str = ""
    thinking: str = "default"
    enabled: bool = True
    extra_models: tuple[str, ...] = ()


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    owner_name: str = "Owner"
    owner_line: str = ""
    rooms_dir: Path = Path("~/Adda-Rooms").expanduser()
    data_dir: Path = Path("~/.adda").expanduser()
    personas_dir: Path = REPOSITORY_PERSONAS_DIR
    router_kind: str = "jev"
    router_provider: str = "openrouter"
    router_model: str = ""  # empty means the provider default; see `jev_model`
    router_threshold_floor: float = 2.5
    router_window: int = 20
    liveliness: int = 6
    budget_usd: float = 1.0
    mode: str = "bidding"
    cooldown_s: float = 0.0
    seat_timeout_s: float = 180.0
    max_auto_turns: int = 40
    max_concurrent: int = 2
    self_reply_penalty: float = 1.5
    more_limit: int = 2
    afterthought_limit: int = 3
    afterthought_scale: float = 1.0
    harnesses: dict[str, HarnessSettings] = field(default_factory=dict)
    jev_api_key: str | None = field(default=None, repr=False)
    # Which keys exist (never their values), for the settings view.
    keys_set: dict[str, bool] = field(default_factory=dict)
    # Dotted paths such as "router.kind" of fields currently set by an environment variable.
    env_overrides: tuple[str, ...] = ()

    @property
    def router(self) -> str:
        """v0.1 name for `router_kind`."""
        return self.router_kind

    @property
    def jev_provider(self) -> str:
        """v0.1 name for `router_provider`."""
        return self.router_provider

    @property
    def jev_model(self) -> str:
        """Router model with the provider default filled in."""
        return self.router_model or JEV_DEFAULT_MODELS[self.router_provider]

    @property
    def database_path(self) -> Path:
        return self.data_dir / "adda.db"

    @property
    def seat_workdir(self) -> Path:
        """v0.1 fixed seat directory. In v0.2 each room has its own folder instead."""
        return self.data_dir / "seat-workdir"


# Reading values ---------------------------------------------------------------------------


def _convert(raw_value: Any, type_name: str, label: str) -> Any:
    """Coerce a TOML value or an environment string to `type_name`, failing loudly."""
    try:
        if type_name == "str":
            if not isinstance(raw_value, str):
                raise TypeError("a string")
            return raw_value
        if type_name == "int":
            if isinstance(raw_value, bool):
                raise TypeError("an integer")
            return int(raw_value)
        if type_name == "float":
            if isinstance(raw_value, bool):
                raise TypeError("a number")
            return float(raw_value)
        if type_name == "bool":
            if isinstance(raw_value, bool):
                return raw_value
            raise TypeError("true or false")
    except (TypeError, ValueError) as error:
        raise ConfigError(f"{label} must be a valid {type_name}, got {raw_value!r}") from error
    raise ConfigError(f"internal: unknown type {type_name}")


def _validate_choice(value: str, choices: tuple[str, ...], label: str) -> str:
    if value not in choices:
        raise ConfigError(f"{label} must be one of {list(choices)}, got {value!r}")
    return value


def _validate_values(values: dict[str, Any]) -> None:
    """Range and choice checks on a complete nested values dict."""
    _validate_choice(values["router"]["kind"], ROUTER_KINDS, "router.kind")
    _validate_choice(values["router"]["provider"], tuple(JEV_DEFAULT_MODELS), "router.provider")
    _validate_choice(values["room_defaults"]["mode"], MODES, "room_defaults.mode")
    liveliness = values["room_defaults"]["liveliness"]
    if not 0 <= liveliness <= 10:
        raise ConfigError(f"room_defaults.liveliness must be 0..10, got {liveliness}")
    if values["room_defaults"]["budget_usd"] < 0:
        raise ConfigError("room_defaults.budget_usd must not be negative")
    max_concurrent = values["room_defaults"]["max_concurrent"]
    if not 1 <= max_concurrent <= 4:
        raise ConfigError(f"room_defaults.max_concurrent must be 1..4, got {max_concurrent}")
    for field_name in ("self_reply_penalty", "more_limit", "afterthought_limit"):
        if values["room_defaults"][field_name] < 0:
            raise ConfigError(f"room_defaults.{field_name} must not be negative")
    if values["room_defaults"]["afterthought_scale"] <= 0:
        raise ConfigError("room_defaults.afterthought_scale must be above 0")
    for program, harness_values in values["harness"].items():
        _validate_choice(harness_values["thinking"], THINKING_LEVELS, f"harness.{program}.thinking")


def _overlay_table(values: dict[str, Any], table: dict[str, Any], source: str) -> None:
    """Copy known fields of a (possibly partial) nested table over `values`, converting types.

    Unknown sections and fields are ignored so an older or newer file still loads.
    """
    for section_name, defaults in DEFAULT_VALUES.items():
        section = table.get(section_name)
        if section is None:
            continue
        if not isinstance(section, dict):
            raise ConfigError(f"{source}: [{section_name}] must be a table")
        if section_name == "harness":
            _overlay_harness_tables(values, section, source)
            continue
        for field_name, default in defaults.items():
            if field_name in section:
                values[section_name][field_name] = _convert(
                    section[field_name],
                    type(default).__name__,
                    f"{source}: {section_name}.{field_name}",
                )


def _overlay_harness_tables(values: dict[str, Any], section: dict[str, Any], source: str) -> None:
    for program, defaults in DEFAULT_VALUES["harness"].items():
        harness_table = section.get(program)
        if harness_table is None:
            continue
        if not isinstance(harness_table, dict):
            raise ConfigError(f"{source}: [harness.{program}] must be a table")
        target = values["harness"][program]
        for field_name, default in defaults.items():
            if field_name not in harness_table:
                continue
            label = f"{source}: harness.{program}.{field_name}"
            raw_value = harness_table[field_name]
            if field_name == "extra_models":
                if not isinstance(raw_value, list) or not all(
                    isinstance(item, str) for item in raw_value
                ):
                    raise ConfigError(f"{label} must be a list of strings")
                target[field_name] = list(raw_value)
            else:
                target[field_name] = _convert(raw_value, type(default).__name__, label)


def _read_toml(config_path: Path) -> dict[str, Any]:
    if not config_path.is_file():
        return {}
    try:
        return tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{config_path}: invalid TOML: {error}") from error


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


# Keys -------------------------------------------------------------------------------------


def _keys_path(environment: dict[str, str], keys_path: Path | None) -> Path:
    if keys_path is not None:
        return keys_path
    return Path(environment.get("ADDA_KEYS_FILE") or DEFAULT_KEYS_PATH).expanduser()


def _legacy_key_path(environment: dict[str, str], legacy_path: Path | None) -> Path:
    if legacy_path is not None:
        return legacy_path
    return Path(environment.get("ADDA_JEV_KEY_FILE") or LEGACY_OPENROUTER_KEY_PATH).expanduser()


def load_keys(
    environment: dict[str, str] | None = None,
    keys_path: Path | None = None,
    legacy_path: Path | None = None,
) -> dict[str, str]:
    """Return `{VARIABLE_NAME: value}` for every known key that is set anywhere.

    Lookup order per variable: the environment, then keys.env, then (OPENROUTER_API_KEY only)
    the legacy `~/.config/jev/openrouter.env`. Paths can be overridden for tests or through
    `ADDA_KEYS_FILE` and `ADDA_JEV_KEY_FILE`. Values are secrets: never log them.
    """
    if environment is None:
        environment = dict(os.environ)
    keys_file_values = read_key_file(_keys_path(environment, keys_path))
    legacy_values = read_key_file(_legacy_key_path(environment, legacy_path))
    found: dict[str, str] = {}
    for variable in KEY_VARIABLES.values():
        value = environment.get(variable) or keys_file_values.get(variable)
        if not value and variable == "OPENROUTER_API_KEY":
            value = legacy_values.get(variable)
        if value:
            found[variable] = value
    return found


def save_key(name: str, value: str, keys_path: Path | None = None) -> None:
    """Set one key in keys.env, keeping other lines, with file mode 600.

    `name` is either a variable name (OPENROUTER_API_KEY) or the short name (openrouter).
    """
    variable = KEY_VARIABLES.get(name, name)
    if variable not in KEY_VARIABLES.values():
        raise ConfigError(f"unknown key {name!r}; expected one of {sorted(KEY_VARIABLES)}")
    cleaned_value = value.strip()
    if not cleaned_value or "\n" in cleaned_value or "\r" in cleaned_value:
        raise ConfigError(f"value for {variable} must be one non-empty line")

    path = _keys_path({}, keys_path)
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    kept_lines = [
        line
        for line in existing_lines
        if line.strip().removeprefix("export ").partition("=")[0].strip() != variable
    ]
    kept_lines.append(f"{variable}={cleaned_value}")
    _write_private_file(path, "\n".join(kept_lines) + "\n")


def _write_private_file(path: Path, content: str) -> None:
    """Write via a temp file created with mode 600, then rename, so the secret is never
    readable by others even briefly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=".keys-")
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


# Loading ----------------------------------------------------------------------------------


def _config_path(environment: dict[str, str], config_path: Path | None) -> Path:
    if config_path is not None:
        return config_path
    return Path(environment.get("ADDA_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()


def _apply_environment(values: dict[str, Any], environment: dict[str, str]) -> list[str]:
    """Overlay environment variables; return the dotted paths they set."""
    overridden: list[str] = []
    for variable, (section, field_name, type_name) in ENVIRONMENT_FIELDS.items():
        raw_value = environment.get(variable)
        if raw_value is None or raw_value.strip() == "":
            continue
        if type_name == "str":
            converted: Any = raw_value
        else:
            converted = _convert_environment_number(raw_value, type_name, variable)
        values[section][field_name] = converted
        overridden.append(f"{section}.{field_name}")
    return overridden


def _convert_environment_number(raw_value: str, type_name: str, variable: str) -> Any:
    try:
        return int(raw_value) if type_name == "int" else float(raw_value)
    except ValueError as error:
        kind = "an integer" if type_name == "int" else "a number"
        raise ConfigError(f"{variable} must be {kind}, got {raw_value!r}") from error


def _settings_from_values(
    values: dict[str, Any],
    host: str,
    port: int,
    jev_api_key: str | None,
    keys_set: dict[str, bool],
    env_overrides: list[str],
) -> Settings:
    harnesses = {
        program: HarnessSettings(
            model=table["model"],
            thinking=table["thinking"],
            enabled=table["enabled"],
            extra_models=tuple(table["extra_models"]),
        )
        for program, table in values["harness"].items()
    }
    return Settings(
        host=host,
        port=port,
        owner_name=values["owner"]["name"],
        owner_line=values["owner"]["line"],
        rooms_dir=Path(values["folders"]["rooms_dir"]).expanduser(),
        data_dir=Path(values["folders"]["data_dir"]).expanduser(),
        personas_dir=Path(values["folders"]["personas_dir"]).expanduser(),
        router_kind=values["router"]["kind"],
        router_provider=values["router"]["provider"],
        router_model=values["router"]["model"],
        router_threshold_floor=values["router"]["threshold_floor"],
        router_window=values["router"]["window"],
        liveliness=values["room_defaults"]["liveliness"],
        budget_usd=values["room_defaults"]["budget_usd"],
        mode=values["room_defaults"]["mode"],
        cooldown_s=values["room_defaults"]["cooldown_s"],
        seat_timeout_s=values["room_defaults"]["seat_timeout_s"],
        max_auto_turns=values["room_defaults"]["max_auto_turns"],
        max_concurrent=values["room_defaults"]["max_concurrent"],
        self_reply_penalty=values["room_defaults"]["self_reply_penalty"],
        more_limit=values["room_defaults"]["more_limit"],
        afterthought_limit=values["room_defaults"]["afterthought_limit"],
        afterthought_scale=values["room_defaults"]["afterthought_scale"],
        harnesses=harnesses,
        jev_api_key=jev_api_key,
        keys_set=keys_set,
        env_overrides=tuple(env_overrides),
    )


def load_settings(
    environment: dict[str, str] | None = None, config_path: Path | None = None
) -> Settings:
    """Build Settings: defaults, then the TOML file, then environment variables."""
    if environment is None:
        environment = dict(os.environ)
    resolved_config_path = _config_path(environment, config_path)

    values = copy.deepcopy(DEFAULT_VALUES)
    _overlay_table(values, _read_toml(resolved_config_path), str(resolved_config_path))
    env_overrides = _apply_environment(values, environment)
    _validate_values(values)

    keys = load_keys(environment)
    key_variable = JEV_KEY_VARIABLES[values["router"]["provider"]]
    keys_set = {short_name: variable in keys for short_name, variable in KEY_VARIABLES.items()}
    return _settings_from_values(
        values,
        host=environment.get("ADDA_HOST") or "127.0.0.1",
        port=_read_port(environment),
        jev_api_key=keys.get(key_variable),
        keys_set=keys_set,
        env_overrides=env_overrides,
    )


def _read_port(environment: dict[str, str]) -> int:
    raw_value = environment.get("ADDA_PORT")
    if raw_value is None or raw_value.strip() == "":
        return 8765
    try:
        return int(raw_value)
    except ValueError as error:
        raise ConfigError(f"ADDA_PORT must be an integer, got {raw_value!r}") from error


# View and saving --------------------------------------------------------------------------


def settings_view(settings: Settings) -> dict[str, Any]:
    """The JSON shape of `GET /api/settings`. Keys appear only as booleans."""
    return {
        "owner": {"name": settings.owner_name, "line": settings.owner_line},
        "folders": {
            "rooms_dir": str(settings.rooms_dir),
            "data_dir": str(settings.data_dir),
            "personas_dir": str(settings.personas_dir),
        },
        "router": {
            "kind": settings.router_kind,
            "provider": settings.router_provider,
            "model": settings.router_model,
            "threshold_floor": settings.router_threshold_floor,
            "window": settings.router_window,
        },
        "room_defaults": {
            "liveliness": settings.liveliness,
            "budget_usd": settings.budget_usd,
            "mode": settings.mode,
            "cooldown_s": settings.cooldown_s,
            "seat_timeout_s": settings.seat_timeout_s,
            "max_auto_turns": settings.max_auto_turns,
            "max_concurrent": settings.max_concurrent,
            "self_reply_penalty": settings.self_reply_penalty,
            "more_limit": settings.more_limit,
            "afterthought_limit": settings.afterthought_limit,
            "afterthought_scale": settings.afterthought_scale,
        },
        "harness": {
            program: {
                "model": harness.model,
                "thinking": harness.thinking,
                "enabled": harness.enabled,
                "extra_models": list(harness.extra_models),
            }
            for program, harness in settings.harnesses.items()
        },
        "keys": dict(settings.keys_set),
        "env_overrides": list(settings.env_overrides),
    }


def save_settings(view: dict[str, Any], config_path: Path | None = None) -> Path:
    """Write the view's fields to config.toml and return the path.

    Fields listed in `view["env_overrides"]` are skipped and keep whatever the file already
    says, because the view shows their environment value and writing that into the file
    would make a temporary override permanent. Unknown keys (`keys`, `env_overrides`) are
    ignored; key values go through `save_key`. Invalid values raise ConfigError and nothing
    is written.
    """
    resolved_path = _config_path({}, config_path)
    overridden = set(view.get("env_overrides") or [])

    file_values = copy.deepcopy(DEFAULT_VALUES)
    _overlay_table(file_values, _read_toml(resolved_path), str(resolved_path))

    submitted_values = copy.deepcopy(file_values)
    _overlay_table(submitted_values, view, "settings payload")
    for dotted_path in overridden:
        section, _, field_name = dotted_path.partition(".")
        submitted_values[section][field_name] = file_values[section][field_name]

    _validate_values(submitted_values)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.write_text(_render_toml(submitted_values), encoding="utf-8")
    return resolved_path


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        # A JSON string literal is also a valid TOML basic string.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ConfigError(f"cannot write {value!r} to TOML")


def _render_toml(values: dict[str, Any]) -> str:
    lines: list[str] = []
    for section_name, section in values.items():
        if section_name == "harness":
            continue
        lines.append(f"[{section_name}]")
        lines.extend(f"{name} = {_toml_value(item)}" for name, item in section.items())
        lines.append("")
    for program, table in values["harness"].items():
        lines.append(f"[harness.{program}]")
        lines.extend(f"{name} = {_toml_value(item)}" for name, item in table.items())
        lines.append("")
    return "\n".join(lines)
