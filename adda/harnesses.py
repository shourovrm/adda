"""Detect the agent programs (harnesses) installed on this machine (SPEC section 4).

`detect_harnesses(settings)` never raises for a missing or broken binary: a program that is
absent comes back with `found=False`; a version or model-list call that fails or times out
yields `None` or an empty list.

Thinking flags, verified on this machine on 2026-10-14 by reading `--help` (no prompt was run):

    claude    2.1.299   `--effort <level>`  accepts low, medium, high, xhigh, max
    pi        1.1.0     `--thinking <level>` accepts off, minimal, low, medium, high, xhigh, max
    opencode  1.17.7    `run --variant <level>`  provider-specific string ("high", "max",
                        "minimal"); it is passed through unchanged. Do not confuse it with
                        `run --thinking`, which only shows thinking blocks.
    codex     0.163.0   `-c model_reasoning_effort=<level>`. `codex exec --help` documents `-c`
                        as "Override a configuration value that would otherwise be loaded from
                        `~/.codex/config.toml`"; the key name itself is present in the binary's
                        config schema (checked 2026-10-19), but no call was run with it.

Our four levels (default, low, medium, high) map as follows: "default" sends nothing; low,
medium and high are sent as is to all four programs, because each accepts those three words.
`thinking_arguments()` builds the argument list so adapters do not repeat this table.

Version output: claude prints "2.1.299 (Claude Code)", opencode "1.17.7", pi "1.1.0", codex
"codex-cli 0.163.0".
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
import tomllib
from collections.abc import Callable
from pathlib import Path

from adda.config import Settings
from adda.models import Harness

VERSION_TIMEOUT_S = 5.0
MODEL_LIST_TIMEOUT_S = 15.0
MODEL_CACHE_SECONDS = 600.0

PROGRAM_ORDER = ("claude", "opencode", "pi", "codex")

BUILT_IN_MODEL_SUGGESTIONS = {
    "claude": [
        "fable",
        "opus",
        "sonnet",
        "haiku",
        "claude-fable-5-1",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
    ],
    "codex": ["gpt-5-codex"],
}
PI_FALLBACK_MODELS = ["z-ai/glm-5.3-flash"]
PI_SETTINGS_PATH = Path("~/.pi/agent/settings.json").expanduser()
CODEX_HOME = Path("~/.codex").expanduser()

logger = logging.getLogger(__name__)

LOGIN_HINTS = {
    "claude": "Run `claude` once and sign in with your Max account (/login).",
    "opencode": "Run `opencode auth login` and pick a provider.",
    "pi": "Run `pi`, use /login, or set the provider's API key in your shell.",
    "codex": "Run `codex login`.",
}

# Callable that runs argv without a shell and returns stdout, or None on any failure
# (missing binary, non-zero exit, timeout). Injected by tests.
Runner = Callable[[list[str], float], str | None]
Finder = Callable[[str], str | None]

_opencode_models_cache: tuple[float, list[str]] | None = None


def clear_model_cache() -> None:
    """Forget the cached `opencode models` list (used by tests and the re-scan button)."""
    global _opencode_models_cache
    _opencode_models_cache = None


def run_command(argv: list[str], timeout_s: float) -> str | None:
    """Default runner: no shell, captured output, never raises."""
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False, shell=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def thinking_arguments(program: str, thinking: str) -> list[str]:
    """Command-line arguments that select a thinking level; empty for "default"."""
    if thinking == "default":
        return []
    if thinking not in ("low", "medium", "high"):
        raise ValueError(f"thinking must be default, low, medium or high, got {thinking!r}")
    if program == "claude":
        return ["--effort", thinking]
    if program == "pi":
        return ["--thinking", thinking]
    if program == "opencode":
        return ["--variant", thinking]
    if program == "codex":
        return ["-c", f"model_reasoning_effort={thinking}"]
    raise ValueError(f"unknown program {program!r}")


def parse_version(output: str | None) -> str | None:
    """First dotted number in the output ("2.1.299 (Claude Code)" -> "2.1.299")."""
    if not output:
        return None
    match = re.search(r"\d+(?:\.\d+)+\S*", output)
    if match:
        return match.group(0)
    first_line = output.strip().splitlines()[0].strip() if output.strip() else ""
    return first_line or None


def _opencode_models(run: Runner, now: float) -> list[str]:
    """`opencode models` prints one `provider/model` per line; cached for 10 minutes."""
    global _opencode_models_cache
    if _opencode_models_cache is not None:
        cached_at, cached_models = _opencode_models_cache
        if now - cached_at < MODEL_CACHE_SECONDS:
            return list(cached_models)
    output = run(["opencode", "models"], MODEL_LIST_TIMEOUT_S)
    if output is None:
        return []
    models = [line.strip() for line in output.splitlines() if "/" in line.strip()]
    _opencode_models_cache = (now, models)
    return list(models)


def pi_models(settings_path: Path) -> list[str]:
    """`enabledModels` then `defaultModel` from pi's settings file, names kept as written.

    A missing or unreadable file, or one with neither key, gives the built-in fallback so the
    model field is never left without a suggestion.
    """
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return list(PI_FALLBACK_MODELS)
    if not isinstance(settings, dict):
        return list(PI_FALLBACK_MODELS)
    enabled = settings.get("enabledModels")
    names = [name for name in enabled if isinstance(name, str)] if isinstance(enabled, list) else []
    default_model = settings.get("defaultModel")
    if isinstance(default_model, str):
        names.append(default_model)
    return names or list(PI_FALLBACK_MODELS)


def _read_toml(path: Path) -> dict:
    """Parse a TOML file; a missing or unparsable file is logged and gives an empty dict."""
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        logger.warning("cannot read codex config %s: %s", path, error)
        return {}


def codex_models(codex_home: Path) -> list[str]:
    """`model` of config.toml, then of each `<name>.config.toml` profile, without repeats.

    Codex has no model-list command; these files are the only place the owner's models are
    written down. Profiles are the files `codex exec -p <name>` layers on the base config.
    """
    config_paths = [codex_home / "config.toml"]
    config_paths += sorted(codex_home.glob("*.config.toml"))
    names: list[str] = []
    for config_path in config_paths:
        model = _read_toml(config_path).get("model")
        if isinstance(model, str):
            names.append(model)
    return _deduplicated(names)


def codex_billing(codex_home: Path) -> str:
    """ "metered" when config.toml routes codex to a provider other than OpenAI's own login."""
    model_provider = _read_toml(codex_home / "config.toml").get("model_provider")
    if isinstance(model_provider, str) and model_provider not in ("", "openai"):
        return "metered"
    return "subscription"


def _deduplicated(names: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def detect_harnesses(
    settings: Settings,
    run: Runner = run_command,
    which: Finder = shutil.which,
    now: Callable[[], float] = time.monotonic,
    pi_settings_path: Path = PI_SETTINGS_PATH,
    codex_home: Path = CODEX_HOME,
) -> list[Harness]:
    """One Harness per program in the fixed order claude, opencode, pi, codex."""
    harnesses: list[Harness] = []
    for program in PROGRAM_ORDER:
        harness_settings = settings.harnesses.get(program)
        extra_models = list(harness_settings.extra_models) if harness_settings else []
        path = which(program)
        found = path is not None

        version = parse_version(run([program, "--version"], VERSION_TIMEOUT_S)) if found else None
        if program == "opencode":
            base_models = _opencode_models(run, now()) if found else []
        elif program == "pi":
            base_models = pi_models(pi_settings_path)
        elif program == "codex":
            base_models = codex_models(codex_home) or list(BUILT_IN_MODEL_SUGGESTIONS["codex"])
        else:
            base_models = list(BUILT_IN_MODEL_SUGGESTIONS[program])

        if program == "claude":
            has_base_url = bool(harness_settings and harness_settings.base_url)
            billing = "metered" if has_base_url else "subscription"
        elif program == "codex":
            billing = codex_billing(codex_home)
        else:
            billing = "metered"

        harnesses.append(
            Harness(
                program=program,
                found=found,
                path=path,
                version=version,
                supports_thinking=True,
                model_suggestions=_deduplicated(base_models + extra_models),
                login_hint=LOGIN_HINTS[program],
                default_model=(harness_settings.model or None) if harness_settings else None,
                default_thinking=harness_settings.thinking if harness_settings else "default",
                enabled=harness_settings.enabled if harness_settings else True,
                billing=billing,
            )
        )
    return harnesses
