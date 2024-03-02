import json
import os
from collections import UserDict
import labelmaker_prefs

# Location of the default config shipped with Labelmaker
# get from env var, or fallback to adjacent to this file
DEFAULT_CONFIG_PATH = os.environ.get(
    "LABELMAKER_DEFAULT_CONFIG_PATH",
    os.path.join(os.path.dirname(__file__), "base_config.json"),
)

# For if your facility has its own base config and you would like to
# disallow users from using the included Labelmaker base_config.json
# Set this env var to '1' to disable the base config.
DISABLE_BASE_CONFIG_ENV_VAR = "LABELMAKER_DISABLE_BASE_CONFIG"

# A ':' (or ';' on Windows) delimited list of config names, e.g.
# SITE:SHOW
# the names "default" and "personal" are used by LabelMaker, and should not be used
CONFIGS_NAMES_ENV_VAR = "LABELMAKER_CONFIGS_NAMES"
# A ':' (or ';' on Windows) delimited list of config file paths, e.g.
# "/foo/bar/labelmaker_site_config.json":"/foo/baz/labelmaker_show_config.json"
# Do not include Labelmaker's default config or your personal config
CONFIGS_PATHS_ENV_VAR = "LABELMAKER_CONFIGS_PATHS"

# get from prefs and fallback
PERSONAL_CONFIG_PATH = labelmaker_prefs.prefs_singleton.get_pref(
    "personal_config_path"
) or os.path.join(os.path.expanduser("~"), ".nuke", "labelmaker_config.json")


class LabelMakerComposedConfig(object):
    """Compose a config out of the various configs.

    Intended for use by the actual labelmaker.
    """

    def __init__(self):
        super(LabelMakerComposedConfig, self).__init__()
        self.configs = []

        use_base_config_knob = labelmaker_prefs.prefs_singleton.get_pref_knob("use_base_config")

        if DISABLE_BASE_CONFIG_ENV_VAR in os.environ.keys() and os.environ[DISABLE_BASE_CONFIG_ENV_VAR] == '1':
            # Do not allow the user to turn on the base config
            # if it has been disabled by the env var
            use_base_config_knob.setValue(False)
            use_base_config_knob.setEnabled(False)
        else:
            # enable the base config knob in case it was previously
            # disabled by the env var

            use_base_config_knob.setValue(True)

            if os.path.exists(DEFAULT_CONFIG_PATH) and labelmaker_prefs.prefs_singleton.get_pref("use_base_config"):
                default_config = LabelMakerConfig(name="default", path=DEFAULT_CONFIG_PATH)
                self.configs.append(default_config)

        if (
            CONFIGS_NAMES_ENV_VAR in os.environ.keys()
            and CONFIGS_PATHS_ENV_VAR in os.environ.keys()
        ):
            custom_config_names = os.environ[CONFIGS_NAMES_ENV_VAR].split(os.pathsep)
            custom_config_paths = os.environ[CONFIGS_PATHS_ENV_VAR].split(os.pathsep)
            custom_config_tuples = zip(custom_config_names, custom_config_paths)
            for custom_config_tuple in custom_config_tuples:
                if os.path.exists(custom_config_tuple[1]):
                    custom_config = LabelMakerConfig(
                        name=custom_config_tuple[0], path=custom_config_tuple[1]
                    )
                    self.configs.append(custom_config)

        if os.path.exists(PERSONAL_CONFIG_PATH):
            personal_config = LabelMakerConfig(
                name="personal", path=PERSONAL_CONFIG_PATH
            )
            self.configs.append(personal_config)

        self.composed_config_dict = {}
        for config in self.configs:
            self.composed_config_dict.update(config.get_underlying_dict())

    def __getitem__(self, node_class):
        self.composed_config_dict[node_class]

    def get(self, key, default=None):
        return self.composed_config_dict.get(key, default)

    def get_config_names(self):
        names = [config.name for config in self.configs]
        return names

    def get_config_by_name(self, name):
        config = [config for config in self.configs if config.name == name][0]
        return config


class LabelMakerConfig(UserDict, object):
    """A single config."""

    def __init__(self, name, path):
        super(LabelMakerConfig, self).__init__()
        self.name = name
        self.path = path
        self.data = self.load_config()

    def load_config(self):
        with open(self.path, "r") as f:
            config = json.load(f)
        self.dirty = False
        return config

    def save_config(self):
        with open(self.path, "w+") as f:
            json.dump(self.data, f)
        self.dirty = False

    def get_underlying_dict(self):
        return self.data

    def node_class_labels(self, node_class):
        labels = self[node_class]
        return labels

    def add_node_class(self, node_class, labels=[]):
        if node_class in self.keys():
            return False
        else:
            self.dirty = True
            self[node_class] = labels
            return True

    def add_label(self, node_class, label):
        self[node_class].append(label)

    def move_label_up(self, node_class, old_index):
        node_object = self[node_class]
        node_object.insert(old_index - 1, node_object.pop(old_index))
        self[node_object] = node_object

    def move_label_down(self, node_class, old_index):
        node_object = self[node_class]
        node_object.insert(old_index + 1, node_object.pop(old_index))
        self[node_object] = node_object


composed_config_singleton = LabelMakerComposedConfig()
