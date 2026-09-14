"""The label cache in front of the autolabel build.

Nuke asks for a label on a real knob change (one or two requests on their
own) or in a whole-script pass (every node, back to back). Every *changed*
string handed back costs Nuke a stall, so bursts are answered from the cache,
changed strings are held back while the user interacts, and stale nodes are
refreshed (by poking dope_sheet) once the traffic goes quiet.
"""
import nuke
import pytest
from stubs import StubKnob, StubNode

import labelmaker
from labelmaker import AutolabelReplacement


class _EmptyConfig:
    def get(self, key, default=None):
        return default


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class _FakeTimer:
    def __init__(self):
        self.interval = None
        self.callback = None
        self.timeout = self

    def connect(self, fn):
        self.callback = fn

    def setSingleShot(self, value):
        pass

    def start(self, interval):
        self.interval = interval

    def fire(self):
        self.callback()


class _RecordingKnob(StubKnob):
    def __init__(self, name, value=None):
        super().__init__(name, value)
        self.sets = []

    def setValue(self, value):
        self.sets.append(value)
        super().setValue(value)


def _node(name):
    return StubNode("Grade", knobs={"name": StubKnob("name", name), "dope_sheet": _RecordingKnob("dope_sheet", False)})


def _unpokeable_node(name):
    return StubNode("Viewer", knobs={"name": StubKnob("name", name)})


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(labelmaker.time, "perf_counter", clock)
    return clock


@pytest.fixture
def labeller(monkeypatch, clock):
    labeller = AutolabelReplacement(_EmptyConfig())
    labeller._refresh_timer = _FakeTimer()
    labeller._refresh_timer.connect(labeller._refresh_stale_labels)
    labeller.texts = {}      # {node name: text the build returns}
    labeller.builds = []     # names built, in order
    labeller.nodes = {}

    def build():
        name = nuke.thisNode().name()
        labeller.builds.append(name)
        labeller.indicators = 0
        labeller.node_label_raw = ""
        return labeller.texts[name]

    monkeypatch.setattr(labeller, "_build_label", build)
    monkeypatch.setattr(nuke, "toNode", lambda name: labeller.nodes.get(name))
    return labeller


def request(labeller, clock, name, text=None, advance=1.0):
    """Nuke asking for `name`'s label `advance` seconds after the last request."""
    clock.now += advance
    if text is not None:
        labeller.texts[name] = text
    node = labeller.nodes.setdefault(name, _node(name))
    nuke.thisNode = lambda: node
    return labeller.create_autolabel()


def whole_script_pass(labeller, clock, names):
    """Every node requested back to back, as after a viewer input change."""
    return [request(labeller, clock, name, advance=1.0 if i == 0 else 0.0001) for i, name in enumerate(names)]


# --- lone requests ---


def test_first_request_builds_and_shows(labeller, clock):
    assert request(labeller, clock, "Grade1", "gain 1.0") == "gain 1.0"
    assert labeller.builds == ["Grade1"]


def test_unchanged_text_is_rebuilt_and_shown(labeller, clock):
    request(labeller, clock, "Grade1", "gain 1.0")
    assert request(labeller, clock, "Grade1", "gain 1.0") == "gain 1.0"
    assert labeller.builds == ["Grade1", "Grade1"]


def test_changed_text_is_held_back_and_node_marked_stale(labeller, clock):
    request(labeller, clock, "Grade1", "gain 1.0")
    assert request(labeller, clock, "Grade1", "gain 1.5") == "gain 1.0"
    assert "Grade1" in labeller._stale
    assert labeller._refresh_timer.interval == 400


def test_refresh_pokes_stale_node_and_releases_new_text(labeller, clock):
    request(labeller, clock, "Grade1", "gain 1.0")
    request(labeller, clock, "Grade1", "gain 1.5")
    knob = labeller.nodes["Grade1"]["dope_sheet"]
    clock.now += 1.0
    labeller._refresh_timer.fire()
    assert knob.sets == [True, False]
    assert "Grade1" in labeller._forced
    assert labeller._stale == set()
    # the stub cannot emit the relabel request a poke causes in Nuke, so the
    # tests simulate it with a lone request
    assert request(labeller, clock, "Grade1", "gain 1.5", advance=0.01) == "gain 1.5"
    assert "Grade1" not in labeller._forced


def test_refresh_waits_while_traffic_continues(labeller, clock):
    request(labeller, clock, "Grade1", "gain 1.0")
    request(labeller, clock, "Grade1", "gain 1.5")
    labeller._refresh_timer.interval = None
    clock.now += 0.05
    labeller._refresh_timer.fire()
    assert labeller.nodes["Grade1"]["dope_sheet"].sets == []
    assert "Grade1" in labeller._stale
    assert labeller._refresh_timer.interval == 400


def test_slider_drag_changes_text_once_at_the_end(labeller, clock):
    request(labeller, clock, "Grade1", "gain 1.0")
    shown = {request(labeller, clock, "Grade1", "gain {}".format(i), advance=0.016) for i in range(40)}
    assert shown == {"gain 1.0"}
    clock.now += 1.0
    labeller._refresh_timer.fire()
    assert request(labeller, clock, "Grade1", "gain 39", advance=0.01) == "gain 39"


# --- bursts ---


def test_whole_script_pass_is_served_from_cache(labeller, clock):
    names = ["Grade{}".format(i) for i in range(30)]
    for name in names:
        request(labeller, clock, name, "text " + name)
    labeller.builds = []
    assert whole_script_pass(labeller, clock, names) == ["text " + name for name in names]
    assert len(labeller.builds) == labelmaker.LABEL_BURST_MIN


def test_uncached_nodes_in_a_pass_are_built(labeller, clock):
    names = ["Grade{}".format(i) for i in range(30)]
    for name in names[:20]:
        request(labeller, clock, name, "old")
    for name in names[20:]:
        labeller.texts[name] = "new"
    labeller.builds = []
    whole_script_pass(labeller, clock, names)
    assert set(names[20:]) <= set(labeller.builds)


def test_small_burst_is_a_real_edit_and_gets_refreshed(labeller, clock):
    names = ["Grade{}".format(i) for i in range(20)]
    for name in names:
        request(labeller, clock, name, "old")
    for name in names:
        labeller.texts[name] = "new"
    assert set(whole_script_pass(labeller, clock, names)) == {"old"}
    request(labeller, clock, "Other", "x")
    assert set(names) <= labeller._stale
    clock.now += 1.0
    labeller._refresh_timer.fire()
    assert all(request(labeller, clock, name, advance=0.01) == "new" for name in names)


def test_large_burst_is_not_marked_stale(labeller, clock):
    names = ["Grade{}".format(i) for i in range(labelmaker.LABEL_BURST_GENUINE_MAX + 50)]
    for name in names:
        request(labeller, clock, name, "old")
    whole_script_pass(labeller, clock, names)
    request(labeller, clock, "Other", "x")
    assert labeller._stale == set()


def test_frame_dependent_node_on_new_frame_is_held_then_refreshed(labeller, clock, monkeypatch):
    names = ["Grade{}".format(i) for i in range(20)]
    for name in names:
        request(labeller, clock, name, "old")
    labeller._content["Grade15"] = (1, "old")
    monkeypatch.setattr(nuke, "frame", lambda: 2)
    labeller.builds = []
    whole_script_pass(labeller, clock, names)
    assert "Grade15" not in labeller.builds
    assert "Grade15" in labeller._stale


def test_tcl_in_the_label_knob_is_cached_per_frame(clock, monkeypatch):
    labeller = AutolabelReplacement(_EmptyConfig())
    monkeypatch.setattr(nuke, "value", lambda path, default="": "[frame]" if path == "this.label" else default)
    monkeypatch.setattr(nuke, "tcl", lambda *args: "1001")
    monkeypatch.setattr(nuke, "frame", lambda: 1001)
    nuke.thisNode = lambda: _node("Grade1")
    assert labeller.create_autolabel() == "Grade1\n1001"
    assert labeller._content["Grade1"] == (1001, "Grade1\n1001")
    assert labeller.node_label_raw == "[frame]"


# --- nodes that cannot be poked ---


def test_node_without_dope_sheet_shows_changed_text_immediately(labeller, clock):
    labeller.nodes["Viewer1"] = _unpokeable_node("Viewer1")
    request(labeller, clock, "Viewer1", "input 1")
    assert request(labeller, clock, "Viewer1", "input 2") == "input 2"
    assert labeller._stale == set()


def test_node_without_dope_sheet_is_rebuilt_and_shown_in_a_burst(labeller, clock):
    names = ["Grade{}".format(i) for i in range(20)] + ["Viewer1"]
    labeller.nodes["Viewer1"] = _unpokeable_node("Viewer1")
    for name in names:
        request(labeller, clock, name, "old")
    for name in names:
        labeller.texts[name] = "new"
    assert whole_script_pass(labeller, clock, names)[-1] == "new"
    request(labeller, clock, "Other", "x")
    assert "Viewer1" not in labeller._stale
    assert set(names[:-1]) <= labeller._stale


# --- invalidation ---


def test_destroyed_node_forgets_its_label(labeller, clock):
    request(labeller, clock, "Grade1", "gain 1.0")
    nuke.thisNode = lambda: labeller.nodes["Grade1"]
    labeller._on_node_destroyed()
    assert "Grade1" not in labeller._content and "Grade1" not in labeller._shown
    assert request(labeller, clock, "Grade1", "gain 2.0") == "gain 2.0"


def test_created_node_forgets_entries_left_under_its_name(labeller, clock):
    request(labeller, clock, "Grade1", "gain 1.0")
    labeller._stale.add("Grade1")
    nuke.thisNode = lambda: _node("Grade1")
    labeller._on_node_created()
    assert "Grade1" not in labeller._content and "Grade1" not in labeller._shown
    assert "Grade1" not in labeller._stale
    assert request(labeller, clock, "Grade1", "gain 2.0") == "gain 2.0"


def test_register_hooks_node_creation_and_destruction(labeller, monkeypatch):
    hooks = {}
    for name in ("addOnCreate", "addOnDestroy", "removeOnCreate", "removeOnDestroy"):
        monkeypatch.setattr(nuke, name, lambda fn, name=name: hooks.__setitem__(name, fn))
    labeller.register_autolabel()
    assert hooks["addOnCreate"] == labeller._on_node_created
    assert hooks["addOnDestroy"] == labeller._on_node_destroyed
    labeller.unregister_autolabel()
    assert hooks["removeOnCreate"] == labeller._on_node_created
    assert hooks["removeOnDestroy"] == labeller._on_node_destroyed


def test_setting_config_invalidates_cache(labeller, clock):
    request(labeller, clock, "Grade1", "gain 1.0")
    labeller.config = _EmptyConfig()
    assert labeller._content == {} and labeller._shown == {}


def test_refresh_all_labels_pokes_every_node(labeller, clock, monkeypatch):
    request(labeller, clock, "Grade1", "gain 1.0")
    monkeypatch.setattr(nuke, "allNodes", lambda recurseGroups=False: list(labeller.nodes.values()))
    labeller.refresh_all_labels()
    assert labeller._content == {}
    assert "Grade1" in labeller._forced


def test_poke_skips_nodes_without_dope_sheet(labeller, clock):
    labeller.nodes["Viewer1"] = _unpokeable_node("Viewer1")
    labeller._poke_nodes(["Viewer1", "Missing"])
    assert labeller._forced == set()


def test_disabling_pokes_every_node_without_forcing(labeller, clock, monkeypatch):
    request(labeller, clock, "Grade1", "gain 1.0")
    monkeypatch.setattr(nuke, "allNodes", lambda recurseGroups=False: list(labeller.nodes.values()))
    labeller.set_enabled(False)
    assert labeller.nodes["Grade1"]["dope_sheet"].sets == [True, False]
    assert labeller._forced == set()
    assert labeller._content == {}


def test_enabling_invalidates_and_forces_every_node(labeller, clock, monkeypatch):
    request(labeller, clock, "Grade1", "gain 1.0")
    monkeypatch.setattr(nuke, "allNodes", lambda recurseGroups=False: list(labeller.nodes.values()))
    labeller.set_enabled(True)
    assert labeller._content == {}
    assert "Grade1" in labeller._forced
