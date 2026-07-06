"""Capture-session bootstrap for nuke-screenshotter.

Put this folder on NUKE_PATH (see the repo Makefile) so the screenshotter's
full-GUI Nuke session sources this menu.py, which:

  1. Loads Labelmaker the way a real user installs it (nuke.pluginAddPath), so the
     DAG shows Labelmaker autolabels and the Edit > Labelmaker commands exist.
  2. Disables Labelmaker's auto-deoverlap for the session, so refreshing labels
     does not move nodes (which would shift them out of the captured backdrop
     regions and can cascade).

Forcing the autolabels to redraw used to be this bootstrap's job too: Nuke only
computes a node's full label once the viewport has been centred on it at a
high-enough zoom, so a plain backdrop grab captured stale (class-name-only)
labels. We worked around it by cutting and pasting every node on script load.
nuke-screenshotter v1.2 (#6) fixes this in the capture path itself — it
runs a warm-up pass that centres each in-backdrop node at the render zoom so
Nuke caches the full label before the grab — so the workaround is no longer
needed and has been removed.

This is a menu.py (not init.py): Labelmaker calls GUI-only APIs (nuke.toolbar) at
import, which only work once the GUI is up (the menu phase). init.py is also
sourced by non-GUI render workers, where that import would crash.
"""
import os
import sys

import nuke

bootstrap_dir = os.path.dirname(os.path.abspath(__file__))
labelmaker_dir = os.path.dirname(os.path.dirname(os.path.dirname(bootstrap_dir)))

if labelmaker_dir not in sys.path:
    sys.path.insert(0, labelmaker_dir)
nuke.pluginAddPath(labelmaker_dir)

try:
    import labelmaker  # noqa: F401  registers the autolabel (enabled by default)
    import labelmaker_config_editor  # noqa: F401  Edit > Labelmaker Config Editor
    import labelmaker_deoverlap  # noqa: F401
    import labelmaker_prefs  # noqa: F401
    import labelmaker_prefs_dialog  # noqa: F401  Edit > Labelmaker Preferences

    # Auto-deoverlap would move nodes as labels grow during the forced refresh.
    labelmaker_prefs.prefs_singleton._prefs["deoverlap_enabled"] = False

    # Capture-only shortcuts for the panel scenarios (panels.scenarios.json).
    # The scenarios must open each dialog deterministically. Driving the Edit menu
    # by typing the item name is not viable: the screenshotter types into the live
    # focusWidget, which in an idle session is the DAG — so the letters become node
    # hotkeys (e.g. "E" triggers a personal ~/.nuke plugin and crashes the capture).
    # Instead we register a unique, collision-free chord per dialog and the scenario
    # presses just that chord; no letters ever reach the DAG.
    # The real prefs entry point (show_prefs_dialog) calls dialog.exec(), which is
    # modal and blocks Nuke's event loop — the playback runner would hang there
    # until the capture times out. For the capture we show the same dialog
    # non-modally instead; the reference is held so Qt does not garbage-collect it
    # while it is on screen. (show_config_editor is already non-modal.)
    captured_dialogs = []

    def _show_prefs_dialog_nonmodal():
        prefs_dialog = labelmaker_prefs_dialog.LabelmakerPrefsDialog()
        captured_dialogs.append(prefs_dialog)
        prefs_dialog.show()

    capture_menu = nuke.menu("Nuke").addMenu("LabelmakerCapture")
    capture_menu.addCommand(
        "Open Preferences",
        _show_prefs_dialog_nonmodal,
        "Ctrl+Alt+Shift+P",
    )
    capture_menu.addCommand(
        "Open Config Editor",
        labelmaker_config_editor.show_config_editor,
        "Ctrl+Alt+Shift+C",
    )

    sys.stderr.write("LABELMAKER_BOOTSTRAP_OK\n")
    sys.stderr.flush()
except Exception:
    import traceback
    sys.stderr.write("LABELMAKER_BOOTSTRAP_FAIL\n" + traceback.format_exc())
    sys.stderr.flush()
