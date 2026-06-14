import json

import pytest

from labelmaker_prefs import DEFAULTS, LabelmakerPrefs


def test_fresh_prefs_return_all_defaults(tmp_path):
    prefs = LabelmakerPrefs(prefs_file=str(tmp_path / "prefs.json"))
    for key, expected in DEFAULTS.items():
        assert prefs.get(key) == expected


def test_set_and_get_bool(tmp_path):
    prefs = LabelmakerPrefs(prefs_file=str(tmp_path / "prefs.json"))
    prefs.set("always_show_all", True)
    assert prefs.get("always_show_all") is True


def test_set_and_get_string(tmp_path):
    prefs = LabelmakerPrefs(prefs_file=str(tmp_path / "prefs.json"))
    prefs.set("personal_config_path", "/custom/path/config.json")
    assert prefs.get("personal_config_path") == "/custom/path/config.json"


def test_save_then_reload_persists_value(tmp_path):
    prefs_path = str(tmp_path / "prefs.json")
    prefs = LabelmakerPrefs(prefs_file=prefs_path)
    prefs.set("colorize_disable", True)
    prefs.save()

    reloaded = LabelmakerPrefs(prefs_file=prefs_path)
    assert reloaded.get("colorize_disable") is True


def test_reload_reverts_in_memory_changes(tmp_path):
    prefs_path = str(tmp_path / "prefs.json")
    prefs = LabelmakerPrefs(prefs_file=prefs_path)
    prefs.set("deoverlap_enabled", False)
    prefs.save()

    prefs.set("deoverlap_enabled", True)  # unsaved in-memory change
    prefs.reload()
    assert prefs.get("deoverlap_enabled") is False


def test_partial_file_merges_with_defaults(tmp_path):
    prefs_path = str(tmp_path / "prefs.json")
    with open(prefs_path, "w") as f:
        json.dump({"always_show_all": True}, f)

    prefs = LabelmakerPrefs(prefs_file=prefs_path)
    assert prefs.get("always_show_all") is True
    assert prefs.get("colorize_disable") is False  # still the default


def test_use_base_config_overridden_by_env_var(tmp_path, monkeypatch):
    prefs = LabelmakerPrefs(prefs_file=str(tmp_path / "prefs.json"))
    monkeypatch.setenv("LABELMAKER_DISABLE_BASE_CONFIG", "1")
    assert prefs.get("use_base_config") is False


def test_use_base_config_not_overridden_when_env_var_absent(tmp_path, monkeypatch):
    prefs = LabelmakerPrefs(prefs_file=str(tmp_path / "prefs.json"))
    monkeypatch.delenv("LABELMAKER_DISABLE_BASE_CONFIG", raising=False)
    assert prefs.get("use_base_config") is True


@pytest.mark.parametrize("key", list(DEFAULTS.keys()))
def test_each_default_key_is_accessible(tmp_path, key):
    prefs = LabelmakerPrefs(prefs_file=str(tmp_path / "prefs.json"))
    value = prefs.get(key)
    assert value == DEFAULTS[key] or key == "use_base_config"  # env var may override
