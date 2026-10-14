import stat
import tomllib
from pathlib import Path

import pytest

from adda.config import (
    ConfigError,
    load_keys,
    load_settings,
    read_key_file,
    save_key,
    save_settings,
    settings_view,
)


@pytest.fixture
def isolated(tmp_path):
    """Paths that point away from the real ~/.config so the owner's keys are never read."""
    return {
        "config_path": tmp_path / "config.toml",
        "environment": {
            "ADDA_KEYS_FILE": str(tmp_path / "keys.env"),
            "ADDA_JEV_KEY_FILE": str(tmp_path / "legacy.env"),
        },
    }


def load(isolated, extra_environment=None):
    environment = {**isolated["environment"], **(extra_environment or {})}
    return load_settings(environment, isolated["config_path"])


def test_defaults(isolated):
    settings = load(isolated)
    assert settings.host == "127.0.0.1"
    assert settings.port == 8765
    assert settings.rooms_dir == Path("~/Adda-Rooms").expanduser()
    assert settings.data_dir == Path("~/.adda").expanduser()
    assert settings.router_kind == "jev"
    assert settings.jev_model == "typesafe/jev-1.13"
    assert settings.router_threshold_floor == 2.5
    assert settings.liveliness == 6
    assert settings.budget_usd == 1.0
    assert settings.max_auto_turns == 40
    assert settings.harnesses["claude"].model == "sonnet"
    assert settings.harnesses["claude"].thinking == "medium"
    assert settings.harnesses["codex"].enabled is False
    assert settings.database_path == settings.data_dir / "adda.db"
    assert settings.env_overrides == ()
    assert settings.jev_api_key is None


def test_toml_overrides_defaults_and_keeps_unset_fields(isolated):
    isolated["config_path"].write_text(
        '[owner]\nname = "Riad"\n[room_defaults]\nliveliness = 9\n'
        '[harness.pi]\nmodel = "z-ai/x"\nextra_models = ["a", "b"]\n',
        encoding="utf-8",
    )
    settings = load(isolated)
    assert settings.owner_name == "Riad"
    assert settings.liveliness == 9
    assert settings.budget_usd == 1.0
    assert settings.harnesses["pi"].model == "z-ai/x"
    assert settings.harnesses["pi"].extra_models == ("a", "b")
    assert settings.harnesses["claude"].model == "sonnet"


def test_environment_overrides_toml_and_is_reported(isolated):
    isolated["config_path"].write_text(
        '[owner]\nname = "Riad"\n[router]\nkind = "heuristic"\n', encoding="utf-8"
    )
    settings = load(isolated, {"ADDA_OWNER_NAME": "Env", "ADDA_ROUTER_WINDOW": "7"})
    assert settings.owner_name == "Env"
    assert settings.router_window == 7
    assert settings.router_kind == "heuristic"
    assert set(settings.env_overrides) == {"owner.name", "router.window"}
    assert set(settings_view(settings)["env_overrides"]) == {"owner.name", "router.window"}


def test_adda_config_variable_selects_the_file(tmp_path, isolated):
    other_file = tmp_path / "elsewhere.toml"
    other_file.write_text('[owner]\nname = "Elsewhere"\n', encoding="utf-8")
    settings = load_settings({**isolated["environment"], "ADDA_CONFIG": str(other_file)})
    assert settings.owner_name == "Elsewhere"


@pytest.mark.parametrize(
    "toml_text",
    [
        "[room_defaults]\nliveliness = 11\n",
        '[room_defaults]\nmode = "chaos"\n',
        '[router]\nkind = "oracle"\n',
        '[harness.claude]\nthinking = "extreme"\n',
        '[room_defaults]\nbudget_usd = "free"\n',
        "[owner\n",
    ],
)
def test_invalid_toml_values_fail_loudly(isolated, toml_text):
    isolated["config_path"].write_text(toml_text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load(isolated)


def test_invalid_environment_number_fails_loudly(isolated):
    with pytest.raises(ConfigError, match="ADDA_PORT"):
        load(isolated, {"ADDA_PORT": "eighty"})


def test_key_lookup_order_environment_then_keys_file_then_legacy(tmp_path, isolated):
    keys_file = tmp_path / "keys.env"
    legacy_file = tmp_path / "legacy.env"
    legacy_file.write_text("OPENROUTER_API_KEY=from-legacy\n", encoding="utf-8")
    environment = dict(isolated["environment"])

    assert load_keys(environment)["OPENROUTER_API_KEY"] == "from-legacy"
    keys_file.write_text("OPENROUTER_API_KEY=from-keys-file\nDEEPSEEK_API_KEY=d\n")
    assert load_keys(environment)["OPENROUTER_API_KEY"] == "from-keys-file"
    environment["OPENROUTER_API_KEY"] = "from-env"
    assert load_keys(environment)["OPENROUTER_API_KEY"] == "from-env"
    assert load_keys(environment)["DEEPSEEK_API_KEY"] == "d"


def test_legacy_file_only_supplies_the_openrouter_key(tmp_path, isolated):
    (tmp_path / "legacy.env").write_text("TYPESAFE_API_KEY=nope\n", encoding="utf-8")
    assert "TYPESAFE_API_KEY" not in load_keys(isolated["environment"])


def test_anthropic_key_is_never_read(tmp_path, isolated):
    (tmp_path / "keys.env").write_text("ANTHROPIC_API_KEY=secret\n", encoding="utf-8")
    keys = load_keys({**isolated["environment"], "ANTHROPIC_API_KEY": "secret"})
    assert "ANTHROPIC_API_KEY" not in keys


def test_settings_carry_the_provider_key_and_view_shows_only_booleans(tmp_path, isolated):
    (tmp_path / "keys.env").write_text("OPENROUTER_API_KEY=sk-test-value\n", encoding="utf-8")
    settings = load(isolated)
    assert settings.jev_api_key == "sk-test-value"
    view = settings_view(settings)
    assert view["keys"] == {"openrouter": True, "typesafe": False, "deepseek": False}
    assert "sk-test-value" not in repr(settings)
    assert "sk-test-value" not in str(view)


def test_save_key_writes_mode_600_and_keeps_other_lines(tmp_path):
    keys_file = tmp_path / "sub" / "keys.env"
    save_key("openrouter", "first", keys_file)
    save_key("DEEPSEEK_API_KEY", "other", keys_file)
    save_key("openrouter", "second", keys_file)
    assert stat.S_IMODE(keys_file.stat().st_mode) == 0o600
    assert read_key_file(keys_file) == {"OPENROUTER_API_KEY": "second", "DEEPSEEK_API_KEY": "other"}


def test_save_key_rejects_unknown_names_and_multiline_values(tmp_path):
    with pytest.raises(ConfigError):
        save_key("ANTHROPIC_API_KEY", "x", tmp_path / "keys.env")
    with pytest.raises(ConfigError):
        save_key("openrouter", "a\nB=c", tmp_path / "keys.env")


def test_save_settings_round_trip(isolated):
    settings = load(isolated)
    view = settings_view(settings)
    view["owner"] = {"name": 'Riad "R"', "line": "Line one\nline two, with ünicode"}
    view["room_defaults"]["liveliness"] = 3
    view["room_defaults"]["budget_usd"] = 0
    view["harness"]["claude"]["extra_models"] = ["sonnet-x"]
    written_path = save_settings(view, isolated["config_path"])

    assert tomllib.loads(written_path.read_text(encoding="utf-8"))["owner"]["name"] == 'Riad "R"'
    reloaded = load(isolated)
    assert reloaded.owner_name == 'Riad "R"'
    assert reloaded.owner_line == "Line one\nline two, with ünicode"
    assert reloaded.liveliness == 3
    assert reloaded.budget_usd == 0
    assert reloaded.harnesses["claude"].extra_models == ("sonnet-x",)
    assert settings_view(reloaded)["harness"] == view["harness"]


def test_save_settings_does_not_bake_environment_values_into_the_file(isolated):
    isolated["config_path"].write_text('[owner]\nname = "FromFile"\n', encoding="utf-8")
    settings = load(isolated, {"ADDA_OWNER_NAME": "FromEnv"})
    view = settings_view(settings)
    view["room_defaults"]["liveliness"] = 2
    save_settings(view, isolated["config_path"])
    assert load(isolated).owner_name == "FromFile"
    assert load(isolated).liveliness == 2


def test_save_settings_rejects_invalid_values_and_writes_nothing(isolated):
    view = settings_view(load(isolated))
    view["room_defaults"]["liveliness"] = 99
    with pytest.raises(ConfigError):
        save_settings(view, isolated["config_path"])
    assert not isolated["config_path"].exists()
