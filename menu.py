import nuke
import labelmaker
import labelmaker_deoverlap
import labelmaker_prefs_dialog

edit_menu = nuke.menu("Nuke").findItem("Edit")
edit_menu.addCommand(
    "Labelmaker Preferences...",
    labelmaker_prefs_dialog.show_prefs_dialog,
)
edit_menu.addCommand(
    "De-overlap All Nodes",
    lambda: labelmaker_deoverlap.deoverlap_all(undoable=True),
)

nuke.addOnScriptLoad(lambda: labelmaker_deoverlap.deoverlap_all(undoable=True))
