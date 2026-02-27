import nuke
import labelmaker
import labelmaker_prefs_dialog

nuke.menu("Nuke").findItem("Edit").addCommand(
    "Labelmaker Preferences...",
    labelmaker_prefs_dialog.show_prefs_dialog,
)
