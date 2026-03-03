import nuke
import labelmaker
import labelmaker_deoverlap
import labelmaker_prefs_dialog

edit_menu = nuke.menu("Nuke").findItem("Edit")

def _find_item_index(parent_menu, item_name):
    for position, menu_item in enumerate(parent_menu.items()):
        if menu_item.name() == item_name:
            return position
    return -1

project_settings_index = _find_item_index(edit_menu, "Project Settings...")
edit_menu.addCommand(
    "Labelmaker Preferences...",
    labelmaker_prefs_dialog.show_prefs_dialog,
    index=project_settings_index + 1,
)

node_layout_menu = edit_menu.addMenu("Node Layout")
node_layout_menu.addCommand(
    "De-overlap All Nodes",
    lambda: labelmaker_deoverlap.deoverlap_all(undoable=True),
)
