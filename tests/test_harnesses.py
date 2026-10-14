import pytest

from adda import harnesses
from adda.config import load_settings
from adda.harnesses import clear_model_cache, detect_harnesses, parse_version, thinking_arguments


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
    by_program = {h.program: h for h in detect_harnesses(settings, system.run, system.which)}
    claude, pi, codex = by_program["claude"], by_program["pi"], by_program["codex"]
    assert (claude.default_model, claude.default_thinking) == ("opus", "high")
    assert claude.model_suggestions == ["sonnet", "opus", "haiku", "x"]
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
