from dataclasses import replace

import pytest

from adda import harnesses
from adda.config import load_settings
from adda.harnesses import (
    clear_model_cache,
    detect_harnesses,
    parse_version,
    pi_models,
    thinking_arguments,
)

PROGRAMS = ("claude", "opencode", "pi", "codex")


@pytest.fixture(autouse=True)
def fresh_cache():
    clear_model_cache()
    yield
    clear_model_cache()


@pytest.fixture
def settings(tmp_path):
    return load_settings(
        {"ADDA_KEYS_FILE": str(tmp_path / "k"), "ADDA_JEV_KEY_FILE": ""}, tmp_path / "config.toml"
    )


class FakeSystem:
    """Stands in for shutil.which and subprocess; records every call."""

    def __init__(self, installed, outputs=None):
        self.installed = installed
        self.outputs = outputs or {}
        self.calls = []

    def which(self, program):
        return f"/usr/bin/{program}" if program in self.installed else None

    def run(self, argv, timeout_s):
        self.calls.append((tuple(argv), timeout_s))
        return self.outputs.get(tuple(argv))


def test_found_programs_get_path_and_version(settings):
    system = FakeSystem(
        {"claude", "pi"},
        {("claude", "--version"): "2.1.299 (Claude Code)\n", ("pi", "--version"): "1.1.0\n"},
    )
    result = detect_harnesses(settings, run=system.run, which=system.which)
    by_program = {harness.program: harness for harness in result}
    assert [harness.program for harness in result] == ["claude", "opencode", "pi", "codex"]
    assert by_program["claude"].found and by_program["claude"].version == "2.1.299"
    assert by_program["claude"].path == "/usr/bin/claude"
    assert by_program["pi"].version == "1.1.0"
    assert not by_program["opencode"].found
    assert by_program["opencode"].version is None
    assert by_program["codex"].path is None


def test_missing_binaries_are_never_probed_and_never_raise(settings):
    system = FakeSystem(set())
    result = detect_harnesses(settings, run=system.run, which=system.which)
    assert system.calls == []
    assert all(not harness.found for harness in result)


def test_failed_version_call_gives_none(settings):
    system = FakeSystem({"claude"})  # run returns None for everything
    claude = detect_harnesses(settings, run=system.run, which=system.which)[0]
    assert claude.found and claude.version is None


def test_version_calls_use_five_second_timeout(settings):
    system = FakeSystem({"claude"}, {("claude", "--version"): "1.0.0"})
    detect_harnesses(settings, run=system.run, which=system.which)
    assert system.calls == [(("claude", "--version"), 5.0)]


def test_defaults_come_from_settings_and_extras_are_appended(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[harness.claude]\nmodel = "opus"\nthinking = "high"\nextra_models = ["sonnet", "x"]\n'
        '[harness.pi]\nenabled = false\nextra_models = ["my/model"]\n',
        encoding="utf-8",
    )
    settings = load_settings({"ADDA_KEYS_FILE": str(tmp_path / "k")}, config_path)
    system = FakeSystem({"claude", "pi"})
    by_program = {
        h.program: h
        for h in detect_harnesses(
            settings,
            system.run,
            system.which,
            pi_settings_path=tmp_path / "no-pi.json",
            codex_home=tmp_path / "no-codex",
        )
    }
    claude, pi, codex = by_program["claude"], by_program["pi"], by_program["codex"]
    assert (claude.default_model, claude.default_thinking) == ("opus", "high")
    assert claude.model_suggestions == [
        "fable",
        "opus",
        "sonnet",
        "haiku",
        "claude-fable-5-1",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
        "x",
    ]
    assert pi.default_model is None and pi.enabled is False
    assert pi.model_suggestions == ["z-ai/glm-5.3-flash", "my/model"]
    assert codex.model_suggestions == ["gpt-5-codex"]
    assert codex.enabled is False


def test_opencode_models_are_listed_and_cached_for_ten_minutes(settings):
    system = FakeSystem(
        {"opencode"},
        {("opencode", "models"): "opencode/big-pickle\nnoise line\nopenrouter/x/y\n"},
    )
    clock = {"now": 1000.0}

    def detect():
        return detect_harnesses(settings, system.run, system.which, lambda: clock["now"])[1]

    first = detect()
    assert first.model_suggestions == ["opencode/big-pickle", "openrouter/x/y"]
    clock["now"] += 599
    detect()
    list_calls = [call for call in system.calls if call[0] == ("opencode", "models")]
    assert len(list_calls) == 1
    assert list_calls[0][1] == 15.0
    clock["now"] += 2
    detect()
    list_calls = [call for call in system.calls if call[0] == ("opencode", "models")]
    assert len(list_calls) == 2


def test_failed_opencode_model_list_is_empty_and_not_cached(settings):
    system = FakeSystem({"opencode"})
    assert detect_harnesses(settings, system.run, system.which)[1].model_suggestions == []
    system.outputs[("opencode", "models")] = "a/b\n"
    assert detect_harnesses(settings, system.run, system.which)[1].model_suggestions == ["a/b"]


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("2.1.299 (Claude Code)", "2.1.299"),
        ("1.17.7\n", "1.17.7"),
        ("codex-cli 0.3.1", "0.3.1"),
        ("weird", "weird"),
        ("", None),
        (None, None),
    ],
)
def test_parse_version(output, expected):
    assert parse_version(output) == expected


def test_thinking_arguments():
    assert thinking_arguments("claude", "default") == []
    assert thinking_arguments("claude", "high") == ["--effort", "high"]
    assert thinking_arguments("pi", "low") == ["--thinking", "low"]
    assert thinking_arguments("opencode", "medium") == ["--variant", "medium"]
    assert thinking_arguments("codex", "low") == ["-c", "model_reasoning_effort=low"]
    with pytest.raises(ValueError):
        thinking_arguments("claude", "extreme")


def test_default_runner_never_raises_for_missing_binary():
    assert harnesses.run_command(["adda-no-such-binary-xyz", "--version"], 1.0) is None


def test_pi_models_come_from_its_settings_file_with_provider_prefixes(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        '{"enabledModels": ["openrouter/a/b", "zai/glm"], "defaultModel": "openrouter/c/d"}',
        encoding="utf-8",
    )
    assert pi_models(settings_file) == ["openrouter/a/b", "zai/glm", "openrouter/c/d"]


def test_pi_models_fall_back_when_the_file_is_missing_or_unusable(tmp_path):
    assert pi_models(tmp_path / "absent.json") == ["z-ai/glm-5.3-flash"]
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert pi_models(broken) == ["z-ai/glm-5.3-flash"]
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    assert pi_models(empty) == ["z-ai/glm-5.3-flash"]


def test_detect_harnesses_lists_pi_models_from_the_given_file(settings, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"defaultModel": "p/m"}', encoding="utf-8")
    system = FakeSystem({"pi"})
    result = detect_harnesses(settings, system.run, system.which, pi_settings_path=settings_file)
    assert result[2].model_suggestions == ["p/m"]


# --- codex models and billing, claude billing ------------------------------------------------


def write_codex_home(codex_home, base="", profiles=None):
    codex_home.mkdir(exist_ok=True)
    (codex_home / "config.toml").write_text(base, encoding="utf-8")
    for name, text in (profiles or {}).items():
        (codex_home / f"{name}.config.toml").write_text(text, encoding="utf-8")


def test_codex_models_lists_base_then_profiles_without_repeats(tmp_path):
    write_codex_home(
        tmp_path,
        'model = "openai/gpt-6-luna"\nmodel_provider = "openrouter"\n',
        {"fast": 'model = "gpt-5-mini"\n', "same": 'model = "openai/gpt-6-luna"\n'},
    )
    assert harnesses.codex_models(tmp_path) == ["openai/gpt-6-luna", "gpt-5-mini"]


def test_codex_models_tolerates_missing_and_broken_files(tmp_path, caplog):
    assert harnesses.codex_models(tmp_path / "absent") == []
    write_codex_home(tmp_path, "model = [unclosed", {"ok": 'model = "m"\n'})
    assert harnesses.codex_models(tmp_path) == ["m"]
    assert "cannot read codex config" in caplog.text


def test_detect_uses_codex_config_and_falls_back_to_built_in_models(settings, tmp_path):
    system = FakeSystem({"codex"})
    write_codex_home(tmp_path, 'model = "openai/gpt-6-luna"\n')
    result = detect_harnesses(settings, system.run, system.which, codex_home=tmp_path)
    codex = {harness.program: harness for harness in result}["codex"]
    assert codex.model_suggestions == ["openai/gpt-6-luna"]
    result = detect_harnesses(settings, system.run, system.which, codex_home=tmp_path / "none")
    codex = {harness.program: harness for harness in result}["codex"]
    assert codex.model_suggestions == harnesses.BUILT_IN_MODEL_SUGGESTIONS["codex"]


@pytest.mark.parametrize(
    ("config_text", "expected_billing"),
    [
        ('model_provider = "openrouter"\n', "metered"),
        ('model_provider = "openai"\n', "subscription"),
        ('model_provider = ""\n', "subscription"),
        ('model = "gpt-5"\n', "subscription"),
        ("", "subscription"),
    ],
)
def test_codex_billing_follows_model_provider(tmp_path, config_text, expected_billing):
    write_codex_home(tmp_path, config_text)
    assert harnesses.codex_billing(tmp_path) == expected_billing


def test_billing_per_harness(tmp_path):
    settings = load_settings(
        {"ADDA_KEYS_FILE": str(tmp_path / "k"), "ADDA_JEV_KEY_FILE": ""}, tmp_path / "config.toml"
    )
    write_codex_home(tmp_path / "codex", 'model_provider = "openrouter"\n')
    system = FakeSystem(set(PROGRAMS))

    def billing_of(current_settings):
        found = detect_harnesses(
            current_settings,
            system.run,
            system.which,
            pi_settings_path=tmp_path / "no-pi.json",
            codex_home=tmp_path / "codex",
        )
        return {harness.program: harness.billing for harness in found}

    assert billing_of(settings) == {
        "claude": "subscription",
        "opencode": "metered",
        "pi": "metered",
        "codex": "metered",
    }
    with_base_url = dict(settings.harnesses)
    with_base_url["claude"] = replace(
        settings.harnesses["claude"], base_url="https://proxy.example/v1"
    )
    assert billing_of(replace(settings, harnesses=with_base_url))["claude"] == "metered"
