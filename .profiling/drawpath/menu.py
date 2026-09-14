"""Draw-path profiling session: let Nuke's own DAG drawing drive the autolabel.

Driven by .profiling/run_drawpath.sh. Unlike bootstrap/menu.py (which calls the
autolabel by hand with nuke.runIn), every step here is a separate QTimer
callback, so control returns to Nuke's real exec() loop between steps. That is
what makes Nuke compute node labels under Xvfb: pumping processEvents() inside
one callback never lets the label pass run (the reason the old "diag" scenario
saw zero calls).

  LM_PROFILE_SCRIPT     .nk to load
  LM_PROFILE_MODE       on (Labelmaker registered) | off (stock autolabel)
  LM_PROFILE_DEOVERLAP  1 to leave auto-deoverlap enabled (default 0)
  LM_PROFILE_OUT        cProfile .prof for the script-open label pass (mode on)
"""
import cProfile
import faulthandler
import os
import pstats
import sys
import time

import nuke
from PySide6 import QtCore, QtWidgets

SCRIPT = os.environ.get("LM_PROFILE_SCRIPT", "")
MODE = os.environ.get("LM_PROFILE_MODE", "on")
DEOVERLAP = os.environ.get("LM_PROFILE_DEOVERLAP", "0") == "1"
PROF_OUT = os.environ.get("LM_PROFILE_OUT", "")
QUIET_MS = 2500     # call count unchanged for this long == "settled"
SETTLE_CAP_S = 60

repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo)
nuke.pluginAddPath(repo)

import labelmaker  # noqa: E402
import labelmaker_prefs  # noqa: E402

labelmaker_prefs.prefs_singleton._prefs["deoverlap_enabled"] = DEOVERLAP
autolabeller = labelmaker.autolabeller_singleton
autolabeller.set_enabled(MODE != "off")

# Wrap whichever label function is live (Labelmaker's, or Nuke's stock
# plugins/autolabel.py — also Python) so both modes report the same two
# numbers: how many times Nuke asked for a label, and the time spent in Python
# answering. Nuke calls the most recently registered autolabel first and stops
# at the first non-None result.
counter = {"calls": 0, "seconds": 0.0}
by_class = {}   # {class: [calls, seconds]} for the wrapped label function
import autolabel as _stock  # noqa: E402

nuke.removeAutolabel(_stock.autolabel)
nuke.removeAutolabel(autolabeller.create_autolabel)
autolabeller.set_enabled(True)
nuke.removeAutolabel(autolabeller.create_autolabel)
_stock_target = _stock.autolabel
_lm_target = autolabeller.create_autolabel
if MODE == "off":
    _target = _stock.autolabel
    if os.environ.get("LM_PROFILE_STOCK_VALUE") == "1":
        # stock label plus one knob value: isolates "label text changes on
        # every slider move" from anything Labelmaker-specific
        def _stock_plus_value():
            text = _stock.autolabel()
            return "{}\ngain {:.3f}".format(text, float(nuke.numvalue("this.white", 1.0)))
        _target = _stock_plus_value
else:
    _target = autolabeller.create_autolabel

# PROTOTYPE "proto" mode (LM_PROFILE_MODE=proto): what Labelmaker's caching
# should look like, in front of the unmodified create_autolabel. No Nuke
# callbacks at all (knobChanged fires per node per pointer move when dragging
# a selection: 123k times for one drag of 3k nodes).
#  * burst detection: Nuke's whole-script relabels (after a viewer input
#    change, a callback registration, or any knob change followed by a frame
#    step) arrive as thousands of requests <5 ms apart in one event-loop
#    iteration. Inside a burst every node with a cached label is answered from
#    the cache. A genuine edit is 1-2 requests on their own -> rebuilt.
#  * text-change hold: every label TEXT change costs Nuke an O(script) stall
#    (~70 ms at 3k, ~200 ms at 10k). After a change the node keeps its old
#    text for `window` (>= 400 ms, 5x the measured stall).
#  * idle refresh: once label traffic has been quiet for a window, every node
#    served stale gets a dope_sheet change-and-revert (one relabel, no undo,
#    no visible effect) so labels settle after the user stops - never during.
if True:
    _build = autolabeller.create_autolabel
    content = {}        # name -> (frame or None, text) from the last real build
    shown = {}          # name -> text Nuke was last given
    changed_at = {}     # name -> perf_counter when shown[name] last changed
    forced = set()      # names whose next request must rebuild (refresh)
    stale = set()       # names currently shown stale
    stall = {"t": None, "ema": 0.05}
    burst = {"t": 0.0, "n": 0, "names": []}
    BURST_GAP = 0.005           # requests closer than this are one pass
    BURST_MIN = 8               # requests before a pass counts as a burst
    BURST_GENUINE_MAX = 200     # a burst this small is a real multi-node edit
    cache_stats = {"hits": 0, "builds": 0, "held": 0, "pokes": 0}
    refresh_timer = QtCore.QTimer()
    refresh_timer.setSingleShot(True)

    def _window():
        return min(1.5, max(0.4, 5.0 * stall["ema"]))

    def _arm_refresh():
        refresh_timer.start(int(_window() * 1000))

    def _refresh():
        if time.perf_counter() - burst["t"] < _window() * 0.9:
            _arm_refresh()      # still busy: wait for the traffic to end
            return
        names = list(stale)
        stale.clear()
        nuke.Undo.disable()
        try:
            for name in names:
                node = nuke.toNode(name)
                if node is None:
                    continue
                k = node.knob("dope_sheet")
                if k is None:
                    continue
                forced.add(name)
                v = k.value()
                k.setValue(not v)
                k.setValue(v)
                cache_stats["pokes"] += 1
        finally:
            nuke.Undo.enable()

    refresh_timer.timeout.connect(_refresh)

    def _proto_label():
        now = time.perf_counter()
        if stall["t"] is not None:
            gap = now - stall["t"]
            stall["t"] = None
            if gap < 1.0:
                stall["ema"] = 0.7 * stall["ema"] + 0.3 * gap
        # burst bookkeeping
        if now - burst["t"] > BURST_GAP:
            if BURST_MIN < burst["n"] <= BURST_GENUINE_MAX:
                stale.update(burst["names"])    # small burst = real multi-node edit
                _arm_refresh()
            burst["n"], burst["names"] = 0, []
        burst["t"] = now
        burst["n"] += 1
        in_burst = burst["n"] > BURST_MIN
        node = nuke.thisNode()
        name = node.name()
        hit = content.get(name)
        if hit is not None and name not in forced:
            frame, text = hit
            if in_burst:
                burst["names"].append(name)
                if frame is not None and frame != nuke.frame():
                    stale.add(name)     # frame-dependent, shown for an old frame
                    _arm_refresh()
                cache_stats["hits"] += 1
                return shown.get(name, text)
        was_forced = name in forced
        forced.discard(name)
        text = _build()
        cache_stats["builds"] += 1
        ind = int(nuke.numvalue("this.indicators", 0))
        frame_dep = bool(ind & 3) or "[" in (nuke.value("this.label", "") or "")
        content[name] = (nuke.frame() if frame_dep else None, text)
        previous = shown.get(name)
        if previous is not None and text != previous and not was_forced:
            # a changed string costs Nuke an O(script) stall: keep showing the
            # old one and release the new one from the idle refresh
            cache_stats["held"] += 1
            stale.add(name)
            _arm_refresh()
            return previous
        if text != previous:
            stall["t"] = now
        shown[name] = text
        return text

    if MODE == "proto":
        _target = _proto_label


def use_impl(which):
    """Switch the label implementation in place (same session, fair A/B/C)."""
    global _target
    # "proto" was the harness-side prototype; it is now ported into labelmaker.py
    _target = {"stock": _stock_target, "on": _lm_target, "proto": _lm_target}[which]
    state["impl"] = which
    autolabeller.invalidate_labels()
    log("      >>> label implementation: {}".format(which))

def _timed_autolabel():
    counter["calls"] += 1
    t0 = time.perf_counter()
    try:
        return _target()
    finally:
        dt = time.perf_counter() - t0
        counter["seconds"] += dt
        entry = by_class.setdefault(nuke.thisClass(), [0, 0.0])
        entry[0] += 1
        entry[1] += dt


def log_by_class(title, top=18):
    log("\n  {} — per node class (slowest total first)".format(title))
    log("  {:<44} {:>6} {:>10} {:>9}".format("class", "calls", "total ms", "mean us"))
    for cls, (n, secs) in sorted(by_class.items(), key=lambda kv: -kv[1][1])[:top]:
        log("  {:<44} {:>6} {:>10.1f} {:>9.1f}".format(cls[:44], n, secs * 1000, secs / n * 1e6))
    by_class.clear()


nuke.addAutolabel(_timed_autolabel)

state = {"dag": None, "gl": None, "profiler": None}
results = []


def log(msg=""):
    sys.stdout = sys.__stderr__
    print(msg)
    sys.stdout.flush()


# ----------------------------------------------------------------------
# real X input via XTest: Nuke's GL DAG ignores QTest-level events (a QTest
# drag leaves the node where it was); XTest events go through the X server
# and Qt's xcb plugin exactly like a physical mouse.
# ----------------------------------------------------------------------
import ctypes  # noqa: E402

_X11 = ctypes.CDLL("libX11.so.6")
_XT = ctypes.CDLL("libXtst.so.6")
_X11.XOpenDisplay.restype = ctypes.c_void_p
_X11.XOpenDisplay.argtypes = [ctypes.c_char_p]
_X11.XFlush.argtypes = [ctypes.c_void_p]
_XT.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
_XT.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
_dpy = _X11.XOpenDisplay(None)
TICK_MS = 16   # one pointer event per tick, ~60 Hz like a real mouse


def x_move(p):
    _XT.XTestFakeMotionEvent(_dpy, -1, int(p.x()), int(p.y()), 0)
    _X11.XFlush(_dpy)


def x_button(button, down):
    _XT.XTestFakeButtonEvent(_dpy, button, 1 if down else 0, 0)
    _X11.XFlush(_dpy)


def gesture(steps):
    """Run callables one per TICK_MS from the event loop; record tick latency.

    Sets state["busy"] so wait_settled holds until the gesture is finished.
    Each tick's latency (actual - scheduled interval) is Nuke's main-loop
    stall during the gesture: what the user feels as sluggishness.
    """
    state["busy"] = True
    state["ticks"] = []
    it = iter(steps)
    last = {"t": time.perf_counter()}

    def tick():
        now = time.perf_counter()
        state["ticks"].append(now - last["t"] - TICK_MS / 1000.0)
        last["t"] = now
        try:
            next(it)()
        except StopIteration:
            state["busy"] = False
            return
        QtCore.QTimer.singleShot(TICK_MS, tick)

    QtCore.QTimer.singleShot(TICK_MS, tick)


def path(a, b, n):
    return [QtCore.QPoint(int(a.x() + (b.x() - a.x()) * i / n), int(a.y() + (b.y() - a.y()) * i / n)) for i in range(1, n + 1)]


def x_drag(button, a, b, n=30, hold=3):
    steps = [lambda a=a: x_move(a)] * hold + [lambda: x_button(button, True)]
    steps += [lambda p=p: x_move(p) for p in path(a, b, n)]
    steps += [lambda: None] * hold + [lambda: x_button(button, False)] + [lambda: None] * hold
    return steps


def x_wheel(at, clicks, up=True):
    steps = [lambda: x_move(at)] * 3
    for _ in range(clicks):
        steps += [lambda: x_button(4 if up else 5, True), lambda: x_button(4 if up else 5, False)]
    return steps


def dag_global(local):
    return state["dag"].mapToGlobal(local)


def dag_centre():
    return dag_global(QtCore.QPoint(state["dag"].width() // 2, state["dag"].height() // 2))


def node_global(node):
    return dag_global(node_screen_pos(node))


def knob_slider_rect(node, knob_name):
    """Global rect of the KnobSlider for knob_name in node's open Properties panel."""
    app = QtWidgets.QApplication.instance()
    for w in app.allWidgets():
        if w.metaObject().className() == "MultiView_Group" and w.objectName() == knob_name and w.isVisible():
            for c in w.findChildren(QtWidgets.QWidget):
                if c.metaObject().className() == "KnobSlider" and c.isVisible():
                    return QtCore.QRect(c.mapToGlobal(QtCore.QPoint(0, 0)), c.size())
    return None


def widget_rect(class_name):
    app = QtWidgets.QApplication.instance()
    best = None
    for w in app.allWidgets():
        if class_name in w.metaObject().className() and w.width() > 200:
            r = QtCore.QRect(w.mapToGlobal(QtCore.QPoint(0, 0)), w.size())
            if w.isVisible():
                return r
            best = best or r
    return best


def slider_drag(node, knob_name, frac_from=0.3, frac_to=0.7, n=40):
    r = knob_slider_rect(node, knob_name)
    if r is None:
        log("      (no slider found for {})".format(knob_name))
        return []
    y = r.center().y()
    return x_drag(1, QtCore.QPoint(int(r.left() + r.width() * frac_from), y),
                  QtCore.QPoint(int(r.left() + r.width() * frac_to), y), n)


def timeslider_drag(frac_from=0.2, frac_to=0.6, n=40):
    r = widget_rect("TimeSlider")
    if r is None:
        # no visible timeslider: feed frames from the event loop instead, which
        # is what the slider does internally (one setFrame per pointer move)
        f0, f1 = 1001 + int(60 * frac_from), 1001 + int(60 * frac_to)
        step = 1 if f1 >= f0 else -1
        return [lambda f=f: nuke.frame(f) for f in range(f0, f1 + step, step)]
    y = r.center().y()
    return x_drag(1, QtCore.QPoint(int(r.left() + r.width() * frac_from), y),
                  QtCore.QPoint(int(r.left() + r.width() * frac_to), y), n)


def wait_settled(then, label):
    """Poll until the autolabel call count stops changing, then call then()."""
    start = time.perf_counter()
    seen = {"calls": counter["calls"], "since": time.perf_counter(), "last_change": None}

    def poll():
        now = time.perf_counter()
        if counter["calls"] != seen["calls"]:
            seen["calls"] = counter["calls"]
            seen["since"] = now
            seen["last_change"] = now
        if state.get("busy"):
            seen["since"] = now
        if (now - seen["since"]) * 1000 >= QUIET_MS or now - start > SETTLE_CAP_S:
            settle = (seen["last_change"] - start) if seen["last_change"] else 0.0
            then(settle)
            return
        QtCore.QTimer.singleShot(50, poll)

    QtCore.QTimer.singleShot(50, poll)


def measure(label, action, then):
    """Run action(), wait for the label pass to settle, record calls + time."""
    before = counter["calls"]
    before_s = counter["seconds"]
    getattr(autolabeller, "reset_stats", lambda: None)()
    t0 = time.perf_counter()
    action()
    action_s = time.perf_counter() - t0

    def done(settle_s):
        calls = counter["calls"] - before
        lm = counter["seconds"] - before_s
        builds = sum(getattr(autolabeller, "_stats_calls_by_node", {}).values())
        hits = sum(getattr(autolabeller, "_stats_hits_by_node", {}).values())
        deov = getattr(autolabeller, "_stats_time", {}).get("deoverlap (timer)", 0.0)
        ticks = state.pop("ticks", None)
        results.append((label, calls, builds, hits, action_s, settle_s, lm, deov))
        extra = ""
        if ticks and len(ticks) > 1:
            lat = sorted(t * 1000 for t in ticks[1:])
            extra = "  ticks={} lat mean={:.1f}ms p95={:.1f}ms max={:.1f}ms".format(
                len(lat), sum(lat) / len(lat), lat[int(len(lat) * 0.95) - 1], lat[-1])
        if deov:
            extra += "  deoverlap={:.1f}ms".format(deov * 1000)
        if False:
            extra += "  proto: {} hits {} builds {} held {} pokes  stall~{:.0f}ms window={:.0f}ms".format(
                cache_stats["hits"], cache_stats["builds"], cache_stats["held"], cache_stats["pokes"], stall["ema"] * 1000, _window() * 1000)
            cache_stats.update(hits=0, builds=0, held=0, pokes=0)
        log("  {:<40} calls={:<5} builds={:<5} hits={:<5} action={:7.1f}ms  settled_after={:6.2f}s  label_py={:7.1f}ms{}".format(
            label, calls, builds, hits, action_s * 1000, settle_s, lm * 1000, extra))
        then()

    wait_settled(done, label)


def node_screen_pos(node):
    """DAG-window pixel position of a node's centre, from the DAG zoom/centre."""
    z = nuke.zoom()
    cx, cy = nuke.center()
    w = state["dag"]
    return QtCore.QPoint(int(w.width() / 2 + (node.xpos() + node.screenWidth() / 2 - cx) * z),
                         int(w.height() / 2 + (node.ypos() + node.screenHeight() / 2 - cy) * z))


# ----------------------------------------------------------------------
# steps — each returns to the exec loop before the next runs
# ----------------------------------------------------------------------
def step_open():
    log("\n" + "#" * 78)
    log("# Labelmaker draw-path profile — mode={} deoverlap={} script={}".format(MODE, DEOVERLAP, SCRIPT))
    log("#" * 78)
    app = QtWidgets.QApplication.instance()
    outer = [w for w in app.allWidgets() if w.metaObject().className() == "DAGNukeWindow"][0]
    state["dag"] = [c for c in outer.children()
                    if isinstance(c, QtWidgets.QWidget) and c.metaObject().className() == "DAG_Window"][0]
    log("DAG window {}x{}".format(state["dag"].width(), state["dag"].height()))

    faulthandler.dump_traceback_later(300, repeat=True, file=sys.__stderr__)
    if MODE != "off" and PROF_OUT:
        state["profiler"] = cProfile.Profile()
        state["profiler"].enable()
    measure("scriptOpen (%s)" % os.path.basename(SCRIPT), lambda: nuke.scriptOpen(SCRIPT), step_after_open)


def step_after_open():
    if state["profiler"]:
        state["profiler"].disable()
    nodes = [n for n in nuke.allNodes() if n.Class() != "Viewer"]
    log("{} nodes loaded; autolabel calls so far = {}".format(len(nodes), counter["calls"]))
    log_by_class("script open")
    if MODE != "off" and hasattr(autolabeller, "dump_stats"):
        autolabeller.dump_stats()
    state["nodes"] = nodes
    run_events()


def run_events():
    nodes = state["nodes"]
    grade = next(n for n in nodes if n.Class() == "Grade")
    read = next(n for n in nodes if n.Class() == "Read")
    merge = next(n for n in nodes if n.Class() == "Merge2")
    blur = next(n for n in nodes if n.Class() == "Blur")
    mid = nodes[len(nodes) // 2]
    cc = next(n for n in nodes if n.Class() == "ColorCorrect")
    grade2 = [n for n in nodes if n.Class() == "Grade"][1]
    ctrl = next((n for n in nodes if n.name().startswith("CTRL_")), None)
    group = next((n for n in nodes if n.Class() == "Group"), None)
    clone = next((n for n in nodes if n.clones()), None)
    all_xy = [(n.xpos(), n.ypos()) for n in nodes]
    cx = sum(x for x, _ in all_xy) / len(all_xy)
    cy = sum(y for _, y in all_xy) / len(all_xy)
    state["mid_x0"] = mid.xpos()

    steps = [
        ("zoom to fit all (nuke.zoom 0.15)", lambda: nuke.zoom(0.15, (cx, cy))),
        ("zoom 1.0 at centre", lambda: nuke.zoom(1.0, (cx, cy))),
        ("zoom 2.5 at centre", lambda: nuke.zoom(2.5, (cx, cy))),
        ("pan x3 via nuke.zoom", lambda: [nuke.zoom(2.5, (cx + d, cy + d)) for d in (300, 600, 900)]),
        ("zoom 1.0 again", lambda: nuke.zoom(1.0, (cx, cy))),
        ("X hover sweep across DAG (60 moves)", lambda: gesture([lambda p=p: x_move(p) for p in path(dag_centre() - QtCore.QPoint(400, 0), dag_centre() + QtCore.QPoint(400, 0), 60)])),
        ("X wheel zoom in x6", lambda: gesture(x_wheel(dag_centre(), 6, up=True))),
        ("X wheel zoom out x6", lambda: gesture(x_wheel(dag_centre(), 6, up=False))),
        ("X middle-drag pan 300px (30 moves)", lambda: gesture(x_drag(2, dag_centre(), dag_centre() + QtCore.QPoint(300, 150)))),
        ("zoom 1.0 on mid node", lambda: nuke.zoom(1.0, (mid.xpos(), mid.ypos()))),
        ("X click on node", lambda: gesture(x_drag(1, node_global(mid), node_global(mid), n=1))),
        ("X drag one node 200px (40 moves)", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(200, 0), n=40))),
        ("  -> node moved?", lambda: log("      {} xpos now {} (was {})".format(mid.name(), mid.xpos(), state.get("mid_x0")))),
        ("select 50 nodes", lambda: _select(nodes[len(nodes) // 2: len(nodes) // 2 + 50])),
        ("X drag 50 selected nodes 200px", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(200, 0), n=40))),
        ("select all", nuke.selectAll),
        ("X drag ALL selected nodes 200px", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(200, 0), n=40))),
        ("deselect all (after drag)", lambda: [n.setSelected(False) for n in nodes]),
        ("X rubber-band select across DAG", lambda: gesture(x_drag(1, dag_global(QtCore.QPoint(20, 20)), dag_global(QtCore.QPoint(state["dag"].width() - 250, state["dag"].height() - 20)), n=30))),
        ("deselect all (after band)", lambda: [n.setSelected(False) for n in nodes]),
        ("X wire-drag: Merge B input to Read", lambda: gesture(x_drag(1, node_global(merge) + QtCore.QPoint(0, -merge.screenHeight() // 2 - 4), node_global(read), n=30))),
        ("open Grade panel", lambda: grade.showControlPanel()),
        ("X slider drag Grade.white (40 moves)", lambda: gesture(slider_drag(grade, "white"))),
        ("  -> white value", lambda: log("      white = {}".format(grade["white"].value()))),
        ("X slider drag Grade.gamma (40 moves)", lambda: gesture(slider_drag(grade, "gamma"))),
        ("X slider drag Grade.mix (40 moves)", lambda: gesture(slider_drag(grade, "mix", 0.9, 0.4))),
        ("  -> mix value", lambda: log("      mix = {}".format(grade["mix"].value()))),
        ("close Grade panel", lambda: grade.hideControlPanel()),
        ("H opHashes before", lambda: log("      grade opHashes={} blur={}".format(grade.opHashes(), blur.opHashes()))),
        ("H edit grade.white", lambda: grade["white"].setValue(grade["white"].value() + 0.1)),
        ("H opHashes after knob edit (no viewer)", lambda: log("      grade opHashes={}".format(grade.opHashes()))),
        ("H connect viewer to grade", lambda: nuke.connectViewer(0, grade)),
        ("H opHashes after viewer connect", lambda: log("      grade opHashes={}".format(grade.opHashes()))),
        ("H edit grade.white again", lambda: grade["white"].setValue(grade["white"].value() + 0.1)),
        ("H opHashes after edit (viewer on)", lambda: log("      grade opHashes={}".format(grade.opHashes()))),
        ("H edit grade.label", lambda: grade["label"].setValue("hello")),
        ("H opHashes after label edit", lambda: log("      grade opHashes={}".format(grade.opHashes()))),
        ("H edit grade.mix", lambda: grade["mix"].setValue(0.5)),
        ("H opHashes after mix edit", lambda: log("      grade opHashes={}".format(grade.opHashes()))),
        ("H edit blur.size (not viewed)", lambda: blur["size"].setValue(blur["size"].value() + 1)),
        ("H blur opHashes after edit (not viewed)", lambda: log("      blur opHashes={}".format(blur.opHashes()))),
        ("H frame +1", lambda: nuke.frame(nuke.frame() + 1)),
        ("H opHashes after frame change", lambda: log("      grade={} blur={}".format(grade.opHashes(), blur.opHashes()))),
        ("H cost of opHashes x3000", lambda: log("      {:.1f} us/node".format(_time_hashes(nodes)))),
        ("N zoom 1.0 on mid", lambda: nuke.zoom(1.0, (mid.xpos(), mid.ypos()))),
        ("N drag 1 node, no callback", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(200, 0), n=40))),
        ("N select 50", lambda: _select(nodes[len(nodes) // 2: len(nodes) // 2 + 50])),
        ("N drag 50 nodes, no callback", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(200, 0), n=40))),
        ("N select all", nuke.selectAll),
        ("N drag ALL nodes, no callback", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(200, 0), n=40))),
        ("N deselect", lambda: [n.setSelected(False) for n in nodes]),
        ("N register counting knobChanged", lambda: [kc_count.update(n=0, names={}), nuke.addKnobChanged(_counting_kc)]),
        ("N drag 1 node, counting callback", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(-200, 0), n=40))),
        ("N  -> knobChanged calls", lambda: _kc_report()),
        ("N select 50 ", lambda: _select(nodes[len(nodes) // 2: len(nodes) // 2 + 50])),
        ("N drag 50 nodes, counting callback", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(-200, 0), n=40))),
        ("N  -> knobChanged calls ", lambda: _kc_report()),
        ("N select all ", nuke.selectAll),
        ("N drag ALL nodes, counting callback", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(-200, 0), n=40))),
        ("N  -> knobChanged calls  ", lambda: _kc_report()),
        ("N deselect ", lambda: [n.setSelected(False) for n in nodes]),
        ("N rubber-band, counting callback", lambda: gesture(x_drag(1, dag_global(QtCore.QPoint(20, 20)), dag_global(QtCore.QPoint(state["dag"].width() - 250, state["dag"].height() - 20)), n=30))),
        ("N  -> knobChanged calls   ", lambda: _kc_report()),
        ("N select all + move via Python (autoplace-ish)", lambda: [nuke.selectAll(), [n.setXYpos(n.xpos() + 10, n.ypos()) for n in nodes]]),
        ("N  -> knobChanged calls    ", lambda: _kc_report()),
        ("N frame +1, counting", lambda: nuke.frame(nuke.frame() + 1)),
        ("N  -> knobChanged calls     ", lambda: _kc_report()),
        ("N open panel + slider drag, counting", lambda: [grade.showControlPanel(), gesture(slider_drag(grade, "white"))]),
        ("N  -> knobChanged calls      ", lambda: [_kc_report(), grade.hideControlPanel()]),
        ("N remove counting knobChanged", lambda: nuke.removeKnobChanged(_counting_kc)),
        ("Q baseline frame +1", lambda: nuke.frame(nuke.frame() + 1)),
        ("Q baseline frame +1 again", lambda: nuke.frame(nuke.frame() + 1)),
        ("Q poke dope_sheet", lambda: _poke(grade, "dope_sheet", lambda v: not v, quiet=True)),
        ("Q frame +1 after poke dope_sheet", lambda: nuke.frame(nuke.frame() + 1)),
        ("Q poke bookmark", lambda: _poke(grade, "bookmark", lambda v: not v, quiet=True)),
        ("Q frame +1 after poke bookmark", lambda: nuke.frame(nuke.frame() + 1)),
        ("Q poke tile_color", lambda: _poke(grade, "tile_color", lambda v: v + 1, quiet=True)),
        ("Q frame +1 after poke tile_color", lambda: nuke.frame(nuke.frame() + 1)),
        ("Q poke note_font_color", lambda: _poke(grade, "note_font_color", lambda v: v + 1, quiet=True)),
        ("Q frame +1 after poke note_font_color", lambda: nuke.frame(nuke.frame() + 1)),
        ("Q poke gl_color", lambda: _poke(grade, "gl_color", lambda v: v + 1, quiet=True)),
        ("Q frame +1 after poke gl_color", lambda: nuke.frame(nuke.frame() + 1)),
        ("Q poke label", lambda: _poke(grade, "label", lambda v: v + ' ', quiet=True)),
        ("Q frame +1 after poke label", lambda: nuke.frame(nuke.frame() + 1)),
        ("Q poke hide_input", lambda: _poke(grade, "hide_input", lambda v: not v, quiet=True)),
        ("Q frame +1 after poke hide_input", lambda: nuke.frame(nuke.frame() + 1)),
        ("Q poke cached", lambda: _poke(grade, "cached", lambda v: not v, quiet=True)),
        ("Q frame +1 after poke cached", lambda: nuke.frame(nuke.frame() + 1)),
        ("Q poke postage_stamp", lambda: _poke(grade, "postage_stamp", lambda v: not v, quiet=True)),
        ("Q frame +1 after poke postage_stamp", lambda: nuke.frame(nuke.frame() + 1)),
        ("Q poke note_font", lambda: _poke(grade, "note_font", lambda v: v + ' ', quiet=True)),
        ("Q frame +1 after poke note_font", lambda: nuke.frame(nuke.frame() + 1)),
        ("Q poke icon", lambda: _poke(grade, "icon", lambda v: 'x', quiet=True)),
        ("Q frame +1 after poke icon", lambda: nuke.frame(nuke.frame() + 1)),
        ("Q poke autolabel", lambda: _poke(grade, "autolabel", lambda v: 'x', quiet=True)),
        ("Q frame +1 after poke autolabel", lambda: nuke.frame(nuke.frame() + 1)),
        ("W frame +1 (proto callbacks registered)", lambda: nuke.frame(nuke.frame() + 1)),
        ("W frame +1 (no knobChanged)", lambda: nuke.frame(nuke.frame() + 1)),
        ("W frame +1 again", lambda: nuke.frame(nuke.frame() + 1)),
        ("W frame +1 (no callbacks)", lambda: nuke.frame(nuke.frame() + 1)),
        ("W viewer connect (no callbacks)", lambda: nuke.connectViewer(0, merge)),
        ("W viewer connect other (no callbacks)", lambda: nuke.connectViewer(0, grade)),
        ("W add empty knobChanged", lambda: nuke.addKnobChanged(_noop_kc)),
        ("W frame +1 (empty knobChanged)", lambda: nuke.frame(nuke.frame() + 1)),
        ("W remove empty knobChanged", lambda: nuke.removeKnobChanged(_noop_kc)),
        ("W frame +1 (none again)", lambda: nuke.frame(nuke.frame() + 1)),
        ("W add knobChanged for class Grade only", lambda: nuke.addKnobChanged(_noop_kc, nodeClass="Grade")),
        ("W frame +1 (Grade-only knobChanged)", lambda: nuke.frame(nuke.frame() + 1)),
        ("W remove it", lambda: nuke.removeKnobChanged(_noop_kc, nodeClass="Grade")),
        ("V viewer connect #1", lambda: nuke.connectViewer(0, merge)),
        ("V viewer connect #2 other node", lambda: nuke.connectViewer(0, grade)),
        ("V frame +1", lambda: nuke.frame(nuke.frame() + 1)),
        ("V viewer connect #3 after frame change", lambda: nuke.connectViewer(0, blur)),
        ("V viewer connect #4", lambda: nuke.connectViewer(0, merge)),
        ("V knob edit", lambda: grade["white"].setValue(1.31)),
        ("V viewer connect #5 after knob edit", lambda: nuke.connectViewer(0, grade)),
        ("V rename node", lambda: blur.setName("Blur_v")),
        ("V frame +1 after rename", lambda: nuke.frame(nuke.frame() + 1)),
        ("V viewer connect #6 after rename", lambda: nuke.connectViewer(0, blur)),
        ("V create node", lambda: state.__setitem__("vn", nuke.nodes.Blur(name="VBlur"))),
        ("V frame +1 after create", lambda: nuke.frame(nuke.frame() + 1)),
        ("V viewer connect #7 after create", lambda: nuke.connectViewer(0, merge)),
        ("V delete node", lambda: nuke.delete(state["vn"])),
        ("V frame +1 after delete", lambda: nuke.frame(nuke.frame() + 1)),
        ("V viewer connect #8 after delete", lambda: nuke.connectViewer(0, grade)),
        ("V disconnect viewer", lambda: nuke.connectViewer(0, None)),
        ("V frame +1 after disconnect", lambda: nuke.frame(nuke.frame() + 1)),
        ("V viewer connect #9 after disconnect", lambda: nuke.connectViewer(0, grade)),
        ("V root first_frame change", lambda: nuke.root()["first_frame"].setValue(1000)),
        ("V viewer connect #10 after root edit", lambda: nuke.connectViewer(0, merge)),
        ("V frame +1 after root edit", lambda: nuke.frame(nuke.frame() + 1)),
        ("V select all + deselect", lambda: [nuke.selectAll(), [n.setSelected(False) for n in nodes]]),
        ("V viewer connect #11", lambda: nuke.connectViewer(0, grade)),
        ("V zoom 0.15 + viewer connect #12", lambda: [nuke.zoom(0.15, (cx, cy)), nuke.connectViewer(0, merge)]),
        ("A open Grade panel + zoom", lambda: [grade.showControlPanel(), nuke.zoom(1.0, (grade.xpos(), grade.ypos()))]),
        ("A round 1 impl stock", lambda: use_impl("stock")),
        ("A stock slider white", lambda: gesture(slider_drag(grade, "white"))),
        ("A stock slider gamma", lambda: gesture(slider_drag(grade, "gamma", 0.7, 0.3))),
        ("A round 1 impl on", lambda: use_impl("on")),
        ("A on slider white", lambda: gesture(slider_drag(grade, "white", 0.7, 0.3))),
        ("A on slider gamma", lambda: gesture(slider_drag(grade, "gamma"))),
        ("A round 1 impl proto", lambda: use_impl("proto")),
        ("A proto slider white", lambda: gesture(slider_drag(grade, "white"))),
        ("A proto slider gamma", lambda: gesture(slider_drag(grade, "gamma", 0.7, 0.3))),
        ("A  -> white value vs label", lambda: _check_label(grade, "white")),
        ("A round 2 impl stock", lambda: use_impl("stock")),
        ("A stock slider white 2", lambda: gesture(slider_drag(grade, "white", 0.7, 0.3))),
        ("A round 2 impl on", lambda: use_impl("on")),
        ("A on slider white 2", lambda: gesture(slider_drag(grade, "white"))),
        ("A round 2 impl proto", lambda: use_impl("proto")),
        ("A proto slider white 2", lambda: gesture(slider_drag(grade, "white", 0.7, 0.3))),
        ("A  -> white value vs label 2", lambda: _check_label(grade, "white")),
        ("A close panel", lambda: grade.hideControlPanel()),
        ("B impl stock", lambda: use_impl("stock")),
        ("B stock warm: viewer connect + frame", lambda: [nuke.connectViewer(0, merge), nuke.frame(nuke.frame() + 1)]),
        ("B stock frame +1 (frame-dependent only)", lambda: nuke.frame(nuke.frame() + 1)),
        ("B stock viewer connect other node", lambda: nuke.connectViewer(0, grade)),
        ("B stock frame +1 after connect (ALL)", lambda: nuke.frame(nuke.frame() + 1)),
        ("B stock viewer connect back", lambda: nuke.connectViewer(0, merge)),
        ("B stock frame -1 after connect (ALL)", lambda: nuke.frame(nuke.frame() - 1)),
        ("B stock scrub", lambda: gesture(timeslider_drag(0.2, 0.5))),
        ("B stock scrub back", lambda: gesture(timeslider_drag(0.5, 0.2))),
        ("B impl on", lambda: use_impl("on")),
        ("B on warm: viewer connect + frame", lambda: [nuke.connectViewer(0, merge), nuke.frame(nuke.frame() + 1)]),
        ("B on frame +1 (frame-dependent only)", lambda: nuke.frame(nuke.frame() + 1)),
        ("B on viewer connect other node", lambda: nuke.connectViewer(0, grade)),
        ("B on frame +1 after connect (ALL)", lambda: nuke.frame(nuke.frame() + 1)),
        ("B on viewer connect back", lambda: nuke.connectViewer(0, merge)),
        ("B on frame -1 after connect (ALL)", lambda: nuke.frame(nuke.frame() - 1)),
        ("B on scrub", lambda: gesture(timeslider_drag(0.2, 0.5))),
        ("B on scrub back", lambda: gesture(timeslider_drag(0.5, 0.2))),
        ("B impl proto", lambda: use_impl("proto")),
        ("B proto warm: viewer connect + frame", lambda: [nuke.connectViewer(0, merge), nuke.frame(nuke.frame() + 1)]),
        ("B proto frame +1 (frame-dependent only)", lambda: nuke.frame(nuke.frame() + 1)),
        ("B proto viewer connect other node", lambda: nuke.connectViewer(0, grade)),
        ("B proto frame +1 after connect (ALL)", lambda: nuke.frame(nuke.frame() + 1)),
        ("B proto viewer connect back", lambda: nuke.connectViewer(0, merge)),
        ("B proto frame -1 after connect (ALL)", lambda: nuke.frame(nuke.frame() - 1)),
        ("B proto scrub", lambda: gesture(timeslider_drag(0.2, 0.5))),
        ("B proto scrub back", lambda: gesture(timeslider_drag(0.5, 0.2))),
        ("C impl stock", lambda: use_impl("stock")),
        ("C stock zoom on mid", lambda: nuke.zoom(1.0, (mid.xpos(), mid.ypos()))),
        ("C stock drag 1 node", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(120, 0), n=40))),
        ("C stock select 50", lambda: _select(nodes[len(nodes) // 2: len(nodes) // 2 + 50])),
        ("C stock drag 50 nodes", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(-120, 0), n=40))),
        ("C stock select all", nuke.selectAll),
        ("C stock drag ALL nodes", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(120, 0), n=40))),
        ("C stock deselect", lambda: [n.setSelected(False) for n in nodes]),
        ("C stock rubber-band", lambda: gesture(x_drag(1, dag_global(QtCore.QPoint(20, 20)), dag_global(QtCore.QPoint(state["dag"].width() - 250, state["dag"].height() - 20)), n=30))),
        ("C stock deselect ", lambda: [n.setSelected(False) for n in nodes]),
        ("C impl on", lambda: use_impl("on")),
        ("C on zoom on mid", lambda: nuke.zoom(1.0, (mid.xpos(), mid.ypos()))),
        ("C on drag 1 node", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(120, 0), n=40))),
        ("C on select 50", lambda: _select(nodes[len(nodes) // 2: len(nodes) // 2 + 50])),
        ("C on drag 50 nodes", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(-120, 0), n=40))),
        ("C on select all", nuke.selectAll),
        ("C on drag ALL nodes", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(120, 0), n=40))),
        ("C on deselect", lambda: [n.setSelected(False) for n in nodes]),
        ("C on rubber-band", lambda: gesture(x_drag(1, dag_global(QtCore.QPoint(20, 20)), dag_global(QtCore.QPoint(state["dag"].width() - 250, state["dag"].height() - 20)), n=30))),
        ("C on deselect ", lambda: [n.setSelected(False) for n in nodes]),
        ("C impl proto", lambda: use_impl("proto")),
        ("C proto zoom on mid", lambda: nuke.zoom(1.0, (mid.xpos(), mid.ypos()))),
        ("C proto drag 1 node", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(120, 0), n=40))),
        ("C proto select 50", lambda: _select(nodes[len(nodes) // 2: len(nodes) // 2 + 50])),
        ("C proto drag 50 nodes", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(-120, 0), n=40))),
        ("C proto select all", nuke.selectAll),
        ("C proto drag ALL nodes", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(120, 0), n=40))),
        ("C proto deselect", lambda: [n.setSelected(False) for n in nodes]),
        ("C proto rubber-band", lambda: gesture(x_drag(1, dag_global(QtCore.QPoint(20, 20)), dag_global(QtCore.QPoint(state["dag"].width() - 250, state["dag"].height() - 20)), n=30))),
        ("C proto deselect ", lambda: [n.setSelected(False) for n in nodes]),
        ("R impl proto", lambda: use_impl("proto")),
        ("R proto zoom on mid", lambda: nuke.zoom(1.0, (mid.xpos(), mid.ypos()))),
        ("R proto drag 1 node", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(120, 0), n=40))),
        ("R proto select all", nuke.selectAll),
        ("R proto drag ALL nodes", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(-120, 0), n=40))),
        ("R proto deselect", lambda: [n.setSelected(False) for n in nodes]),
        ("R impl on", lambda: use_impl("on")),
        ("R on zoom on mid", lambda: nuke.zoom(1.0, (mid.xpos(), mid.ypos()))),
        ("R on drag 1 node", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(120, 0), n=40))),
        ("R on select all", nuke.selectAll),
        ("R on drag ALL nodes", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(-120, 0), n=40))),
        ("R on deselect", lambda: [n.setSelected(False) for n in nodes]),
        ("R impl stock", lambda: use_impl("stock")),
        ("R stock zoom on mid", lambda: nuke.zoom(1.0, (mid.xpos(), mid.ypos()))),
        ("R stock drag 1 node", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(120, 0), n=40))),
        ("R stock select all", nuke.selectAll),
        ("R stock drag ALL nodes", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(-120, 0), n=40))),
        ("R stock deselect", lambda: [n.setSelected(False) for n in nodes]),
        ("R impl proto", lambda: use_impl("proto")),
        ("R proto zoom on mid", lambda: nuke.zoom(1.0, (mid.xpos(), mid.ypos()))),
        ("R proto drag 1 node", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(120, 0), n=40))),
        ("R proto select all", nuke.selectAll),
        ("R proto drag ALL nodes", lambda: gesture(x_drag(1, node_global(mid), node_global(mid) + QtCore.QPoint(-120, 0), n=40))),
        ("R proto deselect", lambda: [n.setSelected(False) for n in nodes]),
        ("K open Grade panel + zoom", lambda: [grade.showControlPanel(), nuke.zoom(1.0, (grade.xpos(), grade.ypos()))]),
        ("K slider drag white (no knobChanged cb)", lambda: gesture(slider_drag(grade, "white"))),
        ("K register empty knobChanged", lambda: nuke.addKnobChanged(_noop_kc)),
        ("K slider drag white (empty knobChanged cb)", lambda: gesture(slider_drag(grade, "white", 0.7, 0.3))),
        ("K remove knobChanged", lambda: nuke.removeKnobChanged(_noop_kc)),
        ("K close panel", lambda: grade.hideControlPanel()),
        ("T poke note_font_size +1/-1 same callback", lambda: _poke(grade, "note_font_size", lambda v: v + 1)),
        ("T poke dope_sheet toggle twice", lambda: _poke(grade, "dope_sheet", lambda v: not v)),
        ("T poke bookmark toggle twice", lambda: _poke(grade, "bookmark", lambda v: not v)),
        ("T poke burst x40 note_font_size (latency)", lambda: gesture([lambda: _poke(grade, "note_font_size", lambda v: v + 1, quiet=True) for _ in range(40)])),
        ("T poke burst x40 dope_sheet (latency)", lambda: gesture([lambda: _poke(grade, "dope_sheet", lambda v: not v, quiet=True) for _ in range(40)])),
        ("T indicators setValue(same)", lambda: grade["indicators"].setValue(grade["indicators"].value())),
        ("T indicators setValue(+1) ", lambda: grade["indicators"].setValue(int(grade["indicators"].value()) + 1)),
        ("T indicators setValue(-1 back)", lambda: grade["indicators"].setValue(int(grade["indicators"].value()) - 1)),
        ("T nuke.knob this.indicators (runIn)", lambda: nuke.runIn(grade.fullName(), "nuke.knob('this.indicators', str(int(nuke.numvalue('this.indicators'))))")),
        ("T tile_color setValue(same)", lambda: grade["tile_color"].setValue(grade["tile_color"].value())),
        ("T label setValue(same)", lambda: grade["label"].setValue(grade["label"].value())),
        ("T note_font_size setValue(same)", lambda: grade["note_font_size"].setValue(grade["note_font_size"].value())),
        ("T hide_input setValue(same)", lambda: grade["hide_input"].setValue(grade["hide_input"].value())),
        ("T icon setValue('')", lambda: grade["icon"].setValue("")),
        ("T nuke.updateUI()", nuke.updateUI),
        ("T forceValidate()", grade.forceValidate),
        ("T setSelected toggle", lambda: [grade.setSelected(True), grade.setSelected(False)]),
        ("T knob white setValue(same)", lambda: grade["white"].setValue(grade["white"].value())),
        ("T autolabel knob? ", lambda: log("      has autolabel knob: {}".format(grade.knob("autolabel") is not None))),
        ("P set debounce 0", lambda: _set_debounce(0)),
        ("P setValue burst white, no panel, zoom 1.0", lambda: gesture([lambda v=1.0 + 0.01 * i: grade["white"].setValue(v) for i in range(40)])),
        ("P setValue burst white, no panel, zoom 0.15", lambda: [nuke.zoom(0.15, (cx, cy)), gesture([lambda v=1.0 + 0.01 * i: grade["white"].setValue(v) for i in range(40)])]),
        ("P setValue burst gamma, no panel, node off-screen", lambda: [nuke.zoom(3.0, (cx + 50000, cy + 50000)), gesture([lambda v=1.0 + 0.01 * i: grade["gamma"].setValue(v) for i in range(40)])]),
        ("P open Grade panel", lambda: [grade.showControlPanel(), nuke.zoom(1.0, (grade.xpos(), grade.ypos()))]),
        ("P setValue burst white, panel open", lambda: gesture([lambda v=1.2 + 0.01 * i: grade["white"].setValue(v) for i in range(40)])),
        ("P setValue burst on OTHER Grade, panel open", lambda: gesture([lambda v=1.2 + 0.01 * i: grade2["white"].setValue(v) for i in range(40)])),
        ("P close panel", lambda: grade.hideControlPanel()),
        ("P setValue burst on CC.saturation (text unchanged)", lambda: gesture([lambda v=1.2 + 0.01 * i: cc["saturation"].setValue(v) for i in range(40)])),
        ("P setValue burst label knob (text changes)", lambda: gesture([lambda v=i: grade["label"].setValue("v{}".format(v)) for i in range(40)])),
        ("P set debounce 100", lambda: _set_debounce(100)),
        ("D open CC panel + zoom on it", lambda: [cc.showControlPanel(), nuke.zoom(1.0, (cc.xpos(), cc.ypos()))]),
        ("D slider CC.saturation (text unchanged)", lambda: gesture(slider_drag(cc, "saturation", 0.4, 0.8))),
        ("D slider CC.saturation again", lambda: gesture(slider_drag(cc, "saturation", 0.8, 0.4))),
        ("D close CC panel", lambda: cc.hideControlPanel()),
        ("D open Grade panel + zoom on it", lambda: [grade.showControlPanel(), nuke.zoom(1.0, (grade.xpos(), grade.ypos()))]),
        ("D slider Grade.white debounce 100", lambda: gesture(slider_drag(grade, "white"))),
        ("D set debounce 0", lambda: _set_debounce(0)),
        ("D slider Grade.white debounce 0", lambda: gesture(slider_drag(grade, "white", 0.7, 0.3))),
        ("D set debounce 300", lambda: _set_debounce(300)),
        ("D slider Grade.white debounce 300", lambda: gesture(slider_drag(grade, "white"))),
        ("D set debounce 0 + zoom to fit all", lambda: [_set_debounce(0), nuke.zoom(0.15, (cx, cy))]),
        ("D slider Grade.white all visible", lambda: gesture(slider_drag(grade, "white", 0.7, 0.3))),
        ("D zoom 4.0 on Grade", lambda: nuke.zoom(4.0, (grade.xpos(), grade.ypos()))),
        ("D slider Grade.white few visible", lambda: gesture(slider_drag(grade, "white"))),
        ("D set debounce 100 + zoom 1.0", lambda: [_set_debounce(100), nuke.zoom(1.0, (grade.xpos(), grade.ypos()))]),
        ("D close Grade panel", lambda: grade.hideControlPanel()),
        ("X timeslider scrub (40 moves)", lambda: gesture(timeslider_drag())),
        ("  -> frame", lambda: log("      frame = {}".format(nuke.frame()))),
        ("X timeslider scrub back (40 moves)", lambda: gesture(timeslider_drag(0.6, 0.2))),
        ("select one node (setSelected)", lambda: mid.setSelected(True)),
        ("select all (nuke.selectAll)", nuke.selectAll),
        ("deselect all", lambda: [n.setSelected(False) for n in nodes]),
        ("move node (setXYpos)", lambda: mid.setXYpos(mid.xpos() + 40, mid.ypos())),
        ("Grade.white knob change", lambda: grade["white"].setValue(1.7)),
        ("Grade.mix knob change", lambda: grade["mix"].setValue(0.55)),
        ("Read.file knob change", lambda: read["file"].setValue("/jobs/SHOW/x/plate_v009.%04d.exr")),
        ("Merge.operation change", lambda: merge["operation"].setValue("screen")),
        ("node label knob change", lambda: blur["label"].setValue("soft\nbloom")),
        ("node label knob cleared", lambda: blur["label"].setValue("")),
        ("rename node", lambda: blur.setName("Blur_renamed")),
        ("disable toggle on", lambda: blur["disable"].setValue(True)),
        ("disable toggle off", lambda: blur["disable"].setValue(False)),
        ("frame change (nuke.frame 1050)", lambda: nuke.frame(1050)),
        ("frame change (nuke.frame 1001)", lambda: nuke.frame(1001)),
        ("frame change (nuke.frame 1002)", lambda: nuke.frame(1002)),
        ("frame change (nuke.frame 1003)", lambda: nuke.frame(1003)),
        ("scrub 24 frames, 40ms apart", lambda: [QtCore.QTimer.singleShot(40 * i, lambda f=1010 + i: nuke.frame(f)) for i in range(24)]),
        ("connect viewer to node", lambda: nuke.connectViewer(0, merge)),
        ("connect viewer to another node", lambda: nuke.connectViewer(0, grade)),
        ("connect viewer input 1", lambda: nuke.connectViewer(1, blur)),
        ("disconnect input on Merge", lambda: merge.setInput(1, None)),
        ("reconnect input on Merge", lambda: merge.setInput(1, blur)),
        ("rewire Blur input 0 to a Read", lambda: blur.setInput(0, read)),
        ("connect Grade mask input", lambda: grade.setInput(1, read)),
        ("disconnect Grade mask input", lambda: grade.setInput(1, None)),
        ("insert node into chain (createNode)", lambda: [_select([grade]), state.__setitem__("ins", nuke.createNode("Blur", inpanel=False))]),
        ("delete mid-chain node (auto-rewire)", lambda: nuke.delete(state["ins"]) if "ins" in state else None),
        ("insert node via nodes.X + setInput", lambda: state.__setitem__("ins2", nuke.nodes.Blur(inputs=[grade]))),
        ("delete that node (setInput orphan)", lambda: nuke.delete(state["ins2"]) if "ins2" in state else None),
        ("control knob edit (expression fan-out)", lambda: ctrl["gain"].setValue(1.234) if ctrl else None),
        ("control knob edit again", lambda: ctrl["softness"].setValue(7.5) if ctrl else None),
        ("edit a cloned Grade", lambda: clone["white"].setValue(1.42) if clone else None),
        ("open a Group in the DAG", lambda: nuke.showDag(group) if group else None),
        ("edit knob inside the open Group", lambda: (group.node("Grade1") or group.nodes()[1])["white"].setValue(1.3) if group else None),
        ("back to root DAG", lambda: nuke.showDag(nuke.root()) if group else None),
        ("edit inside Group while root shown", lambda: (group.node("Grade1") or group.nodes()[1])["white"].setValue(1.4) if group else None),
        ("create node (nuke.nodes.Blur)", lambda: state.__setitem__("tmp", nuke.nodes.Blur(name="TmpBlur", xpos=int(cx), ypos=int(cy)))),
        ("delete that node", lambda: nuke.delete(state["tmp"])),
        ("open properties panel", lambda: grade.showControlPanel()),
        ("edit knob with panel open", lambda: grade["gamma"].setValue(1.21)),
        ("close properties panel", lambda: grade.hideControlPanel()),
        ("undo x2 (nuke.undo)", lambda: [nuke.undo(), nuke.undo()]),
        ("copy/paste 5 nodes", lambda: [_select(nodes[:5]), nuke.nodeCopy("%clipboard%"), nuke.nodePaste("%clipboard%")]),
        ("slider drag: white 1.1..1.5, 20ms apart", lambda: [QtCore.QTimer.singleShot(20 * i, lambda v=1.1 + 0.1 * i: grade["white"].setValue(v)) for i in range(5)]),
        ("  -> label shown vs knob value", lambda: _check_label(grade, "white")),
        ("two edits 50ms apart: white 3.1 then 3.2", lambda: [grade["white"].setValue(3.1), QtCore.QTimer.singleShot(50, lambda: grade["white"].setValue(3.2))]),
        ("  -> label shown vs knob value", lambda: _check_label(grade, "white")),
        ("two edits 50ms apart: mix 0.31 then 0.32", lambda: [grade["mix"].setValue(0.31), QtCore.QTimer.singleShot(50, lambda: grade["mix"].setValue(0.32))]),
        ("  -> label shown vs knob value", lambda: _check_label(grade, "mix")),
        ("slider drag: white 2.1..2.5, 200ms apart", lambda: [QtCore.QTimer.singleShot(200 * i, lambda v=2.1 + 0.1 * i: grade["white"].setValue(v)) for i in range(5)]),
        ("  -> label shown vs knob value", lambda: _check_label(grade, "white")),
        ("scriptClear", nuke.scriptClear),
    ]
    only = os.environ.get("LM_PROFILE_ONLY", "")
    if only:
        steps = [st for st in steps if any(k in st[0] for k in only.split(","))]
    it = iter(steps)

    def next_step():
        try:
            label, action = next(it)
        except StopIteration:
            finish()
            return
        measure(label, action, next_step)

    log("\nevent census (calls = label requests from Nuke; label_py = time in the Python label fn, stock or Labelmaker)")
    next_step()


def _poke(node, knob, alter, quiet=False):
    """Change a knob and revert it in the same callback, without undo."""
    k = node[knob]
    v = k.value()
    nuke.Undo.disable()
    try:
        k.setValue(alter(v))
        k.setValue(v)
    finally:
        nuke.Undo.enable()
    if not quiet:
        log("      {} = {!r}".format(knob, k.value()))


kc_count = {"n": 0, "names": {}}


def _counting_kc():
    kc_count["n"] += 1
    try:
        k = nuke.thisKnob().name()
    except Exception:
        k = "?"
    kc_count["names"][k] = kc_count["names"].get(k, 0) + 1


def _kc_report():
    top = sorted(kc_count["names"].items(), key=lambda kv: -kv[1])[:6]
    log("      knobChanged fired {} times: {}".format(kc_count["n"], top))
    kc_count.update(n=0, names={})


def _time_hashes(nodes):
    t0 = time.perf_counter()
    for n in nodes:
        n.opHashes()
    return (time.perf_counter() - t0) / len(nodes) * 1e6


def _noop_kc():
    return None


def _set_debounce(ms):
    log("      (debounce setting no longer exists; ms={})".format(ms))


def _check_label(node, knob):
    """What Nuke is drawing (Labelmaker's cache == last string it returned) vs the knob."""
    shown = autolabeller._shown.get(node.fullName(), "<no cache>")
    value = node[knob].value()
    ok = str(round(value, 2)) in shown or "{:.1f}".format(value) in shown
    log("      knob {}={!r}  label text={!r}  ->  {}".format(knob, value, shown.replace("\n", " / "), "OK" if ok else "STALE"))


def _select(sel):
    for n in nuke.allNodes():
        n.setSelected(False)
    for n in sel:
        n.setSelected(True)


def finish():
    if state["profiler"]:
        log("\ncProfile — script-open label pass, top 25 by internal time")
        sys.stdout = sys.__stderr__
        st = pstats.Stats(state["profiler"], stream=sys.stdout)
        st.sort_stats("tottime").print_stats(25)
        log("\ncProfile — top 25 by cumulative time")
        st.sort_stats("cumulative").print_stats(25)
        state["profiler"].dump_stats(PROF_OUT)
        log("wrote " + PROF_OUT)
    log("\nDONE")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


QtCore.QTimer.singleShot(4000, step_open)
