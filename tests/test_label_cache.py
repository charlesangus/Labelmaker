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

    labeller.verified = []   # names verified in the background, in order

    def build():
        name = nuke.thisNode().name()
        labeller.builds.append(name)
        return labeller.texts[name]

    def compose():
        name = nuke.thisNode().name()
        labeller.verified.append(name)
        return labeller.texts[name]

    def run_in(name, code):
        # nuke.runIn: evaluate `code` with `name` as the current node
        nuke.thisNode = lambda: labeller.nodes[name]
        eval(code)

    monkeypatch.setattr(labeller, "_build_label", build)
    monkeypatch.setattr(labeller, "_compose_label", compose)
    monkeypatch.setattr(nuke, "toNode", lambda name: labeller.nodes.get(name))
    monkeypatch.setattr(nuke, "runIn", run_in, raising=False)
    labeller._verify_timer = _FakeTimer()
    labeller._verify_timer.connect(labeller._verify_slice)
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
    """Every node requested back to back (a viewer change, a frame step after
    an edit, or a tool editing many nodes: the cache cannot tell)."""
    return [request(labeller, clock, name, advance=1.0 if i == 0 else 0.0001) for i, name in enumerate(names)]


def go_idle(labeller, clock):
    """Label traffic stops: the refresh fires, verification runs to the end
    (the frozen clock never exhausts a slice) and stale labels are poked."""
    clock.now += 1.0
    labeller._refresh_timer.fire()
    while labeller._verify:
        labeller._verify_timer.fire()


def pokes(labeller):
    return {name for name, node in labeller.nodes.items() if node["dope_sheet"].sets}


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


def test_cache_answered_labels_are_verified_when_idle(labeller, clock):
    names = ["Grade{}".format(i) for i in range(30)]
    for name in names:
        request(labeller, clock, name, "text " + name)
    whole_script_pass(labeller, clock, names)
    request(labeller, clock, "Other", "x")
    assert labeller._verify == set(names[labelmaker.LABEL_BURST_MIN:])
    go_idle(labeller, clock)
    assert set(labeller.verified) == set(names[labelmaker.LABEL_BURST_MIN:])
    assert labeller._verify == set()


def test_unchanged_labels_are_not_poked_after_verification(labeller, clock):
    names = ["Grade{}".format(i) for i in range(30)]
    for name in names:
        request(labeller, clock, name, "same")
    whole_script_pass(labeller, clock, names)
    go_idle(labeller, clock)
    assert labeller._stale == set()
    assert pokes(labeller) == set()


@pytest.mark.parametrize("count", [20, 250, 1000])
def test_bulk_edit_of_any_size_is_served_old_then_verified_and_released(labeller, clock, count):
    """A tool setting a knob on every selected node relabels them in one
    burst that looks exactly like a whole-script pass."""
    names = ["Grade{}".format(i) for i in range(count)]
    for name in names:
        request(labeller, clock, name, "old")
    for name in names:
        labeller.texts[name] = "new"
    served = whole_script_pass(labeller, clock, names)
    assert set(served[labelmaker.LABEL_BURST_MIN:]) == {"old"}
    go_idle(labeller, clock)
    assert pokes(labeller) >= set(names[labelmaker.LABEL_BURST_MIN:])
    assert all(request(labeller, clock, name, advance=0.01) == "new" for name in names)
    assert labeller._stale == set() and labeller._verify == set()


def test_only_the_labels_that_changed_are_poked(labeller, clock):
    names = ["Grade{}".format(i) for i in range(40)]
    for name in names:
        request(labeller, clock, name, "old")
    labeller.texts["Grade20"] = "new"
    labeller.texts["Grade30"] = "new"
    whole_script_pass(labeller, clock, names)
    go_idle(labeller, clock)
    assert pokes(labeller) == {"Grade20", "Grade30"}
    assert request(labeller, clock, "Grade20") == "new"


def test_verification_runs_in_slices_and_yields_between_them(labeller, clock, monkeypatch):
    names = ["Grade{}".format(i) for i in range(30)]
    for name in names:
        request(labeller, clock, name, "old")
    whole_script_pass(labeller, clock, names)
    compose = labeller._compose_label

    def slow_compose():
        clock.now += labelmaker.LABEL_VERIFY_SLICE_S  # each label exhausts the slice
        return compose()

    monkeypatch.setattr(labeller, "_compose_label", slow_compose)
    clock.now += 1.0
    labeller._refresh_timer.fire()
    assert len(labeller.verified) == 1
    assert labeller._verify_timer.interval == labelmaker.LABEL_VERIFY_GAP_MS
    labeller._verify_timer.fire()
    assert len(labeller.verified) == 2


def test_verification_backs_off_while_label_traffic_resumes(labeller, clock):
    names = ["Grade{}".format(i) for i in range(30)]
    for name in names:
        request(labeller, clock, name, "old")
    whole_script_pass(labeller, clock, names)
    clock.now += 1.0
    labeller._refresh_timer.fire()  # verification done (frozen clock, one slice)
    assert labeller._verify == set()
    whole_script_pass(labeller, clock, names)
    request(labeller, clock, "Other", "x", advance=0.1)  # traffic 0.1 s ago
    labeller.verified = []
    labeller._verify_timer.fire()
    assert labeller.verified == []           # nothing verified while busy
    assert labeller._refresh_timer.interval  # waits for the traffic to end
    go_idle(labeller, clock)
    assert labeller._verify == set() and len(labeller.verified) == 30 - labelmaker.LABEL_BURST_MIN


def test_frame_dependent_labels_are_verified_first_and_released_early(labeller, clock, monkeypatch):
    names = ["Grade{}".format(i) for i in range(40)]
    for name in names:
        request(labeller, clock, name, "old")
    labeller._frame_dep.update({"Grade20", "Grade30"})   # keys/expressions/[tcl] last time
    labeller.texts["Grade20"] = "new"
    labeller.texts["Grade9"] = "new"
    whole_script_pass(labeller, clock, names)
    request(labeller, clock, "Other", "x")   # closes the burst
    assert labeller._verify_first == {"Grade20", "Grade30"}
    compose = labeller._compose_label

    def slow_compose():
        clock.now += labelmaker.LABEL_VERIFY_SLICE_S  # one label per slice
        return compose()

    monkeypatch.setattr(labeller, "_compose_label", slow_compose)
    clock.now += 1.0
    labeller._refresh_timer.fire()
    labeller._verify_timer.fire()
    assert set(labeller.verified) == {"Grade20", "Grade30"}   # first two slices
    assert pokes(labeller) == {"Grade20"}                      # released before the rest
    while labeller._verify:
        labeller._verify_timer.fire()
    assert pokes(labeller) == {"Grade20", "Grade9"}


def test_frame_dependence_is_noted_from_the_build(clock, monkeypatch):
    labeller = AutolabelReplacement(_EmptyConfig())
    monkeypatch.setattr(nuke, "expression", lambda expr: 1.0)   # "keys" bit
    nuke.thisNode = lambda: _node("Grade1")
    labeller.create_autolabel()
    assert "Grade1" in labeller._frame_dep
    monkeypatch.setattr(nuke, "expression", lambda expr: 0.0)
    clock.now += 1.0
    labeller.create_autolabel()
    assert "Grade1" not in labeller._frame_dep


def test_node_deleted_before_verification_is_skipped(labeller, clock):
    names = ["Grade{}".format(i) for i in range(30)]
    for name in names:
        request(labeller, clock, name, "old")
    whole_script_pass(labeller, clock, names)
    del labeller.nodes["Grade20"]
    go_idle(labeller, clock)
    assert "Grade20" not in labeller.verified
    assert "Grade20" not in labeller._stale


def test_real_request_during_verification_drops_the_node_from_the_queue(labeller, clock):
    names = ["Grade{}".format(i) for i in range(30)]
    for name in names:
        request(labeller, clock, name, "old")
    whole_script_pass(labeller, clock, names)
    request(labeller, clock, "Grade20", "new")   # lone edit: built and held back
    assert "Grade20" not in labeller._verify
    assert "Grade20" in labeller._stale


def test_tcl_in_the_label_knob_is_composed_in_node_context(clock, monkeypatch):
    labeller = AutolabelReplacement(_EmptyConfig())
    monkeypatch.setattr(nuke, "value", lambda path, default="": "[frame]" if path == "this.label" else default)
    monkeypatch.setattr(nuke, "tcl", lambda *args: "1001")
    nuke.thisNode = lambda: _node("Grade1")
    assert labeller.create_autolabel() == "Grade1\n1001"
    assert labeller._content["Grade1"] == "Grade1\n1001"


def test_compose_in_context_runs_the_label_code_with_the_node_as_context(clock, monkeypatch):
    labeller = AutolabelReplacement(_EmptyConfig())
    node = _node("Grade1")
    seen = []

    def run_in(name, code):
        seen.append(name)
        nuke.thisNode = lambda: node
        eval(code)

    monkeypatch.setattr(nuke, "runIn", run_in, raising=False)
    monkeypatch.setattr(nuke, "toNode", lambda name: node if name == "Grade1" else None)
    assert labeller._compose_in_context("Grade1") == "Grade1"
    assert seen == ["Grade1"]
    assert labeller._compose_in_context("Gone") is None


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
    assert "Viewer1" not in labeller._verify
    assert set(names[labelmaker.LABEL_BURST_MIN:-1]) <= labeller._verify


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
