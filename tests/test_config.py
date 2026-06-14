import json

from labelmaker_config import LabelMakerComposedConfig, LabelMakerConfig
from labelmaker_prefs import LabelmakerPrefs


def _write_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


# --- LabelMakerConfig ---


def test_load_config_from_json(tmp_path):
    config_path = str(tmp_path / "config.json")
    _write_json(config_path, {"Blur": [{"name": "size", "label": "Size", "default": 0}]})
    config = LabelMakerConfig(name="test", path=config_path)
    assert "Blur" in config
    assert config["Blur"][0]["name"] == "size"


def test_save_config_round_trips(tmp_path):
    config_path = str(tmp_path / "config.json")
    original_data = {
        "Grade": [{"name": "whitepoint", "label": "WP", "default": [1.0, 1.0, 1.0, 1.0]}]
    }
    _write_json(config_path, original_data)
    config = LabelMakerConfig(name="test", path=config_path)
    config.save_config()

    reloaded = LabelMakerConfig(name="test", path=config_path)
    assert reloaded["Grade"] == original_data["Grade"]


def test_add_node_class_new_class(tmp_path):
    config_path = str(tmp_path / "config.json")
    _write_json(config_path, {})
    config = LabelMakerConfig(name="test", path=config_path)
    result = config.add_node_class("ColorCorrect")
    assert result is True
    assert "ColorCorrect" in config
    assert config["ColorCorrect"] == []


def test_add_node_class_duplicate_returns_false(tmp_path):
    config_path = str(tmp_path / "config.json")
    _write_json(config_path, {"Blur": []})
    config = LabelMakerConfig(name="test", path=config_path)
    assert config.add_node_class("Blur") is False


def test_add_node_class_does_not_share_default_list(tmp_path):
    """Two classes added without explicit labels must not share a list."""
    config_path = str(tmp_path / "config.json")
    _write_json(config_path, {})
    config = LabelMakerConfig(name="test", path=config_path)
    config.add_node_class("Blur")
    config.add_node_class("Grade")
    config.add_label("Blur", {"name": "size"})
    assert config["Grade"] == []


def test_add_label(tmp_path):
    config_path = str(tmp_path / "config.json")
    _write_json(config_path, {"Blur": []})
    config = LabelMakerConfig(name="test", path=config_path)
    label = {"name": "size", "label": "Size", "default": 0}
    config.add_label("Blur", label)
    assert config["Blur"] == [label]


def test_move_label_up(tmp_path):
    config_path = str(tmp_path / "config.json")
    _write_json(config_path, {"Blur": [{"name": "a"}, {"name": "b"}, {"name": "c"}]})
    config = LabelMakerConfig(name="test", path=config_path)
    config.move_label_up("Blur", 1)  # move "b" up
    names = [item["name"] for item in config["Blur"]]
    assert names == ["b", "a", "c"]


def test_move_label_down(tmp_path):
    config_path = str(tmp_path / "config.json")
    _write_json(config_path, {"Blur": [{"name": "a"}, {"name": "b"}, {"name": "c"}]})
    config = LabelMakerConfig(name="test", path=config_path)
    config.move_label_down("Blur", 0)  # move "a" down
    names = [item["name"] for item in config["Blur"]]
    assert names == ["b", "a", "c"]


def test_config_get_returns_none_for_missing_key(tmp_path):
    config_path = str(tmp_path / "config.json")
    _write_json(config_path, {})
    config = LabelMakerConfig(name="test", path=config_path)
    assert config.get("NonExistentNode") is None


# --- LabelMakerComposedConfig ---


def test_composed_config_later_layer_overrides_earlier(tmp_path, monkeypatch):
    base_path = str(tmp_path / "base.json")
    personal_path = str(tmp_path / "personal.json")
    _write_json(base_path, {"Blur": [{"name": "size", "default": 0}], "Grade": []})
    _write_json(personal_path, {"Blur": [{"name": "size", "default": 99}]})

    prefs = LabelmakerPrefs(prefs_file=str(tmp_path / "prefs.json"))
    prefs.set("personal_config_path", personal_path)
    prefs.set("use_base_config", True)

    monkeypatch.setattr("labelmaker_config.labelmaker_prefs.prefs_singleton", prefs)
    monkeypatch.setattr("labelmaker_config.DEFAULT_CONFIG_PATH", base_path)
    monkeypatch.delenv("LABELMAKER_CONFIGS_NAMES", raising=False)
    monkeypatch.delenv("LABELMAKER_CONFIGS_PATHS", raising=False)

    composed = LabelMakerComposedConfig()
    # personal layer overrides base for Blur
    assert composed.get("Blur")[0]["default"] == 99
    # Grade only in base, still accessible
    assert composed.get("Grade") == []


def test_composed_config_with_base_only(tmp_path, monkeypatch):
    base_path = str(tmp_path / "base.json")
    _write_json(base_path, {"Blur": [{"name": "size", "default": 5}]})

    prefs = LabelmakerPrefs(prefs_file=str(tmp_path / "prefs.json"))
    prefs.set("personal_config_path", str(tmp_path / "nonexistent.json"))
    prefs.set("use_base_config", True)

    monkeypatch.setattr("labelmaker_config.labelmaker_prefs.prefs_singleton", prefs)
    monkeypatch.setattr("labelmaker_config.DEFAULT_CONFIG_PATH", base_path)
    monkeypatch.delenv("LABELMAKER_CONFIGS_NAMES", raising=False)
    monkeypatch.delenv("LABELMAKER_CONFIGS_PATHS", raising=False)

    composed = LabelMakerComposedConfig()
    assert composed.get("Blur")[0]["default"] == 5


def test_composed_config_get_config_names(tmp_path, monkeypatch):
    base_path = str(tmp_path / "base.json")
    _write_json(base_path, {})

    prefs = LabelmakerPrefs(prefs_file=str(tmp_path / "prefs.json"))
    prefs.set("personal_config_path", str(tmp_path / "nonexistent.json"))
    prefs.set("use_base_config", True)

    monkeypatch.setattr("labelmaker_config.labelmaker_prefs.prefs_singleton", prefs)
    monkeypatch.setattr("labelmaker_config.DEFAULT_CONFIG_PATH", base_path)
    monkeypatch.delenv("LABELMAKER_CONFIGS_NAMES", raising=False)
    monkeypatch.delenv("LABELMAKER_CONFIGS_PATHS", raising=False)

    composed = LabelMakerComposedConfig()
    assert "default" in composed.get_config_names()
