import json
import os

PREFS_FILE = os.path.join(os.path.expanduser("~"), ".nuke", "labelmaker_prefs.json")

DEFAULTS = {
    "personal_config_path": os.path.join(os.path.expanduser("~"), ".nuke", "labelmaker_config.json"),
    "labelmaker_enabled": True,
    "always_show_all": False,
    "colorize_disable": False,
    "use_base_config": True,
    "deoverlap_enabled": True,
}


class LabelmakerPrefs:
    def __init__(self, prefs_file=PREFS_FILE):
        self._prefs_file = prefs_file
        self._prefs = self._load()

    def _load(self):
        if os.path.exists(self._prefs_file):
            with open(self._prefs_file, "r") as f:
                loaded = json.load(f)
            return {**DEFAULTS, **loaded}
        return dict(DEFAULTS)

    def get(self, key):
        if key == "use_base_config" and os.environ.get("LABELMAKER_DISABLE_BASE_CONFIG") == "1":
            return False
        return self._prefs.get(key, DEFAULTS.get(key))

    def set(self, key, value):
        self._prefs[key] = value

    def save(self):
        with open(self._prefs_file, "w") as f:
            json.dump(self._prefs, f, indent=2)

    def reload(self):
        self._prefs = self._load()


prefs_singleton = LabelmakerPrefs()
