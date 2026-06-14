"""Non-modal GUI editor for Labelmaker config files.

Lets the user pick a config layer (base / facility / personal), browse the node
classes and labels it defines, and add / modify / reorder / delete labels for
both simple knob-readout labels and TCL labels. Saving writes the edited layer
to disk and reloads the live autolabeller so changes apply without restarting
Nuke.

Because the cascade replaces whole node-class entries (a later layer's "Grade"
list replaces the earlier one wholesale, with no per-label merge), classes that
are inherited from a lower-priority layer are shown as greyed-out "ghost" rows.
Forking one copies the inherited definition into the layer being edited so it
can be overridden.
"""

import copy
import json
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QCheckBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

import nuke

import labelmaker
import labelmaker_config
import labelmaker_prefs

# Keys understood by the simple-label form. Any other keys on a label dict are
# preserved untouched on save (forward compatibility / facility extensions).
KNOWN_SIMPLE_KEYS = {"name", "label", "default", "always_show", "colorize"}

GHOST_COLOR = QColor(140, 140, 140)
OVERRIDDEN_COLOR = QColor(150, 150, 150)


def _nuke_main_window():
    """Best-effort lookup of Nuke's main window to parent the editor to."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return None
    for widget in app.topLevelWidgets():
        if widget.metaObject().className() == "Foundry::UI::DockMainWindow":
            return widget
    return None


def _path_is_writable(path):
    """Whether ``path`` can be written, walking up to the nearest existing parent
    when the file itself does not exist yet."""
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return bool(probe) and os.access(probe, os.W_OK)


class LabelmakerConfigEditor(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Tool)
        self.setWindowTitle("Labelmaker Config Editor")
        self.setMinimumSize(900, 500)

        # --- edit state ---------------------------------------------------
        self._layers = []                 # [{name, path, exists, writable}], priority order
        self._current_layer_index = -1
        self._layer_name = None
        self._layer_path = None
        self._is_writable = False
        self._working_dict = {}           # deep-copied edit buffer for current layer
        self._dirty = False
        self._current_class = None
        self._current_is_ghost = False
        self._current_label_index = None

        self._populating = False          # guard against edit-signals while loading form
        self._suppress_layer_signal = False

        self._build_ui()
        self._rebuild_layers()
        if self._layers:
            self._load_layer(0)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        main_layout = QVBoxLayout(self)

        # Header: layer selector + writability badge
        header = QHBoxLayout()
        header.addWidget(QLabel("Editing layer:"))
        self.layer_combo = QComboBox()
        self.layer_combo.currentIndexChanged.connect(self._on_layer_changed)
        header.addWidget(self.layer_combo)
        self.writable_badge = QLabel("")
        header.addWidget(self.writable_badge)
        header.addStretch(1)
        main_layout.addLayout(header)

        # Body: three panes
        splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(splitter, 1)

        splitter.addWidget(self._build_class_pane())
        splitter.addWidget(self._build_label_pane())
        splitter.addWidget(self._build_editor_pane())
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 2)
        splitter.setStretchFactor(2, 3)

        # Footer
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self._on_save)
        self.revert_button = QPushButton("Revert")
        self.revert_button.clicked.connect(self._on_revert)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close)
        footer.addWidget(self.revert_button)
        footer.addWidget(self.save_button)
        footer.addWidget(self.close_button)
        main_layout.addLayout(footer)

    def _build_class_pane(self):
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Node classes"))

        self.class_list = QListWidget()
        self.class_list.currentItemChanged.connect(lambda *_: self._on_class_changed())
        self.class_list.itemDoubleClicked.connect(self._on_class_double_clicked)
        layout.addWidget(self.class_list, 1)

        toolbar = QHBoxLayout()
        self.add_class_button = QPushButton("Add Class...")
        self.add_class_button.clicked.connect(self._on_add_class)
        self.remove_class_button = QPushButton("Remove Class")
        self.remove_class_button.clicked.connect(self._on_remove_class)
        self.fork_button = QPushButton("Fork to Edit")
        self.fork_button.setToolTip(
            "Copy this inherited class definition into the layer you are editing "
            "so you can override it. Because configs replace whole classes, your "
            "copy will fully control this class."
        )
        self.fork_button.clicked.connect(self._on_fork)
        toolbar.addWidget(self.add_class_button)
        toolbar.addWidget(self.remove_class_button)
        toolbar.addWidget(self.fork_button)
        layout.addLayout(toolbar)
        return pane

    def _build_label_pane(self):
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Labels (top to bottom)"))

        self.label_list = QListWidget()
        self.label_list.currentItemChanged.connect(lambda *_: self._on_label_changed())
        layout.addWidget(self.label_list, 1)

        toolbar = QHBoxLayout()
        self.add_simple_button = QPushButton("Add Simple")
        self.add_simple_button.clicked.connect(lambda: self._on_add_label(tcl=False))
        self.add_tcl_button = QPushButton("Add TCL")
        self.add_tcl_button.clicked.connect(lambda: self._on_add_label(tcl=True))
        self.delete_label_button = QPushButton("Delete")
        self.delete_label_button.clicked.connect(self._on_delete_label)
        self.up_button = QPushButton("Up")
        self.up_button.clicked.connect(lambda: self._on_move_label(-1))
        self.down_button = QPushButton("Down")
        self.down_button.clicked.connect(lambda: self._on_move_label(1))
        for button in (
            self.add_simple_button,
            self.add_tcl_button,
            self.delete_label_button,
            self.up_button,
            self.down_button,
        ):
            toolbar.addWidget(button)
        layout.addLayout(toolbar)
        return pane

    def _build_editor_pane(self):
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Label definition"))

        self.editor_stack = QStackedWidget()
        layout.addWidget(self.editor_stack, 1)

        # Page 0: nothing selected
        placeholder = QLabel("Select a label to edit, or add a new one.")
        placeholder.setAlignment(Qt.AlignCenter)
        placeholder.setWordWrap(True)
        self.editor_stack.addWidget(placeholder)

        # Page 1: simple-label form
        simple_page = QWidget()
        form = QFormLayout(simple_page)
        self.name_edit = QLineEdit()
        self.name_edit.setToolTip("Internal Nuke knob name (required).")
        self.name_edit.editingFinished.connect(self._commit_simple_form)
        form.addRow("Knob name:", self.name_edit)

        self.label_edit = QLineEdit()
        self.label_edit.setToolTip("Display label shown in the DAG. Defaults to the knob name.")
        self.label_edit.editingFinished.connect(self._commit_simple_form)
        form.addRow("Display label:", self.label_edit)

        self.default_edit = QLineEdit()
        self.default_edit.setToolTip(
            "Skip this line when the knob is at this value. JSON syntax: 0, 1.5, "
            "true, [0, 0], \"text\". Leave blank to always show the value."
        )
        self.default_edit.editingFinished.connect(self._commit_simple_form)
        form.addRow("Default value:", self.default_edit)

        self.always_show_check = QCheckBox()
        self.always_show_check.setToolTip("Show this line even when the value matches the default.")
        self.always_show_check.stateChanged.connect(self._commit_simple_form)
        form.addRow("Always show:", self.always_show_check)

        self.disable_colorize_check = QCheckBox()
        self.disable_colorize_check.setToolTip(
            "Color/AColor knobs are shown with a colour swatch by default. Check this "
            "to turn the swatch off for this label. Has no effect on non-color knobs."
        )
        self.disable_colorize_check.stateChanged.connect(self._commit_simple_form)
        form.addRow("Disable colorization:", self.disable_colorize_check)

        self.editor_stack.addWidget(simple_page)

        # Page 2: TCL editor
        tcl_page = QWidget()
        tcl_layout = QVBoxLayout(tcl_page)
        tcl_layout.setContentsMargins(0, 0, 0, 0)
        tcl_layout.addWidget(
            QLabel("TCL string (evaluated in node context, e.g. in [value in]-->out [value out])")
        )
        self.tcl_edit = QPlainTextEdit()
        monospace = QFont("Courier")
        monospace.setStyleHint(QFont.Monospace)
        self.tcl_edit.setFont(monospace)
        self.tcl_edit.textChanged.connect(self._commit_tcl)
        tcl_layout.addWidget(self.tcl_edit, 1)
        self.editor_stack.addWidget(tcl_page)

        self._simple_page_index = 1
        self._tcl_page_index = 2
        return pane

    # -------------------------------------------------------------- layers
    def _rebuild_layers(self):
        """Recompute the ordered list of editable layers and repopulate the combo."""
        composed = labelmaker_config.composed_config_singleton
        layers = []
        seen_personal = False
        for config in composed.configs:
            layers.append(
                {
                    "name": config.name,
                    "path": config.path,
                    "exists": True,
                    "writable": _path_is_writable(config.path),
                }
            )
            if config.name == "personal":
                seen_personal = True

        if not seen_personal:
            personal_path = labelmaker_prefs.prefs_singleton.get(
                "personal_config_path"
            ) or os.path.join(os.path.expanduser("~"), ".nuke", "labelmaker_config.json")
            layers.append(
                {
                    "name": "personal",
                    "path": personal_path,
                    "exists": False,
                    "writable": _path_is_writable(personal_path),
                }
            )

        self._layers = layers
        self._suppress_layer_signal = True
        self.layer_combo.clear()
        for layer in layers:
            suffix = "" if layer["exists"] else "  (new — not yet saved)"
            self.layer_combo.addItem(layer["name"] + suffix)
        self._suppress_layer_signal = False

    def _load_layer(self, index):
        layer = self._layers[index]
        self._current_layer_index = index
        self._layer_name = layer["name"]
        self._layer_path = layer["path"]
        self._is_writable = layer["writable"]

        if layer["exists"]:
            source = labelmaker_config.composed_config_singleton.get_config_by_name(
                layer["name"]
            ).get_underlying_dict()
            self._working_dict = copy.deepcopy(source)
        else:
            self._working_dict = {}

        self._dirty = False
        self._current_class = None
        self._current_is_ghost = False
        self._current_label_index = None

        self._suppress_layer_signal = True
        self.layer_combo.setCurrentIndex(index)
        self._suppress_layer_signal = False

        self._update_writable_badge()
        self._refresh_class_list()
        self.label_list.clear()
        self.editor_stack.setCurrentIndex(0)
        self._update_editability()

    def _update_writable_badge(self):
        if self._is_writable:
            self.writable_badge.setText("writable")
            self.writable_badge.setStyleSheet("color: #6a6;")
        else:
            self.writable_badge.setText("READ-ONLY")
            self.writable_badge.setStyleSheet("color: #c66;")

    def _layer_classes(self, layer_name):
        try:
            config = labelmaker_config.composed_config_singleton.get_config_by_name(layer_name)
        except IndexError:
            return set()
        return set(config.get_underlying_dict().keys())

    def _inherited_classes(self):
        """Classes defined in a lower-priority layer but absent from the current
        edit buffer. Maps class name -> source layer name (highest below current)."""
        if not self._is_writable:
            return {}
        inherited = {}
        for index in range(self._current_layer_index):
            layer = self._layers[index]
            if not layer["exists"]:
                continue
            for class_name in self._layer_classes(layer["name"]):
                if class_name not in self._working_dict:
                    inherited[class_name] = layer["name"]
        return inherited

    def _effective_lower_def(self, class_name):
        """Deep copy of the class definition from the highest-priority layer below
        the current one that defines it, or None."""
        result = None
        for index in range(self._current_layer_index):
            layer = self._layers[index]
            if not layer["exists"]:
                continue
            data = labelmaker_config.composed_config_singleton.get_config_by_name(
                layer["name"]
            ).get_underlying_dict()
            if class_name in data:
                result = copy.deepcopy(data[class_name])
        return result

    def _class_annotation(self, class_name):
        """(suffix_text, is_dim) describing how this class relates to other layers."""
        higher = []
        lower = []
        for index, layer in enumerate(self._layers):
            if index == self._current_layer_index or not layer["exists"]:
                continue
            if class_name in self._layer_classes(layer["name"]):
                if index > self._current_layer_index:
                    higher.append(layer["name"])
                else:
                    lower.append(layer["name"])
        if higher:
            return "overridden by {}".format(", ".join(higher)), True
        if lower:
            return "overrides {}".format(", ".join(lower)), False
        return None, False

    # ---------------------------------------------------------- class list
    def _refresh_class_list(self, select_class=None):
        self.class_list.blockSignals(True)
        self.class_list.clear()

        for class_name in sorted(self._working_dict.keys()):
            annotation, is_dim = self._class_annotation(class_name)
            text = class_name if not annotation else "{}  ({})".format(class_name, annotation)
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, {"class": class_name, "ghost": False})
            if is_dim:
                item.setForeground(OVERRIDDEN_COLOR)
            self.class_list.addItem(item)

        for class_name, source in sorted(self._inherited_classes().items()):
            item = QListWidgetItem("{}  (inherited from {})".format(class_name, source))
            item.setData(Qt.UserRole, {"class": class_name, "ghost": True, "source": source})
            item.setForeground(GHOST_COLOR)
            self.class_list.addItem(item)

        self.class_list.blockSignals(False)

        if select_class is not None:
            for row in range(self.class_list.count()):
                data = self.class_list.item(row).data(Qt.UserRole)
                if data["class"] == select_class and not data["ghost"]:
                    self.class_list.setCurrentRow(row)
                    return
        self._on_class_changed()

    def _on_class_changed(self):
        item = self.class_list.currentItem()
        if item is None:
            self._current_class = None
            self._current_is_ghost = False
        else:
            data = item.data(Qt.UserRole)
            self._current_class = data["class"]
            self._current_is_ghost = data["ghost"]
        self._current_label_index = None
        self.editor_stack.setCurrentIndex(0)
        self._refresh_label_list()
        self._update_editability()

    def _on_class_double_clicked(self, item):
        data = item.data(Qt.UserRole)
        if data and data.get("ghost"):
            self._fork_class(data["class"])

    def _on_add_class(self):
        class_name = nuke.getInput("Node class name (e.g. Grade, Merge2):", "")
        if not class_name:
            return
        class_name = class_name.strip()
        if not class_name:
            return
        if class_name in self._working_dict:
            nuke.message("'{}' already exists in this layer.".format(class_name))
            self._refresh_class_list(select_class=class_name)
            return
        self._working_dict[class_name] = []
        self._mark_dirty()
        self._refresh_class_list(select_class=class_name)

    def _on_remove_class(self):
        if self._current_class is None or self._current_is_ghost:
            return
        confirm = QMessageBox.question(
            self,
            "Remove class",
            "Remove '{}' and all its labels from this layer?".format(self._current_class),
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        del self._working_dict[self._current_class]
        self._current_class = None
        self._mark_dirty()
        self._refresh_class_list()

    def _on_fork(self):
        if self._current_is_ghost and self._current_class is not None:
            self._fork_class(self._current_class)

    def _fork_class(self, class_name):
        definition = self._effective_lower_def(class_name)
        if definition is None:
            return
        self._working_dict[class_name] = definition
        self._mark_dirty()
        self._refresh_class_list(select_class=class_name)

    # ---------------------------------------------------------- label list
    def _current_labels(self):
        if self._current_class is None:
            return []
        if self._current_is_ghost:
            return self._effective_lower_def(self._current_class) or []
        return self._working_dict.get(self._current_class, [])

    def _refresh_label_list(self, select_index=None):
        self.label_list.blockSignals(True)
        self.label_list.clear()
        for label in self._current_labels():
            self.label_list.addItem(QListWidgetItem(self._label_summary(label)))
        self.label_list.blockSignals(False)
        if select_index is not None and 0 <= select_index < self.label_list.count():
            self.label_list.setCurrentRow(select_index)
        else:
            self._on_label_changed()

    def _refresh_label_row(self, index):
        if 0 <= index < self.label_list.count():
            self.label_list.item(index).setText(
                self._label_summary(self._current_labels()[index])
            )

    @staticmethod
    def _label_summary(label):
        if "tcl_string" in label:
            text = str(label.get("tcl_string", ""))
            if len(text) > 40:
                text = text[:40] + "…"
            return "[tcl] " + text
        name = str(label.get("name", "")) or "(unnamed)"
        extras = []
        if label.get("label"):
            extras.append('"{}"'.format(label["label"]))
        if label.get("always_show"):
            extras.append("always")
        return name + ("  " + " ".join(extras) if extras else "")

    def _on_label_changed(self):
        item = self.label_list.currentItem()
        if item is None:
            self._current_label_index = None
            self.editor_stack.setCurrentIndex(0)
            self._update_editability()
            return
        index = self.label_list.row(item)
        self._current_label_index = index
        label = self._current_labels()[index]
        self._populate_editor(label)
        self._update_editability()

    def _populate_editor(self, label):
        self._populating = True
        if "tcl_string" in label:
            self.editor_stack.setCurrentIndex(self._tcl_page_index)
            self.tcl_edit.setPlainText(str(label.get("tcl_string", "")))
        else:
            self.editor_stack.setCurrentIndex(self._simple_page_index)
            self.name_edit.setText(str(label.get("name", "")))
            self.label_edit.setText(str(label.get("label", "")))
            self.default_edit.setText(self._default_to_text(label))
            self.always_show_check.setChecked(bool(label.get("always_show", False)))
            self.disable_colorize_check.setChecked(not bool(label.get("colorize", True)))
        self._populating = False

    @staticmethod
    def _default_to_text(label):
        if "default" not in label:
            return ""
        try:
            return json.dumps(label["default"])
        except (TypeError, ValueError):
            return str(label["default"])

    def _on_add_label(self, tcl):
        if self._current_class is None or self._current_is_ghost or not self._is_writable:
            return
        new_label = {"tcl_string": ""} if tcl else {"name": ""}
        self._working_dict[self._current_class].append(new_label)
        self._mark_dirty()
        self._refresh_label_list(select_index=len(self._working_dict[self._current_class]) - 1)

    def _on_delete_label(self):
        if not self._can_edit_label():
            return
        del self._working_dict[self._current_class][self._current_label_index]
        self._mark_dirty()
        self._refresh_label_list()

    def _on_move_label(self, delta):
        if not self._can_edit_label():
            return
        labels = self._working_dict[self._current_class]
        old_index = self._current_label_index
        new_index = old_index + delta
        if not (0 <= new_index < len(labels)):
            return
        labels.insert(new_index, labels.pop(old_index))
        self._mark_dirty()
        self._refresh_label_list(select_index=new_index)

    def _can_edit_label(self):
        return (
            self._is_writable
            and self._current_class is not None
            and not self._current_is_ghost
            and self._current_label_index is not None
        )

    # ----------------------------------------------------------- committing
    def _commit_simple_form(self):
        if self._populating or not self._can_edit_label():
            return
        existing = self._working_dict[self._current_class][self._current_label_index]
        if "tcl_string" in existing:
            return  # form does not apply to a TCL label

        new_label = {"name": self.name_edit.text().strip()}
        display_label = self.label_edit.text().strip()
        if display_label:
            new_label["label"] = display_label
        default_text = self.default_edit.text().strip()
        if default_text:
            new_label["default"] = self._parse_default(default_text)
        if self.always_show_check.isChecked():
            new_label["always_show"] = True
        if self.disable_colorize_check.isChecked():
            new_label["colorize"] = False

        # preserve any unknown keys the user/facility added
        for key, value in existing.items():
            if key not in KNOWN_SIMPLE_KEYS:
                new_label[key] = value

        self._working_dict[self._current_class][self._current_label_index] = new_label
        self._mark_dirty()
        self._refresh_label_row(self._current_label_index)

    def _commit_tcl(self):
        if self._populating or not self._can_edit_label():
            return
        existing = self._working_dict[self._current_class][self._current_label_index]
        if "tcl_string" not in existing:
            return
        new_label = {"tcl_string": self.tcl_edit.toPlainText()}
        for key, value in existing.items():
            if key != "tcl_string":
                new_label[key] = value
        self._working_dict[self._current_class][self._current_label_index] = new_label
        self._mark_dirty()
        self._refresh_label_row(self._current_label_index)

    @staticmethod
    def _parse_default(text):
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return text

    # ------------------------------------------------------------ editability
    def _update_editability(self):
        writable = self._is_writable
        real_class = (
            writable and self._current_class is not None and not self._current_is_ghost
        )
        real_label = real_class and self._current_label_index is not None

        for widget in (
            self.name_edit,
            self.label_edit,
            self.default_edit,
            self.always_show_check,
            self.disable_colorize_check,
            self.tcl_edit,
        ):
            widget.setEnabled(real_label)

        self.add_simple_button.setEnabled(real_class)
        self.add_tcl_button.setEnabled(real_class)
        self.delete_label_button.setEnabled(real_label)
        self.up_button.setEnabled(real_label)
        self.down_button.setEnabled(real_label)

        self.add_class_button.setEnabled(writable)
        self.remove_class_button.setEnabled(real_class)
        self.fork_button.setEnabled(self._current_is_ghost)

        self.save_button.setEnabled(writable and self._dirty)

    def _mark_dirty(self):
        self._dirty = True
        self.save_button.setEnabled(self._is_writable and self._dirty)

    # ------------------------------------------------------------------ save
    def _validate(self):
        errors = []
        for class_name, labels in self._working_dict.items():
            if not isinstance(labels, list):
                errors.append("{}: entry is not a list of labels".format(class_name))
                continue
            for index, label in enumerate(labels):
                if "tcl_string" in label:
                    if not str(label.get("tcl_string", "")).strip():
                        errors.append("{} [{}]: TCL string is empty".format(class_name, index))
                elif not str(label.get("name", "")).strip():
                    errors.append("{} [{}]: simple label is missing a knob name".format(class_name, index))
        try:
            json.dumps(self._working_dict)
        except (TypeError, ValueError) as error:
            errors.append("config is not JSON-serializable: {}".format(error))
        return errors

    def _on_save(self):
        if not self._is_writable:
            return
        errors = self._validate()
        if errors:
            nuke.message("Cannot save — fix these first:\n\n" + "\n".join(errors))
            return
        try:
            directory = os.path.dirname(self._layer_path)
            if directory and not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
            with open(self._layer_path, "w") as config_file:
                json.dump(self._working_dict, config_file, indent=2)
        except OSError as error:
            nuke.message("Failed to write config file:\n{}".format(error))
            return

        labelmaker_config.reload_composed_config()
        labelmaker.autolabeller_singleton.config = (
            labelmaker_config.composed_config_singleton
        )

        saved_layer_name = self._layer_name
        saved_class = self._current_class
        self._dirty = False
        self._rebuild_layers()
        target_index = next(
            (i for i, layer in enumerate(self._layers) if layer["name"] == saved_layer_name),
            0,
        )
        self._load_layer(target_index)
        if saved_class is not None:
            self._refresh_class_list(select_class=saved_class)

    def _on_revert(self):
        if not self._dirty:
            return
        confirm = QMessageBox.question(
            self,
            "Revert changes",
            "Discard unsaved changes to '{}'?".format(self._layer_name),
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm == QMessageBox.Yes:
            self._load_layer(self._current_layer_index)

    # ------------------------------------------------------- unsaved guard
    def _confirm_discard(self):
        """Return True if it is OK to abandon the current edit buffer."""
        if not self._dirty:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("Unsaved changes")
        box.setText("You have unsaved changes to '{}'.".format(self._layer_name))
        box.setStandardButtons(
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel
        )
        choice = box.exec()
        if choice == QMessageBox.Cancel:
            return False
        if choice == QMessageBox.Save:
            before = self._dirty
            self._on_save()
            # if validation failed, _on_save leaves us dirty: treat as cancel
            return not (before and self._dirty)
        return True  # Discard

    def _on_layer_changed(self, index):
        if self._suppress_layer_signal or index < 0:
            return
        if index == self._current_layer_index:
            return
        if not self._confirm_discard():
            self._suppress_layer_signal = True
            self.layer_combo.setCurrentIndex(self._current_layer_index)
            self._suppress_layer_signal = False
            return
        self._load_layer(index)

    def closeEvent(self, event):
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()


_editor_window = None


def show_config_editor():
    global _editor_window
    if _editor_window is None:
        _editor_window = LabelmakerConfigEditor(parent=_nuke_main_window())
    _editor_window.show()
    _editor_window.raise_()
    _editor_window.activateWindow()
