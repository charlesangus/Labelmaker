import sys
import types

import pytest

# Install stubs before any plugin modules are collected or imported.


class _StubUndoManager:
    def disable(self):
        pass

    def enable(self):
        pass


class _StubMenu:
    def items(self):
        return []

    def name(self):
        return ""


class _StubMenuItem:
    pass


_nuke_stub = types.ModuleType("nuke")
_nuke_stub.warning = lambda msg: None
_nuke_stub.addAutolabel = lambda fn: None
_nuke_stub.removeAutolabel = lambda fn: None
_nuke_stub.allNodes = lambda: []
_nuke_stub.thisNode = lambda: None
_nuke_stub.expression = lambda expr: 0
_nuke_stub.numvalue = lambda knob, default=0: default
_nuke_stub.knob = lambda path, value=None: None
_nuke_stub.value = lambda path, default="": default
_nuke_stub.tcl = lambda *args: ""
_nuke_stub.Undo = _StubUndoManager()
_nuke_stub.Menu = _StubMenu
_nuke_stub.MenuItem = _StubMenuItem
_nuke_stub.toolbar = lambda name: _StubMenu()

sys.modules["nuke"] = _nuke_stub
sys.modules["nukescripts"] = types.ModuleType("nukescripts")

# Minimal PySide6 stubs so GUI module imports don't crash at collection time.
_pyside6 = types.ModuleType("PySide6")
_qtwidgets = types.ModuleType("PySide6.QtWidgets")
_qtcore = types.ModuleType("PySide6.QtCore")
_qtgui = types.ModuleType("PySide6.QtGui")

for _cls_name in [
    "QDialog",
    "QWidget",
    "QVBoxLayout",
    "QHBoxLayout",
    "QFormLayout",
    "QLabel",
    "QPushButton",
    "QCheckBox",
    "QLineEdit",
    "QTreeWidget",
    "QTreeWidgetItem",
    "QSplitter",
    "QFrame",
    "QApplication",
    "QMessageBox",
    "QFileDialog",
    "QAction",
    "QMenu",
    "QToolBar",
    "QGroupBox",
    "QDialogButtonBox",
    "QListWidget",
    "QListWidgetItem",
    "QStackedWidget",
    "QTabWidget",
    "QScrollArea",
    "QSizePolicy",
    "QAbstractItemView",
    "QComboBox",
]:
    setattr(_qtwidgets, _cls_name, type(_cls_name, (), {"__init__": lambda self, *a, **kw: None}))


class _StubQTimer:
    class _Signal:
        def connect(self, fn):
            pass

    def __init__(self):
        self.timeout = self._Signal()

    def setSingleShot(self, value):
        pass

    def setInterval(self, value):
        pass

    def start(self):
        pass


_qtcore.QTimer = _StubQTimer
_qtcore.Qt = types.SimpleNamespace(
    Horizontal=1,
    Vertical=2,
    AlignCenter=4,
    AlignLeft=1,
    AlignTop=32,
    MatchExactly=0,
    ItemIsEditable=2,
    ItemIsEnabled=32,
    ItemIsSelectable=1,
)
_qtcore.Signal = lambda *args: None
_qtcore.QObject = type("QObject", (), {"__init__": lambda self, *a, **kw: None})

for _cls_name in ["QFont", "QIcon", "QColor", "QPixmap"]:
    setattr(_qtgui, _cls_name, type(_cls_name, (), {"__init__": lambda self, *a, **kw: None}))

_pyside6.QtWidgets = _qtwidgets
_pyside6.QtCore = _qtcore
_pyside6.QtGui = _qtgui

sys.modules["PySide6"] = _pyside6
sys.modules["PySide6.QtWidgets"] = _qtwidgets
sys.modules["PySide6.QtCore"] = _qtcore
sys.modules["PySide6.QtGui"] = _qtgui


@pytest.fixture
def tmp_prefs_file(tmp_path):
    return str(tmp_path / "labelmaker_prefs.json")


@pytest.fixture
def tmp_config_file(tmp_path):
    return str(tmp_path / "labelmaker_config.json")
