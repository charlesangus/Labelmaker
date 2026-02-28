import os

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QCheckBox,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import labelmaker
import labelmaker_config
import labelmaker_prefs


class LabelmakerPrefsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Labelmaker Preferences")
        self.setMinimumWidth(500)
        self._build_ui()
        self._populate_from_prefs()

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        form_layout = QFormLayout()
        main_layout.addLayout(form_layout)

        # labelmaker_enabled checkbox (master switch — at top)
        self.labelmaker_enabled_checkbox = QCheckBox()
        self.labelmaker_enabled_checkbox.setToolTip(
            "When unchecked, Labelmaker is disabled entirely and Nuke's default "
            "autolabel behavior is restored. Takes effect immediately."
        )
        form_layout.addRow("Enable Labelmaker:", self.labelmaker_enabled_checkbox)

        # personal_config_path row: line edit + browse button
        path_row_widget = QWidget()
        path_row_layout = QHBoxLayout(path_row_widget)
        path_row_layout.setContentsMargins(0, 0, 0, 0)

        self.personal_config_path_edit = QLineEdit()
        self.personal_config_path_edit.setToolTip(
            "This file holds your personal configuration for Labelmaker, "
            "which overrides all other configs."
        )
        path_row_layout.addWidget(self.personal_config_path_edit)

        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self._browse_personal_config_path)
        path_row_layout.addWidget(browse_button)

        form_layout.addRow("Personal Config Path:", path_row_widget)

        # always_show_all checkbox
        self.always_show_all_checkbox = QCheckBox()
        self.always_show_all_checkbox.setToolTip(
            "By default, most labels show only if the knob value is not default. "
            "This causes the node size to change when knobs are adjusted, but makes "
            "it easier to see at a glance which knobs are in use. This option displays "
            "all knob labels, all the time, keeping node sizes constant."
        )
        form_layout.addRow("Always Show All Labels:", self.always_show_all_checkbox)

        # colorize_disable checkbox
        self.colorize_disable_checkbox = QCheckBox()
        self.colorize_disable_checkbox.setToolTip(
            "By default, Labelmaker colorizes certain knobs, which enables you to see "
            "what RGB/RGBA knobs are doing at a glance. Disable this function here if "
            "you find it distracting."
        )
        form_layout.addRow("Disable Colorization:", self.colorize_disable_checkbox)

        # use_base_config checkbox
        env_var_active = os.environ.get("LABELMAKER_DISABLE_BASE_CONFIG") == "1"
        self.use_base_config_checkbox = QCheckBox()
        self.use_base_config_checkbox.setToolTip(
            "Use the default base config which ships with Labelmaker in addition to "
            "any custom or personal configs you have set up."
            + (" (Disabled by LABELMAKER_DISABLE_BASE_CONFIG environment variable.)" if env_var_active else "")
        )
        if env_var_active:
            self.use_base_config_checkbox.setEnabled(False)
        form_layout.addRow("Use Base Config:", self.use_base_config_checkbox)

        # deoverlap_enabled checkbox
        self.deoverlap_enabled_checkbox = QCheckBox()
        self.deoverlap_enabled_checkbox.setToolTip(
            "When enabled, Labelmaker automatically pushes downstream nodes down to "
            "prevent overlap whenever a node's label grows taller."
        )
        form_layout.addRow("Enable Auto De-overlap:", self.deoverlap_enabled_checkbox)

        # OK / Cancel buttons
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        main_layout.addWidget(button_box)

    def _populate_from_prefs(self):
        prefs = labelmaker_prefs.prefs_singleton
        self.labelmaker_enabled_checkbox.setChecked(bool(prefs.get("labelmaker_enabled")))
        self.personal_config_path_edit.setText(prefs.get("personal_config_path") or "")
        self.always_show_all_checkbox.setChecked(bool(prefs.get("always_show_all")))
        self.colorize_disable_checkbox.setChecked(bool(prefs.get("colorize_disable")))
        # For use_base_config, read the raw stored value (not the env-var-masked one)
        raw_use_base_config = prefs._prefs.get("use_base_config", True)
        self.use_base_config_checkbox.setChecked(bool(raw_use_base_config))
        self.deoverlap_enabled_checkbox.setChecked(bool(prefs.get("deoverlap_enabled")))

    def _browse_personal_config_path(self):
        current_path = self.personal_config_path_edit.text()
        start_dir = os.path.dirname(current_path) if current_path else os.path.expanduser("~")
        chosen_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Personal Config File",
            start_dir,
            "JSON Files (*.json);;All Files (*)",
        )
        if chosen_path:
            self.personal_config_path_edit.setText(chosen_path)

    def _on_accept(self):
        prefs = labelmaker_prefs.prefs_singleton
        prefs.set("labelmaker_enabled", self.labelmaker_enabled_checkbox.isChecked())
        prefs.set("personal_config_path", self.personal_config_path_edit.text())
        prefs.set("always_show_all", self.always_show_all_checkbox.isChecked())
        prefs.set("colorize_disable", self.colorize_disable_checkbox.isChecked())
        prefs.set("use_base_config", self.use_base_config_checkbox.isChecked())
        prefs.set("deoverlap_enabled", self.deoverlap_enabled_checkbox.isChecked())
        prefs.save()

        labelmaker_config.reload_composed_config()
        labelmaker.autolabeller_singleton.config = labelmaker_config.composed_config_singleton
        labelmaker.autolabeller_singleton.set_enabled(self.labelmaker_enabled_checkbox.isChecked())

        self.accept()


def show_prefs_dialog():
    dialog = LabelmakerPrefsDialog()
    dialog.exec()
