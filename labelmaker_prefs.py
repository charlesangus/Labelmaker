import os
import nuke
import custom_prefs


def setup_prefs():
    knobs_list = []

    personal_config_path_knob = nuke.File_Knob(
        "personal_config_path", "Personal Config Path",
    )
    personal_config_path_knob.setTooltip(
        "This file holds your personal configuration for Labelmaker, which "
        "overrides all other configs."
    )
    home_dir = os.path.expanduser("~")
    default_value = os.path.join(home_dir, ".nuke", "labelmaker_config.json",)
    personal_config_path_knob.setValue(default_value)
    knobs_list.append(personal_config_path_knob)

    div_one = nuke.Text_Knob("div_one", "",)
    knobs_list.append(div_one)

    always_show_all_knob = nuke.Boolean_Knob(
        "always_show_all", "Always Show All Labels",
    )
    always_show_all_knob.setTooltip(
        "By default, most labels show only if the knob value "
        "is not default. This causes the node size to change "
        "when knobs are adjusted, but makes it easier to see "
        "at a glance which knobs are in use. This option displays "
        "all knob labels, all the time, keeping node sizes constant."
    )
    knobs_list.append(always_show_all_knob)

    colorize_enable_knob = nuke.Boolean_Knob(
        "colorize_disable", "Disable Colorization",
    )
    colorize_enable_knob.setTooltip(
        "By default, Labelmaker colorizes certain knobs, which enables "
        "you to see what RGB/RGBA knobs are doing at a glance. "
        "Disable this function here if you find it distracting."
    )
    knobs_list.append(colorize_enable_knob)

    use_base_config_knob = nuke.Boolean_Knob(
        "use_base_config", "Use Base Config",
    )
    use_base_config_knob.setTooltip(
        "Use the default base config which ships with Labelmaker "
        "in addition to any custom or personal configs you have set up."
    )
    use_base_config_knob.setValue(True)
    knobs_list.append(use_base_config_knob)

    custom_pref_manager = custom_prefs.CustomPrefs(
        tab_label="Labelmaker",
        knob_name_prefix="labelmaker",
        knobs_list=knobs_list,
        version="0.1.0",
    )

    return custom_pref_manager

prefs_singleton = setup_prefs()
