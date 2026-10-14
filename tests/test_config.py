from pathlib import Path

import pytest

from adda.config import ConfigError, load_settings, read_key_file


def missing_key_file(tmp_path) -> dict[str, str]:
    # Points at a file that does not exist so the real ~/.config/jev is never read.
    return {"ADDA_JEV_KEY_FILE": str(tmp_path / "absent.env")}


def test_defaults(tmp_path):
    settings = load_settings(missing_key_file(tmp_path))
    assert settings.host == "127.0.0.1"
    assert settings.port == 8765
    assert settings.data_dir == Path("data")
    assert settings.personas_dir == Path("personas")
    assert settings.router == "jev"
    assert settings.jev_provider == "openrouter"
    assert settings.jev_model == "typesafe/jev-1.13"
    assert settings.jev_api_key is None
    assert settings.bid_threshold == 5.0
    assert settings.cooldown_s == 20.0
    assert settings.max_auto_turns == 6
    assert settings.seat_timeout_s == 180.0
    assert settings.router_window == 20
    assert settings.database_path == Path("data") / "adda.db"


def test_env_overrides(tmp_path):
    environment = {
        **missing_key_file(tmp_path),
        "ADDA_HOST": "0.0.0.0",
        "ADDA_PORT": "9000",
        "ADDA_DATA_DIR": "/tmp/adda-data",
        "ADDA_PERSONAS_DIR": "/tmp/personas",
        "ADDA_ROUTER": "heuristic",
        "ADDA_JEV_PROVIDER": "typesafe",
        "ADDA_BID_THRESHOLD": "6.5",
        "ADDA_COOLDOWN_S": "3",
        "ADDA_MAX_AUTO_TURNS": "2",
        "ADDA_SEAT_TIMEOUT_S": "30",
        "ADDA_ROUTER_WINDOW": "5",
        "TYPESAFE_API_KEY": "test-key-123",
    }
    settings = load_settings(environment)
    assert settings.host == "0.0.0.0"
    assert settings.port == 9000
    assert settings.data_dir == Path("/tmp/adda-data")
    assert settings.personas_dir == Path("/tmp/personas")
    assert settings.router == "heuristic"
    assert settings.jev_provider == "typesafe"
    assert settings.jev_model == "jev-latest"
    assert settings.jev_api_key == "test-key-123"
    assert settings.bid_threshold == 6.5
    assert settings.cooldown_s == 3.0
    assert settings.max_auto_turns == 2
    assert settings.seat_timeout_s == 30.0
    assert settings.router_window == 5


def test_explicit_model_overrides_provider_default(tmp_path):
    settings = load_settings({**missing_key_file(tmp_path), "ADDA_JEV_MODEL": "custom/model"})
    assert settings.jev_model == "custom/model"


@pytest.mark.parametrize(
    "name, value",
    [("ADDA_PORT", "eighty"), ("ADDA_BID_THRESHOLD", "high"), ("ADDA_COOLDOWN_S", "1,5")],
)
def test_invalid_number_raises_config_error(tmp_path, name, value):
    with pytest.raises(ConfigError, match=name):
        load_settings({**missing_key_file(tmp_path), name: value})


def test_invalid_choice_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="ADDA_ROUTER"):
        load_settings({**missing_key_file(tmp_path), "ADDA_ROUTER": "magic"})


def test_key_read_from_file_when_env_unset(tmp_path):
    key_file = tmp_path / "keys.env"
    key_file.write_text(
        "# comment\n\nexport OPENROUTER_API_KEY='test-key-123'\nTYPESAFE_API_KEY=other-456\n"
    )
    settings = load_settings({"ADDA_JEV_KEY_FILE": str(key_file)})
    assert settings.jev_api_key == "test-key-123"
    typesafe = load_settings({"ADDA_JEV_KEY_FILE": str(key_file), "ADDA_JEV_PROVIDER": "typesafe"})
    assert typesafe.jev_api_key == "other-456"


def test_env_var_wins_over_file(tmp_path):
    key_file = tmp_path / "keys.env"
    key_file.write_text("OPENROUTER_API_KEY=from-file\n")
    settings = load_settings({"ADDA_JEV_KEY_FILE": str(key_file), "OPENROUTER_API_KEY": "from-env"})
    assert settings.jev_api_key == "from-env"


def test_read_key_file_missing_returns_empty(tmp_path):
    assert read_key_file(tmp_path / "absent.env") == {}


def test_settings_repr_hides_key(tmp_path):
    settings = load_settings({**missing_key_file(tmp_path), "OPENROUTER_API_KEY": "test-key-123"})
    assert "test-key-123" not in repr(settings)
