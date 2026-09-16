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
import bisect
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import labelmaker  # noqa: E402
import labelmaker_prefs  # noqa: E402
import lm_probe  # noqa: E402

labelmaker_prefs.prefs_singleton._prefs["deoverlap_enabled"] = DEOVERLAP
autolabeller = labelmaker.autolabeller_singleton
# not set_enabled(): its poke runs nuke.Undo.disable() while undo is still
# uninitialised (disabled() is True at load), and two of those unmatched
# disables leave the whole session without undo ("Undo::init() error")
if MODE == "off":
    autolabeller.unregister_autolabel()
else:
    autolabeller.register_autolabel()

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
autolabeller.register_autolabel()
autolabeller.invalidate_labels()
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
        cls = nuke.thisClass()
        entry = by_class.setdefault(cls, [0, 0.0])
        entry[0] += 1
        entry[1] += dt
        viewer_log = state.get("v_viewer_log")
        if viewer_log is not None and cls == "Viewer":
            viewer_log.append(dt)
        watch = state.get("v_watch")
        if watch is not None:
            name = nuke.thisNode().fullName()
            watch[name] = watch.get(name, 0) + 1


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


def gesture(steps, tick_ms=TICK_MS):
    """Run callables one per tick_ms from the event loop; record tick latency.

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
        state["ticks"].append(now - last["t"] - tick_ms / 1000.0)
        last["t"] = now
        try:
            next(it)()
        except StopIteration:
            state["busy"] = False
            return
        QtCore.QTimer.singleShot(tick_ms, tick)

    QtCore.QTimer.singleShot(tick_ms, tick)


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
        if state.get("busy") or getattr(autolabeller, "_verify", None):
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
    impl_stats["step_t0"] = time.perf_counter()
    getattr(autolabeller, "reset_stats", lambda: None)()
    t0 = time.perf_counter()
    try:
        action()
    except Exception as error:
        log("      (action failed: {})".format(str(error)[:120]))
    action_s = time.perf_counter() - t0

    def done(settle_s):
        calls = counter["calls"] - before
        lm = counter["seconds"] - before_s
        builds = sum(getattr(autolabeller, "_stats_calls_by_node", {}).values())
        hits = sum(getattr(autolabeller, "_stats_hits_by_node", {}).values())
        deov = getattr(autolabeller, "_stats_time", {}).get("deoverlap (timer)", 0.0)
        ticks = state.pop("ticks", None)
        results.append((label, calls, builds, hits, action_s, settle_s, lm, deov))
        state["last_ticks"] = ticks
        state["last_stats"] = dict(impl_stats)
        extra = ""
        if ticks and len(ticks) > 1:
            lat = sorted(t * 1000 for t in ticks[1:])
            extra = "  ticks={} lat mean={:.1f}ms p95={:.1f}ms max={:.1f}ms".format(
                len(lat), sum(lat) / len(lat), lat[int(len(lat) * 0.95) - 1], lat[-1])
        if deov:
            extra += "  deoverlap={:.1f}ms".format(deov * 1000)
        if state.get("impl", MODE) == "on":
            extra += "  builds={} pokes={} ({:.0f}ms) verified={} ({:.0f}ms)".format(
                impl_stats["builds"], impl_stats["pokes"], impl_stats["poke_s"] * 1000,
                impl_stats["verified"], impl_stats["verify_s"] * 1000)
            if impl_stats.get("first_release"):
                extra += " first_release={:.2f}s releases={}".format(
                    impl_stats["first_release"] - impl_stats["step_t0"], impl_stats["releases"])
            if impl_stats.get("slices"):
                extra += " slices={} span={:.2f}s".format(
                    impl_stats["slices"], impl_stats["slice_t1"] - impl_stats["slice_t0"])
                rec = impl_stats.get("slice_log", [])
                if len(rec) > 3:
                    gaps = sorted(r[4] for r in rec[1:])
                    extra += " gap median={:.0f}ms p95={:.0f}ms busy_exits={}".format(
                        gaps[len(gaps) // 2], gaps[int(len(gaps) * 0.95) - 1], sum(1 for r in rec if r[5]))
                    log("      first slices (work ms, n, gap ms, busy): " + " ".join(
                        "({:.0f},{},{:.0f},{})".format(r[2], r[3], r[4], int(r[5])) for r in rec[:12]))
        impl_stats.clear()
        impl_stats.update(builds=0, pokes=0, poke_s=0.0, verified=0, verify_s=0.0)
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
    # E deletes/undoes whole ranges; undo of a delete yields NEW C++ nodes, so
    # the wrappers must be re-resolved by name. Keep the named fixtures out of
    # the pool so `grade`, `mid` … stay valid for the groups that follow.
    specials = {id(n) for n in (grade, read, merge, blur, mid, cc, grade2, ctrl, group, clone) if n is not None}
    pool = [n for n in nodes if id(n) not in specials]
    pool_names = [n.fullName() for n in pool]

    def _reresolve():
        pool[:] = [nuke.toNode(nm) for nm in pool_names]
        missing = sum(n is None for n in pool)
        if missing:
            log("      ({} pool nodes not found)".format(missing))
            pool[:] = [n for n in pool if n is not None]
            pool_names[:] = [n.fullName() for n in pool]

    def _rename_pool(lo, hi, tag):
        for i in range(lo, hi):
            pool[i].setName("Ren_{}_{}".format(tag, i))
            pool_names[i] = pool[i].fullName()

    tcl_label = "[python {__import__('lm_probe').tick()}]"
    # clones share the label knob (one edit relabels two nodes) and an
    # expression-driven size ignores setValue, so neither can be counted
    tcl_blurs = [n for n in pool if n.Class() == "Blur" and not n.clones() and not n["size"].hasExpression()][:100]
    tcl_grades = [n for n in pool if n.Class() == "Grade" and not n.clones()][:100]
    tcl_nodes = tcl_blurs + tcl_grades
    tcl_grade = tcl_grades[0]
    # the cache paths under test (verify, release) only ever see pokeable nodes
    pokeable = [n for n in pool if n.knob("dope_sheet") is not None]
    cset, dset, eset = pokeable[300:400], pokeable[400:500], pokeable[500:600]
    fset, gset = pokeable[600:700], pokeable[700:800]
    victim_c, victim_e = cset[50], eset[50]
    deepest, depth = _deepest(nodes)
    # one Grade drives 50 others through an expression on a readout knob
    expr_pool = [n for n in pool if n.Class() == "Grade" and not n.clones()][100:]
    expr_pool = [n for n in expr_pool if not n["white"].hasExpression() and not n["white"].isAnimated()
                 and isinstance(n["white"].value(), float)]
    expr_src, expr_deps = expr_pool[0], expr_pool[1:51]
    # a multi-line label on an unlabelled node grows it; clones share the knob
    kset = [n for n in pokeable[800:] if not n.clones() and n.knob("label") is not None and not n["label"].value()][:300]

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
        ("M impl stock", lambda: use_impl("stock")),
        ("M stock hooks off", lambda: [nuke.removeOnCreate(autolabeller._on_node_created), nuke.removeOnDestroy(autolabeller._on_node_destroyed)] if "stock" == "stock" else None),
        ("M stock create 100 nodes (nuke.nodes.Blur)", lambda: state.__setitem__("made", [nuke.nodes.Blur(xpos=int(cx) + 20 * i, ypos=int(cy)) for i in range(100)])),
        ("M stock frame +1 after create", lambda: nuke.frame(nuke.frame() + 1)),
        ("M stock delete those 100", lambda: [nuke.delete(n) for n in state["made"]]),
        ("M stock undo delete (100 back)", lambda: nuke.undo()),
        ("M stock redo delete", lambda: nuke.redo()),
        ("M stock copy 100 + paste", lambda: [_select(nodes[:100]), nuke.nodeCopy("%clipboard%"), nuke.nodePaste("%clipboard%")]),
        ("M stock frame +1 after paste 100", lambda: nuke.frame(nuke.frame() + 1)),
        ("M stock delete pasted 100", lambda: [nuke.delete(n) for n in nuke.selectedNodes()]),
        ("M stock copy 1000 + paste", lambda: [_select(nodes[:1000]), nuke.nodeCopy("%clipboard%"), nuke.nodePaste("%clipboard%")]),
        ("M stock frame +1 after paste 1000", lambda: nuke.frame(nuke.frame() + 1)),
        ("M stock delete pasted 1000", lambda: [nuke.delete(n) for n in nuke.selectedNodes()]),
        ("M stock undo delete 1000", lambda: nuke.undo()),
        ("M stock redo delete 1000", lambda: nuke.redo()),
        ("M stock deselect", lambda: [n.setSelected(False) for n in nuke.allNodes()]),
        ("M stock playback 2s via viewer", lambda: _play(2.0)),
        ("M stock hooks on", lambda: [nuke.addOnCreate(autolabeller._on_node_created), nuke.addOnDestroy(autolabeller._on_node_destroyed)] if "stock" == "stock" else None),
        ("M impl on", lambda: use_impl("on")),
        ("M on hooks off", lambda: [nuke.removeOnCreate(autolabeller._on_node_created), nuke.removeOnDestroy(autolabeller._on_node_destroyed)] if "on" == "stock" else None),
        ("M on create 100 nodes (nuke.nodes.Blur)", lambda: state.__setitem__("made", [nuke.nodes.Blur(xpos=int(cx) + 20 * i, ypos=int(cy)) for i in range(100)])),
        ("M on frame +1 after create", lambda: nuke.frame(nuke.frame() + 1)),
        ("M on delete those 100", lambda: [nuke.delete(n) for n in state["made"]]),
        ("M on undo delete (100 back)", lambda: nuke.undo()),
        ("M on redo delete", lambda: nuke.redo()),
        ("M on copy 100 + paste", lambda: [_select(nodes[:100]), nuke.nodeCopy("%clipboard%"), nuke.nodePaste("%clipboard%")]),
        ("M on frame +1 after paste 100", lambda: nuke.frame(nuke.frame() + 1)),
        ("M on delete pasted 100", lambda: [nuke.delete(n) for n in nuke.selectedNodes()]),
        ("M on copy 1000 + paste", lambda: [_select(nodes[:1000]), nuke.nodeCopy("%clipboard%"), nuke.nodePaste("%clipboard%")]),
        ("M on frame +1 after paste 1000", lambda: nuke.frame(nuke.frame() + 1)),
        ("M on delete pasted 1000", lambda: [nuke.delete(n) for n in nuke.selectedNodes()]),
        ("M on undo delete 1000", lambda: nuke.undo()),
        ("M on redo delete 1000", lambda: nuke.redo()),
        ("M on deselect", lambda: [n.setSelected(False) for n in nuke.allNodes()]),
        ("M on playback 2s via viewer", lambda: _play(2.0)),
        ("M on hooks on", lambda: [nuke.addOnCreate(autolabeller._on_node_created), nuke.addOnDestroy(autolabeller._on_node_destroyed)] if "on" == "stock" else None),
        ("M on prefs-style disable (set_enabled False)", lambda: autolabeller.set_enabled(False)),
        ("M on prefs-style enable (set_enabled True)", lambda: autolabeller.set_enabled(True)),
        # set_enabled(True) put Labelmaker's own function in front of the timing wrapper
        ("M on re-wrap autolabel", lambda: [nuke.removeAutolabel(autolabeller.create_autolabel), nuke.removeAutolabel(_timed_autolabel), nuke.addAutolabel(_timed_autolabel)]),
        ("G impl on", lambda: use_impl("on")),
        ("G1 hooks off", lambda: [nuke.removeOnCreate(autolabeller._on_node_created), nuke.removeOnDestroy(autolabeller._on_node_destroyed)]),
        ("G1 off copy 1000 + paste", lambda: [_select(nodes[:1000]), nuke.nodeCopy("%clipboard%"), nuke.nodePaste("%clipboard%")]),
        ("G1 off delete pasted 1000", lambda: [nuke.delete(n) for n in nuke.selectedNodes()]),
        ("G1 off undo delete 1000", lambda: nuke.undo()),
        ("G1 off redo delete 1000", lambda: nuke.redo()),
        ("G1 off create 200 (nuke.nodes.Blur)", lambda: state.__setitem__("made", [nuke.nodes.Blur(xpos=int(cx) + 20 * i, ypos=int(cy)) for i in range(200)])),
        ("G1 off delete those 200", lambda: [nuke.delete(n) for n in state["made"]]),
        ("G1 off frame +1", lambda: nuke.frame(nuke.frame() + 1)),
        ("G1 hooks on", lambda: [nuke.addOnCreate(autolabeller._on_node_created), nuke.addOnDestroy(autolabeller._on_node_destroyed)]),
        ("G1 on copy 1000 + paste", lambda: [_select(nodes[:1000]), nuke.nodeCopy("%clipboard%"), nuke.nodePaste("%clipboard%")]),
        ("G1 on delete pasted 1000", lambda: [nuke.delete(n) for n in nuke.selectedNodes()]),
        ("G1 on undo delete 1000", lambda: nuke.undo()),
        ("G1 on redo delete 1000", lambda: nuke.redo()),
        ("G1 on create 200 (nuke.nodes.Blur)", lambda: state.__setitem__("made", [nuke.nodes.Blur(xpos=int(cx) + 20 * i, ypos=int(cy)) for i in range(200)])),
        ("G1 on delete those 200", lambda: [nuke.delete(n) for n in state["made"]]),
        ("G1 on frame +1", lambda: nuke.frame(nuke.frame() + 1)),
        ("G2 hooks off", lambda: [nuke.removeOnCreate(autolabeller._on_node_created), nuke.removeOnDestroy(autolabeller._on_node_destroyed)]),
        ("G2 off copy 1000 + paste", lambda: [_select(nodes[:1000]), nuke.nodeCopy("%clipboard%"), nuke.nodePaste("%clipboard%")]),
        ("G2 off delete pasted 1000", lambda: [nuke.delete(n) for n in nuke.selectedNodes()]),
        ("G2 off undo delete 1000", lambda: nuke.undo()),
        ("G2 off redo delete 1000", lambda: nuke.redo()),
        ("G2 off create 200 (nuke.nodes.Blur)", lambda: state.__setitem__("made", [nuke.nodes.Blur(xpos=int(cx) + 20 * i, ypos=int(cy)) for i in range(200)])),
        ("G2 off delete those 200", lambda: [nuke.delete(n) for n in state["made"]]),
        ("G2 off frame +1", lambda: nuke.frame(nuke.frame() + 1)),
        ("G2 hooks on", lambda: [nuke.addOnCreate(autolabeller._on_node_created), nuke.addOnDestroy(autolabeller._on_node_destroyed)]),
        ("G2 on copy 1000 + paste", lambda: [_select(nodes[:1000]), nuke.nodeCopy("%clipboard%"), nuke.nodePaste("%clipboard%")]),
        ("G2 on delete pasted 1000", lambda: [nuke.delete(n) for n in nuke.selectedNodes()]),
        ("G2 on undo delete 1000", lambda: nuke.undo()),
        ("G2 on redo delete 1000", lambda: nuke.redo()),
        ("G2 on create 200 (nuke.nodes.Blur)", lambda: state.__setitem__("made", [nuke.nodes.Blur(xpos=int(cx) + 20 * i, ypos=int(cy)) for i in range(200)])),
        ("G2 on delete those 200", lambda: [nuke.delete(n) for n in state["made"]]),
        ("G2 on frame +1", lambda: nuke.frame(nuke.frame() + 1)),
        ("S probe 0ms timer, no work", lambda: _probe_cadence(lambda: None, 60)),
        ("S probe runIn noop", lambda: _probe_cadence(lambda: nuke.runIn(pool[5].fullName(), "None"), 60)),
        ("S probe runIn thisNode().Class()", lambda: _probe_cadence(lambda: nuke.runIn(pool[5].fullName(), "nuke.thisNode().Class()"), 60)),
        ("S probe runIn nuke.expression(keys)", lambda: _probe_cadence(lambda: nuke.runIn(pool[5].fullName(), "nuke.expression('(keys?1:0)+(has_expression?2:0)')"), 60)),
        ("S probe runIn nuke.value(this.label)", lambda: _probe_cadence(lambda: nuke.runIn(pool[5].fullName(), "nuke.value('this.label', '')"), 60)),
        ("S probe runIn nuke.knob(this.indicators) write", lambda: _probe_cadence(lambda: nuke.runIn(pool[5].fullName(), "nuke.knob('this.indicators', nuke.value('this.indicators'))"), 60)),
        ("S probe compose_in_context x1/slice", lambda: _probe_cadence(lambda: autolabeller._compose_in_context(pool[5].fullName()), 60)),
        ("S probe compose_in_context x30/slice", lambda: _probe_cadence(lambda: [autolabeller._compose_in_context(n.fullName()) for n in pool[:30]], 30)),
        ("S probe compose_in_context x30/slice DISTINCT", lambda: _probe_cadence(_walk(lambda n: autolabeller._compose_in_context(n.fullName()), 30, pool), 40)),
        ("S probe runIn noop x30/slice DISTINCT", lambda: _probe_cadence(_walk(lambda n: nuke.runIn(n.fullName(), "None"), 30, pool), 40)),
        ("S probe runIn expression(keys) x30/slice DISTINCT", lambda: _probe_cadence(_walk(lambda n: nuke.runIn(n.fullName(), "nuke.expression('(keys?1:0)+(has_expression?2:0)')"), 30, pool), 40)),
        ("S probe runIn value(this.label) x30/slice DISTINCT", lambda: _probe_cadence(_walk(lambda n: nuke.runIn(n.fullName(), "nuke.value('this.label', '')"), 30, pool), 40)),
        ("S probe node['label'].value() x30/slice DISTINCT", lambda: _probe_cadence(_walk(lambda n: n["label"].value(), 30, pool), 40)),
        ("S probe compose_in_context x30/slice DISTINCT again", lambda: _probe_cadence(_walk(lambda n: autolabeller._compose_in_context(n.fullName()), 30, pool), 40)),
        ("S probe node.knob('label').value() x30", lambda: _probe_cadence(lambda: [n.knob("label").value() for n in pool[:30]], 30)),
        ("S probe nuke.toNode x30", lambda: _probe_cadence(lambda: [nuke.toNode(nm) for nm in pool_names[:30]], 30)),
        ("S timer cadence 0 ms x100", lambda: _timer_cadence(0, 100)),
        ("S timer cadence 1 ms x100", lambda: _timer_cadence(1, 100)),
        ("S timer cadence 4 ms x100", lambda: _timer_cadence(4, 100)),
        ("S timer cadence 15 ms x50", lambda: _timer_cadence(15, 50)),
        # S: verify-at-idle spike. Cost of the background verification after
        # the passes that now trigger it, interaction while it runs, and the
        # bulk-edit cases the burst heuristics got wrong — plus attempts to
        # break it (edits, deletes, renames, undo, group nodes, disable,
        # scriptClear while the verification queue is non-empty).
        *[st for impl in ("stock", "on") for st in (
        ("S impl " + impl, lambda impl=impl: use_impl(impl)),
        ("S {} stamps: {}".format(impl, os.environ.get("LM_STAMPS", "on")), lambda: [n["postage_stamp"].setValue(False) for n in nuke.allNodes(recurseGroups=True) if n.knob("postage_stamp")] if os.environ.get("LM_STAMPS") == "off" else None),
        ("S {} warm: frame +1".format(impl), lambda: nuke.frame(nuke.frame() + 1)),
        ("S {} probe after frame change".format(impl), lambda: _probe_cadence(_walk(lambda n: autolabeller._compose_in_context(n.fullName()), 30, pool), 40)),
        ("S {} probe again, quiet".format(impl), lambda: _probe_cadence(_walk(lambda n: autolabeller._compose_in_context(n.fullName()), 30, pool), 40)),
        ("S {} edit knob".format(impl), lambda: grade["white"].setValue(grade["white"].value() + 0.01)),
        ("S {} pass: frame +1 after edit".format(impl), lambda: nuke.frame(nuke.frame() + 1)),
        ("S {} edit knob ".format(impl), lambda: grade["white"].setValue(grade["white"].value() + 0.01)),
        ("S {} pass: frame +1 after edit (2)".format(impl), lambda: nuke.frame(nuke.frame() + 1)),
        ("S {} pass: viewer connect".format(impl), lambda: nuke.connectViewer(0, merge)),
        ("S {} pass: viewer connect other".format(impl), lambda: nuke.connectViewer(0, grade)),
        ("S {} scrub 24 frames, 40ms apart".format(impl), lambda: [QtCore.QTimer.singleShot(40 * i, lambda f=1010 + i: nuke.frame(f)) for i in range(24)]),
        ("S {} X timeslider scrub (40 moves)".format(impl), lambda: gesture(timeslider_drag())),
        ("S {} open Grade panel + zoom".format(impl), lambda: [grade.showControlPanel(), nuke.zoom(1.0, (grade.xpos(), grade.ypos()))]),
        ("S {} pass then immediate slider drag".format(impl), lambda: [nuke.connectViewer(0, blur), gesture(slider_drag(grade, "white"))]),
        ("S {} slider drag (quiet)".format(impl), lambda: gesture(slider_drag(grade, "white", 0.7, 0.3))),
        ("S {}  -> white shown".format(impl), lambda: _check_label(grade, "white")),
        ("S {} close panel".format(impl), lambda: grade.hideControlPanel()),
        ("S {} bulk label on 100".format(impl), lambda: [n["label"].setValue("s100") for n in pool[:100]]),
        ("S {}  -> shown".format(impl), lambda: _check_bulk(pool[:100], "s100")),
        ("S {} bulk label on 1000".format(impl), lambda: [n["label"].setValue("s1k") for n in pool[:1000]]),
        ("S {}  -> shown ".format(impl), lambda: _check_bulk(pool[:1000], "s1k")),
        ("S {} bulk label on ALL".format(impl), lambda: [n["label"].setValue("sall") for n in pool]),
        ("S {}  -> shown  ".format(impl), lambda: _check_bulk(pool, "sall")),
        ("S {} bulk label 1000 + frame +1 same callback".format(impl), lambda: [[n["label"].setValue("sfr") for n in pool[:1000]], nuke.frame(nuke.frame() + 1)]),
        ("S {}  -> shown   ".format(impl), lambda: _check_bulk(pool[:1000], "sfr")),
        ("S {} bulk label 1000 + viewer connect same callback".format(impl), lambda: [[n["label"].setValue("svw") for n in pool[:1000]], nuke.connectViewer(0, merge)]),
        ("S {}  -> shown    ".format(impl), lambda: _check_bulk(pool[:1000], "svw")),
        ("S {} bulk label 1000 then delete 100 of them at once".format(impl), lambda: [[n["label"].setValue("sdel") for n in pool[:1000]], _undoable("del", lambda: [_select(pool[900:1000]), nuke.nodeDelete()])]),
        ("S {}  -> shown     ".format(impl), lambda: _check_bulk(pool[:900], "sdel")),
        ("S {} undo that delete".format(impl), lambda: nuke.undo()),
        ("S {} re-resolve".format(impl), _reresolve),
        ("S {} bulk label 1000 then rename 100 of them at once".format(impl), lambda: [[n["label"].setValue("sren") for n in pool[:1000]], _rename_pool(800, 900, "S" + impl)]),
        ("S {}  -> shown      ".format(impl), lambda: _check_bulk(pool[:1000], "sren")),
        ("S {} bulk label 1000 in undo group".format(impl), lambda: _undoable("bulk", lambda: [n["label"].setValue("sundo") for n in pool[:1000]])),
        ("S {}  -> shown       ".format(impl), lambda: _check_bulk(pool[:1000], "sundo")),
        ("S {} undo the bulk edit".format(impl), lambda: nuke.undo()),
        ("S {}  -> shown        ".format(impl), lambda: _check_bulk(pool[:1000], "sundo", expect=0)),
        ("S {} bulk label inside Group (all its nodes)".format(impl), lambda: [n["label"].setValue("sgrp") for n in group.nodes() if n.knob("label")] if group else None),
        ("S {}  -> shown         ".format(impl), lambda: _check_bulk([n for n in group.nodes() if n.knob("label")], "sgrp") if group else None),
        ("S {} open the Group in the DAG".format(impl), lambda: nuke.showDag(group) if group else None),
        ("S {}  -> shown          ".format(impl), lambda: _check_bulk([n for n in group.nodes() if n.knob("label")], "sgrp") if group else None),
        ("S {} back to root".format(impl), lambda: nuke.showDag(nuke.root()) if group else None),
        ("S {} bulk label 1000 then set_enabled(False) at once".format(impl), lambda: [[n["label"].setValue("sdis") for n in pool[:1000]], autolabeller.set_enabled(False)]),
        ("S {} set_enabled(True) + re-wrap".format(impl), lambda: [autolabeller.set_enabled(True), nuke.removeAutolabel(autolabeller.create_autolabel), nuke.removeAutolabel(_timed_autolabel), nuke.addAutolabel(_timed_autolabel)]),
        ("S {} frame +1 (cold after enable)".format(impl), lambda: nuke.frame(nuke.frame() + 1)),
        ("S {}  -> shown           ".format(impl), lambda: _check_bulk(pool[:1000], "sdis")),
        ("S {} bulk label cleared on ALL".format(impl), lambda: [n["label"].setValue("") for n in pool]),
        ("S {}  -> shown            ".format(impl), lambda: _check_bulk(pool, "sdis", expect=0)),
        )],
        ("S on bulk label 1000 then scriptClear at once", lambda: [[n["label"].setValue("sclr") for n in pool[:1000]], nuke.scriptClear()]),
        ("S on  -> queue after scriptClear", lambda: log("      verify={} stale={} content={}".format(len(autolabeller._verify), len(autolabeller._stale), len(autolabeller._content)))),
        ("S on scriptOpen again", lambda: [_rm_autosave(), nuke.scriptOpen(SCRIPT)]),
        ("S on  -> queue after scriptOpen", lambda: log("      verify={} stale={} content={}".format(len(autolabeller._verify), len(autolabeller._stale), len(autolabeller._content)))),
        # V: label-cache edge cases (Tcl side effects, verify failure, undo
        # state, a failing poke). lm_probe counts how often each impl
        # substitutes a [python] label knob, so a pass that reads the cache
        # but must not run the knob's Tcl shows 0.
        *[st for impl in ("stock", "on") for st in (
        ("V impl " + impl, lambda impl=impl: [use_impl(impl), state.setdefault("v_frame0", nuke.frame()), state.setdefault("v_white0", grade["white"].value())]),
        ("V {}  -> undo alive at group start".format(impl), lambda: log("      Undo.disabled()={} (want False)  ->  {}".format(nuke.Undo.disabled(), "MISMATCH" if nuke.Undo.disabled() else "OK"))),
        ("V {} warm: edit + frame +1 (whole-script pass)".format(impl), lambda: [grade["white"].setValue(grade["white"].value() + 0.01), nuke.frame(nuke.frame() + 1)]),
        ("V {} (a) set [python] label on 200".format(impl), lambda: [_tcl_mark(), _v_set_labels(tcl_nodes, tcl_label)]),
        ("V {} (a)  -> tcl execs: set label".format(impl), lambda impl=impl: [_tcl_check(impl, "set label on 200", want=200), _check_bulk(tcl_nodes, "probe")]),
        ("V {} (a) pass: frame +1".format(impl), lambda: [_tcl_mark(), nuke.frame(nuke.frame() + 1)]),
        ("V {} (a)  -> tcl execs: frame +1".format(impl), lambda impl=impl: _tcl_check(impl, "frame +1 pass")),
        ("V {} (a) pass: viewer connect".format(impl), lambda impl=impl: [_tcl_mark(), nuke.connectViewer(0, grade if impl == "stock" else merge)]),
        ("V {} (a)  -> tcl execs: viewer connect".format(impl), lambda impl=impl: _tcl_check(impl, "viewer connect pass")),
        ("V {} (a) bulk edit label on the 200".format(impl), lambda: [_tcl_mark(), _v_set_labels(tcl_nodes, tcl_label + " v2")]),
        ("V {} (a)  -> tcl execs: bulk label edit".format(impl), lambda impl=impl: [_tcl_check(impl, "bulk label edit on 200", want=200), _check_bulk(tcl_nodes, "probe v2")]),
        ("V {} (a) open panel of a [python] Grade + zoom".format(impl), lambda: [tcl_grade.showControlPanel(), nuke.zoom(1.0, (tcl_grade.xpos(), tcl_grade.ypos()))]),
        ("V {} (a) X slider drag its white (40 moves)".format(impl), lambda: [_tcl_mark(), gesture(slider_drag(tcl_grade, "white"))]),
        ("V {} (a)  -> tcl execs: slider drag".format(impl), lambda impl=impl: [_tcl_check(impl, "slider drag"), _check_label(tcl_grade, "white") if impl == "on" else None]),
        ("V {} (a) close panel".format(impl), lambda: tcl_grade.hideControlPanel()),
        ("V {} (b) edit size on 100 [python] Blurs".format(impl), lambda: [_tcl_mark(), _v_set_sizes(tcl_blurs, 7.25)]),
        ("V {} (b)  -> readout shown, tcl execs".format(impl), lambda impl=impl: [_tcl_check(impl, "readout edit on 100", want=100), _check_bulk(tcl_blurs, "size 7.250")]),
        ("V {} (a/b) restore labels and sizes".format(impl), lambda: [_tcl_mark(), _v_restore_labels(tcl_nodes), _v_restore_sizes(tcl_blurs)]),
        ("V {} (a/b)  -> tcl execs: restore".format(impl), lambda impl=impl: [_tcl_check(impl, "restore"), _check_bulk(tcl_nodes, "probe", expect=0)]),
        ("V {} (c) install failing _compose_label for one node".format(impl), lambda: _v_fail_compose(victim_c)),
        ("V {} (c) bulk label on 100 incl. that node".format(impl), lambda: _v_set_labels(cset, "vfail")),
        ("V {} (c)  -> verify drained, others released".format(impl), lambda impl=impl: _v_check_c(impl, cset, victim_c)),
        ("V {} (c) restore labels".format(impl), lambda: _v_restore_labels(cset)),
        ("V {} (d) Undo.disable + bulk label on 100".format(impl), lambda: [state.__setitem__("v_undo_before", nuke.Undo.disabled()), nuke.Undo.disable(), _v_set_labels(dset, "vundo")]),
        ("V {} (d)  -> Undo still disabled after release".format(impl), lambda: _v_check_d(dset)),
        ("V {} (d) restore labels".format(impl), lambda: _v_restore_labels(dset)),
        ("V {} (e) install failing poke for one node".format(impl), lambda: _v_fail_poke(victim_e)),
        ("V {} (e) bulk label on 100 incl. that node".format(impl), lambda: _v_set_labels(eset, "vpoke")),
        ("V {} (e)  -> others released despite the failed poke".format(impl), lambda impl=impl: _v_check_e(impl, eset, victim_e)),
        ("V {} (e) restore labels".format(impl), lambda: _v_restore_labels(eset)),
        ("V {} (f) pane closed: bulk label on 100".format(impl), lambda: _v_set_labels(fset, "vpane")),
        ("V {} (f)  -> shown, pane closed".format(impl), lambda: [_v_check_pane(False), _check_bulk(fset, "vpane"), _v_pane_snapshot("closed")]),
        ("V {} (f) restore labels (closed)".format(impl), lambda: _v_restore_labels(fset)),
        ("V {} (f) float the Dope Sheet beside the DAG".format(impl), _v_show_dope_sheet),
        ("V {} (f) pane open: bulk label on 100".format(impl), lambda: _v_set_labels(fset, "vpane2")),
        ("V {} (f)  -> shown, pane open vs closed".format(impl), lambda: [_v_check_pane(True), _check_bulk(fset, "vpane2"), _v_pane_snapshot("open"), _v_check_f()]),
        ("V {} (f) restore labels (open)".format(impl), lambda: _v_restore_labels(fset)),
        ("V {} (f) hide the floated Dope Sheet".format(impl), _v_hide_dope_sheet),
        ("V {} (f)  -> pane closed again".format(impl), lambda: _v_check_pane(False)),
        ("V {} (g) record viewer, connect to deepest chain ({} deep)".format(impl, depth), lambda: [_v_record_viewer(), nuke.connectViewer(0, deepest)]),
        ("V {} (g) bulk label on 100 + playback 3s".format(impl), lambda: [_v_set_labels(gset, "vplay"), _v_playback(3.0)]),
        ("V {} (g)  -> settled after playback, shown".format(impl), lambda: [_v_check_g(), _check_bulk(gset, "vplay")]),
        ("V {} (g) restore labels".format(impl), lambda: _v_restore_labels(gset)),
        ("V {} (g2) bulk label on 100 + frame-step 3s".format(impl), lambda: [_v_set_labels(gset, "vstep"), _v_playback(3.0, step_frames=True)]),
        ("V {} (g2)  -> settled after stepping, shown".format(impl), lambda: [_v_check_g(), _check_bulk(gset, "vstep")]),
        ("V {} (g2) restore labels".format(impl), lambda: _v_restore_labels(gset)),
        ("V {} (h) wire viewer inputs 0/1".format(impl), lambda: [nuke.connectViewer(0, merge), nuke.connectViewer(1, grade)]),
        ("V {} (h) switch viewer input 20x, 40 ms apart".format(impl), lambda: [state.__setitem__("v_viewer_log", []), gesture([lambda i=i: nuke.connectViewer(i % 2, (merge, grade)[i % 2]) for i in range(20)], tick_ms=40)]),
        ("V {} (h)  -> Viewer label calls + per-call time".format(impl), lambda impl=impl: _v_check_h(impl)),
        ("V {} (g/h) restore viewer inputs".format(impl), _v_restore_viewer),
        ("V {} (i) create LiveGroup(20) / Precomp(5) / Group with onCreate Tcl".format(impl), _v_make_groups),
        ("V {} (i) dirty: white=1.5 on every inner Grade (root shown)".format(impl), lambda: _v_edit_inner(1.5)),
        ("V {} (i) warm: show LiveGroup DAG".format(impl), lambda: _v_show_group(0)),
        ("V {} (i) warm: LiveGroup back to root".format(impl), lambda: _v_leave_group(0)),
        ("V {} (i) warm: show Precomp DAG".format(impl), lambda: _v_show_group(1)),
        ("V {} (i) warm: Precomp back to root".format(impl), lambda: _v_leave_group(1)),
        ("V {} (i) warm: show onCreate Group DAG".format(impl), lambda: _v_show_group(2)),
        ("V {} (i) warm: Group back to root".format(impl), lambda: _v_leave_group(2)),
        ("V {} (i) bulk edit white=2.5 on every inner Grade (root shown)".format(impl), lambda: _v_edit_inner(2.5)),
        ("V {} (i) show LiveGroup DAG".format(impl), lambda: _v_show_group(0)),
        ("V {} (i)  -> LiveGroup inner labels".format(impl), lambda impl=impl: _v_check_i(impl, 0)),
        ("V {} (i) show Precomp DAG".format(impl), lambda: _v_show_group(1)),
        ("V {} (i)  -> Precomp inner labels".format(impl), lambda impl=impl: _v_check_i(impl, 1)),
        ("V {} (i) show onCreate Group DAG".format(impl), lambda: _v_show_group(2)),
        ("V {} (i)  -> onCreate Group inner labels".format(impl), lambda impl=impl: _v_check_i(impl, 2)),
        ("V {} (i)  -> verify warnings; delete the groups".format(impl), lambda impl=impl: _v_finish_i(impl)),
        ("V {} (j) link white on 50 Grades to {}.white".format(impl, expr_src.name()), lambda: _v_link_white(expr_deps, expr_src)),
        ("V {} (j) edit the source once".format(impl), lambda: [_v_watch(expr_deps), expr_src["white"].setValue(2.75)]),
        ("V {} (j)  -> dependents before the pass".format(impl), lambda impl=impl: _v_check_j(impl, expr_deps, "2.750", before=True)),
        ("V {} (j) pass: frame +1".format(impl), lambda: [_v_watch(expr_deps), nuke.frame(nuke.frame() + 1)]),
        ("V {} (j)  -> dependents after the pass".format(impl), lambda impl=impl: _v_check_j(impl, expr_deps, "2.750", before=False)),
        ("V {} (j) clear expressions + restore values".format(impl), _v_unlink_white),
        ("V {} (j)  -> restored".format(impl), lambda: _check_bulk(expr_deps, "2.750", expect=0)),
        *((
        ("V {} (k) bulk 4-line label on 300 (de-overlap on)".format(impl), lambda: _v_deov_edit(kset)),
        ("V {} (k)  -> pending fed by the pokes, overlaps".format(impl), lambda impl=impl: _v_check_k(impl, kset)),
        ("V {} (k) restore labels".format(impl), lambda: _v_restore_labels(kset)),
        ("V {} (k) restore layout".format(impl), _v_deov_restore_layout),
        ) if DEOVERLAP else (
        ("V {} (k) de-overlap interplay".format(impl), lambda: log("      (skipped: LM_PROFILE_DEOVERLAP=0)")),
        )),
        ("V {} restore grade.white + frame".format(impl), lambda: [grade["white"].setValue(state["v_white0"]), nuke.frame(state["v_frame0"])]),
        )],
        # Z: do label requests grow with the undo history? 3 identical
        # paste/delete/undo/redo cycles per implementation, node count logged
        *[st for impl in ("stock", "on") for cyc in (1, 2, 3) for st in (
        ("Z{} impl {}".format(cyc, impl), lambda impl=impl: use_impl(impl)),
        ("Z{} {} copy 1000 + paste".format(cyc, impl), lambda: [_select(nodes[:1000]), nuke.nodeCopy("%clipboard%"), nuke.nodePaste("%clipboard%")]),
        ("Z{} {} delete pasted 1000".format(cyc, impl), lambda: [nuke.delete(n) for n in nuke.selectedNodes()]),
        ("Z{} {} undo delete 1000".format(cyc, impl), lambda: nuke.undo()),
        ("Z{} {} redo delete 1000".format(cyc, impl), lambda: nuke.redo()),
        ("Z{} {} frame +1".format(cyc, impl), lambda: nuke.frame(nuke.frame() + 1)),
        ("Z{} {} frame +1 again".format(cyc, impl), lambda: nuke.frame(nuke.frame() + 1)),
        ("Z{} {}  -> node count".format(cyc, impl), lambda: log("      allNodes={} (recurse {})  cache entries={}".format(len(nuke.allNodes()), len(nuke.allNodes(recurseGroups=True)), len(autolabeller._content)))),
        )],
        # G3: same ops with EMPTY callbacks registered instead of Labelmaker's —
        # is the per-node cost Nuke's callback dispatch or _forget()?
        ("G3 hooks off, empty cbs on", lambda: [nuke.removeOnCreate(autolabeller._on_node_created), nuke.removeOnDestroy(autolabeller._on_node_destroyed), nuke.addOnCreate(_noop_cb), nuke.addOnDestroy(_noop_cb)]),
        ("G3 empty copy 1000 + paste", lambda: [_select(nodes[:1000]), nuke.nodeCopy("%clipboard%"), nuke.nodePaste("%clipboard%")]),
        ("G3 empty delete pasted 1000", lambda: [nuke.delete(n) for n in nuke.selectedNodes()]),
        ("G3 empty undo delete 1000", lambda: nuke.undo()),
        ("G3 empty redo delete 1000", lambda: nuke.redo()),
        ("G3 empty create 200 (nuke.nodes.Blur)", lambda: state.__setitem__("made", [nuke.nodes.Blur(xpos=int(cx) + 20 * i, ypos=int(cy)) for i in range(200)])),
        ("G3 empty delete those 200", lambda: [nuke.delete(n) for n in state["made"]]),
        ("G3 empty frame +1", lambda: nuke.frame(nuke.frame() + 1)),
        ("G3 empty cbs off, hooks on", lambda: [nuke.removeOnCreate(_noop_cb), nuke.removeOnDestroy(_noop_cb), nuke.addOnCreate(autolabeller._on_node_created), nuke.addOnDestroy(autolabeller._on_node_destroyed)]),
        # E: the remaining one-vs-many edit paths (cut, bulk wiring, bulk
        # text-changing edits, grouping, autoplace, save, nameless nodes)
        *[st for impl in ("stock", "on") for st in (
        ("E impl " + impl, lambda impl=impl: use_impl(impl)),
        ("E {} cut 1 (copy+delete)".format(impl), lambda: [_select([pool[len(pool) // 2]]), nuke.nodeCopy("%clipboard%"), nuke.nodeDelete()]),
        ("E {} paste 1 back".format(impl), lambda: nuke.nodePaste("%clipboard%")),
        ("E {} re-resolve after paste 1".format(impl), _reresolve),
        ("E {} cut 100 (copy+delete)".format(impl), lambda: [_select(pool[100:200]), nuke.nodeCopy("%clipboard%"), nuke.nodeDelete()]),
        ("E {} paste 100 back".format(impl), lambda: nuke.nodePaste("%clipboard%")),
        ("E {} re-resolve after paste 100".format(impl), _reresolve),
        ("E {} cut 1000 (copy+delete)".format(impl), lambda: [_select(pool[:1000]), nuke.nodeCopy("%clipboard%"), nuke.nodeDelete()]),
        ("E {} paste 1000 back".format(impl), lambda: nuke.nodePaste("%clipboard%")),
        ("E {} re-resolve after paste 1000".format(impl), _reresolve),
        ("E {} frame +1 after paste 1000".format(impl), lambda: nuke.frame(nuke.frame() + 1)),
        ("E {} rewire 1 (setInput)".format(impl), lambda: blur.setInput(0, read)),
        ("E {} rewire 200 (setInput 0 -> Read)".format(impl), lambda: _undoable("rewire", lambda: [n.setInput(0, read) for n in pool[200:400] if n.inputs() and n.Class() not in ("Read", "Constant")])),
        ("E {} undo rewire 200".format(impl), lambda: nuke.undo()),
        ("E {} delete 200 mid-chain (auto-rewire)".format(impl), lambda: _undoable("delete", lambda: [_select([n for n in pool[400:600] if n.inputs() and n.dependent()]), nuke.nodeDelete()])),
        ("E {} undo delete 200".format(impl), lambda: nuke.undo()),
        ("E {} re-resolve node wrappers ".format(impl), _reresolve),
        ("E {} disable 1 (text unchanged)".format(impl), lambda: grade["disable"].setValue(True)),
        ("E {} enable 1".format(impl), lambda: grade["disable"].setValue(False)),
        ("E {} disable 1000 selected".format(impl), lambda: [n["disable"].setValue(True) for n in pool[:1000] if n.knob("disable")]),
        ("E {} enable 1000".format(impl), lambda: [n["disable"].setValue(False) for n in pool[:1000] if n.knob("disable")]),
        ("E {} label knob on 1 (text changes)".format(impl), lambda: grade["label"].setValue("bulk")),
        ("E {}  -> shown".format(impl), lambda: _check_bulk([grade], "bulk")),
        ("E {} label knob on 100".format(impl), lambda: [n["label"].setValue("bulk") for n in pool[:100]]),
        ("E {}  -> shown ".format(impl), lambda: _check_bulk(pool[:100], "bulk")),
        ("E {} label knob on 1000".format(impl), lambda: [n["label"].setValue("bulk") for n in pool[:1000]]),
        ("E {}  -> shown  ".format(impl), lambda: _check_bulk(pool[:1000], "bulk")),
        ("E {} label knob cleared on 1000".format(impl), lambda: [n["label"].setValue("") for n in pool[:1000]]),
        ("E {}  -> shown   ".format(impl), lambda: _check_bulk(pool[:1000], "bulk", expect=0)),
        ("E {} rename 200".format(impl), lambda impl=impl: _rename_pool(600, 800, impl)),
        ("E {} tile_color on 1000".format(impl), lambda: [n["tile_color"].setValue(0x55555500) for n in pool[:1000]]),
        ("E {} autoplace 1000 selected".format(impl), lambda: _undoable("autoplace", lambda: [_select(pool[:1000]), [nuke.autoplace(n) for n in pool[:1000]]])),
        ("E {} undo autoplace".format(impl), lambda: nuke.undo()),
        ("E {} deselect".format(impl), lambda: [n.setSelected(False) for n in nuke.allNodes()]),
        ("E {} create 100 Dots (nameless)".format(impl), lambda: state.__setitem__("made", [nuke.nodes.Dot(xpos=int(cx) + 20 * i, ypos=int(cy) + 60) for i in range(100)])),
        ("E {} delete those Dots".format(impl), lambda: [nuke.delete(n) for n in state["made"]]),
        ("E {} create 20 Backdrops".format(impl), lambda: state.__setitem__("made", [nuke.nodes.BackdropNode(xpos=int(cx) + 40 * i, ypos=int(cy) + 120, bdwidth=200, bdheight=100) for i in range(20)])),
        ("E {} delete those Backdrops".format(impl), lambda: [nuke.delete(n) for n in state["made"]]),
        ("E {} scriptSave (tmp copy)".format(impl), lambda: nuke.scriptSaveAs(SCRIPT + ".saved.nk", overwrite=1)),
        ("E {} frame +1 after save".format(impl), lambda: nuke.frame(nuke.frame() + 1)),
        )],
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
        ("M hooks off + scriptClear", lambda: [nuke.removeOnCreate(autolabeller._on_node_created), nuke.removeOnDestroy(autolabeller._on_node_destroyed), nuke.scriptClear()]),
        ("M hooks off + scriptOpen", lambda: [_rm_autosave(), nuke.scriptOpen(SCRIPT)]),
        ("M hooks on + scriptClear", lambda: [nuke.addOnCreate(autolabeller._on_node_created), nuke.addOnDestroy(autolabeller._on_node_destroyed), nuke.scriptClear()]),
        ("M hooks on + scriptOpen", lambda: [_rm_autosave(), nuke.scriptOpen(SCRIPT)]),
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


def _walk(fn, per_tick, nodes_list):
    """fn() over a fresh window of `per_tick` nodes on every tick."""
    pos = [0]

    def step():
        window = nodes_list[pos[0]:pos[0] + per_tick]
        pos[0] = (pos[0] + per_tick) % max(1, len(nodes_list) - per_tick)
        for node in window:
            fn(node)

    return step


def _probe_cadence(fn, n):
    """Wall time per event-loop round trip when each 0 ms tick does fn()."""
    state["busy"] = True
    stamps = [time.perf_counter()]
    work = [0.0]

    def tick():
        t0 = time.perf_counter()
        fn()
        work[0] += time.perf_counter() - t0
        stamps.append(time.perf_counter())
        if len(stamps) <= n:
            QtCore.QTimer.singleShot(0, tick)
            return
        gaps = sorted((b - a) * 1000 for a, b in zip(stamps, stamps[1:]))
        log("      x{}: work {:.2f} ms/tick, round trip mean {:.1f} ms  median {:.1f}  p95 {:.1f}  max {:.1f}".format(
            n, work[0] / n * 1000, sum(gaps) / len(gaps), gaps[len(gaps) // 2], gaps[int(len(gaps) * 0.95) - 1], gaps[-1]))
        state["busy"] = False

    QtCore.QTimer.singleShot(0, tick)


def _timer_cadence(ms, n):
    """How fast Nuke's event loop services a chain of single-shot timers."""
    state["busy"] = True
    stamps = [time.perf_counter()]

    def tick():
        stamps.append(time.perf_counter())
        if len(stamps) <= n:
            QtCore.QTimer.singleShot(ms, tick)
            return
        gaps = sorted((b - a) * 1000 for a, b in zip(stamps, stamps[1:]))
        log("      {} ms timer x{}: mean {:.1f} ms  median {:.1f}  p95 {:.1f}  max {:.1f}".format(
            ms, n, sum(gaps) / len(gaps), gaps[len(gaps) // 2], gaps[int(len(gaps) * 0.95) - 1], gaps[-1]))
        state["busy"] = False

    QtCore.QTimer.singleShot(ms, tick)


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


def _play(seconds):
    """Viewer playback (not a scrub): start, stop after `seconds` from the event loop."""
    viewer = nuke.activeViewer()
    if viewer is None:
        log("      (no active viewer)")
        return
    try:
        viewer.play(1)
    except Exception as error:
        log("      (play failed: {})".format(error))
        return
    state["busy"] = True
    state["ticks"] = []
    last = {"t": time.perf_counter(), "n": 0}

    def tick():
        now = time.perf_counter()
        state["ticks"].append(now - last["t"] - TICK_MS / 1000.0)
        last["t"] = now
        last["n"] += 1
        if last["n"] * TICK_MS / 1000.0 >= seconds:
            viewer.stop()
            state["busy"] = False
            return
        QtCore.QTimer.singleShot(TICK_MS, tick)

    QtCore.QTimer.singleShot(TICK_MS, tick)


def _rm_autosave():
    for suffix in (".autosave",):
        path = SCRIPT + suffix
        if os.path.exists(path):
            os.remove(path)


impl_stats = {"builds": 0, "pokes": 0, "poke_s": 0.0, "verified": 0, "verify_s": 0.0}
_orig_compose_in_context = autolabeller._compose_in_context


def _counting_compose_in_context(full_name):
    t0 = time.perf_counter()
    try:
        return _orig_compose_in_context(full_name)
    finally:
        impl_stats["verified"] += 1
        impl_stats["verify_s"] += time.perf_counter() - t0


autolabeller._compose_in_context = _counting_compose_in_context
_orig_verify_slice = autolabeller._verify_slice


def _counting_verify_slice():
    now = time.perf_counter()
    impl_stats["slices"] = impl_stats.get("slices", 0) + 1
    impl_stats.setdefault("slice_t0", now)
    impl_stats["slice_t1"] = now
    before = len(autolabeller._verify)
    busy = autolabeller._busy()
    try:
        return _orig_verify_slice()
    finally:
        end = time.perf_counter()
        rec = impl_stats.setdefault("slice_log", [])
        gap = (now - rec[-1][1]) * 1000 if rec else 0.0
        rec.append((now, end, (end - now) * 1000, before - len(autolabeller._verify), gap, busy))


autolabeller._verify_slice = _counting_verify_slice
_orig_build_label = autolabeller._build_label
_orig_poke_nodes = autolabeller._poke_nodes


def _counting_build_label():
    impl_stats["builds"] += 1
    return _orig_build_label()


def _counting_poke_nodes(full_names, force=True, **kwargs):
    impl_stats["pokes"] += len(full_names)
    if full_names:
        impl_stats.setdefault("first_release", time.perf_counter())
        impl_stats["releases"] = impl_stats.get("releases", 0) + 1
    t0 = time.perf_counter()
    try:
        return _orig_poke_nodes(full_names, force=force, **kwargs)
    finally:
        impl_stats["poke_s"] += time.perf_counter() - t0


autolabeller._build_label = _counting_build_label
autolabeller._poke_nodes = _counting_poke_nodes
_orig_run_deoverlap = autolabeller._run_deoverlap


def _counting_run_deoverlap():
    rec = state.get("v_deov")
    if rec is not None:
        rec["fires"].append((len(autolabeller._pending_deoverlap), len(_v_overlaps(rec["names"]))))
    return _orig_run_deoverlap()


# installed before the timer exists: it connects to whatever _run_deoverlap
# is bound at its first creation
autolabeller._run_deoverlap = _counting_run_deoverlap


def _noop_kc():
    return None


def _noop_cb():
    return None


def _undoable(name, fn):
    """Python-driven edits only get an undo entry inside an explicit group."""
    undo = nuke.Undo()
    undo.begin(name)
    try:
        return fn()
    finally:
        undo.end()


def _set_debounce(ms):
    log("      (debounce setting no longer exists; ms={})".format(ms))


def _check_label(node, knob):
    """What Nuke is drawing (Labelmaker's cache == last string it returned) vs the knob."""
    shown = autolabeller._shown.get(node.fullName(), "<no cache>")
    value = node[knob].value()
    ok = str(round(value, 2)) in shown or "{:.1f}".format(value) in shown
    log("      knob {}={!r}  label text={!r}  ->  {}".format(knob, value, shown.replace("\n", " / "), "OK" if ok else "STALE"))


def _check_bulk(sel, needle, expect=None):
    """After a bulk edit: how many of `sel` show `needle` (Labelmaker's cache
    == the last string Nuke was given), plus what is still marked stale."""
    if state.get("impl", MODE) != "on":
        log("      (stock: no cache to inspect)")
        return
    shown = [autolabeller._shown.get(n.fullName(), "") for n in sel]
    n_has = sum(needle in s for s in shown)
    n_none = sum(1 for n in sel if n.fullName() not in autolabeller._shown)
    want = len(sel) if expect is None else expect
    log("      {}/{} show {!r} (want {}), {} not in cache, stale={} forced={} verify={}  ->  {}".format(
        n_has, len(sel), needle, want, n_none, len(autolabeller._stale), len(autolabeller._forced),
        len(autolabeller._verify), "OK" if n_has == want and not autolabeller._stale else "MISMATCH"))


def _tcl_mark():
    state["v_tcl_mark"] = lm_probe.count


def _tcl_check(impl, name, want=None):
    """How often the label knob's Tcl ran since _tcl_mark(); `on` is judged
    against the stock round's count for the same pass."""
    n = lm_probe.count - state["v_tcl_mark"]
    ref = state.setdefault("v_tcl", {}).setdefault(name, {})
    ref[impl] = n
    verdict = []
    if want is not None:
        verdict.append("want {}: {}".format(want, "OK" if n == want else "MISMATCH"))
    if impl == "on" and "stock" in ref:
        stock = ref["stock"]
        verdict.append("<= stock {}: {}".format(stock, "OK" if n <= stock else "NO (+{})".format(n - stock)))
        verdict.append("< 2x stock: {}".format("OK" if (n < 2 * stock if stock else n == 0) else "NO"))
    log("      tcl execs [{}] {}={}  ->  {}".format(name, impl, n, "  ".join(verdict) or "(reference)"))


def _v_set_labels(sel, text):
    orig = state.setdefault("v_orig_labels", {})
    for n in sel:
        orig.setdefault(n.fullName(), n["label"].value())
        n["label"].setValue(text)


def _v_restore_labels(sel):
    orig = state.get("v_orig_labels", {})
    for n in sel:
        n["label"].setValue(orig.pop(n.fullName(), ""))


def _v_set_sizes(sel, value):
    state["v_orig_sizes"] = [(n, n["size"].value()) for n in sel]
    for n in sel:
        n["size"].setValue(value)


def _v_restore_sizes(sel):
    for n, value in state.pop("v_orig_sizes", []):
        n["size"].setValue(value)


_orig_compose_label = autolabeller._compose_label
_orig_warning = nuke.warning
_orig_toNode = nuke.toNode


def _recording_warning(message):
    state.setdefault("v_warnings", []).append(str(message))
    return _orig_warning(message)


def _failing_compose_label(write_indicators=False, substitute_label=True):
    # substitute_label=False is the verifier's call; real builds pass through
    if not substitute_label and nuke.thisNode().fullName() == state.get("v_victim"):
        raise RuntimeError("harness: compose failure")
    return _orig_compose_label(write_indicators=write_indicators, substitute_label=substitute_label)


def _failing_toNode(name):
    # verification resolves the name too; only the poke sees the victim in _stale
    if name == state.get("v_victim") and name in autolabeller._stale:
        raise RuntimeError("harness: poke failure")
    return _orig_toNode(name)


def _v_warnings(needle):
    return [w for w in state.get("v_warnings", []) if needle in w]


def _v_fail_compose(victim):
    state["v_victim"] = victim.fullName()
    state["v_warnings"] = []
    nuke.warning = _recording_warning
    autolabeller._compose_label = _failing_compose_label


def _v_check_c(impl, sel, victim):
    autolabeller._compose_label = _orig_compose_label
    nuke.warning = _orig_warning
    warned = _v_warnings("could not verify")
    if impl != "on":
        log("      (stock: no verifier to fail; {} warnings, verify={})".format(len(warned), len(autolabeller._verify)))
        return
    log("      verify={} (want 0)  'could not verify' warnings={} (want 1)  ->  {}".format(
        len(autolabeller._verify), len(warned), "OK" if not autolabeller._verify and len(warned) == 1 else "MISMATCH"))
    for w in warned:
        log("        " + w[:140])
    _check_bulk([n for n in sel if n is not victim], "vfail")
    _check_bulk([victim], "vfail", expect=0)


def _v_check_d(sel):
    """nuke.Undo.disable()/enable() nest as a counter, so the release must
    leave the count where it found it: after the caller's own enable() the
    state has to read as it did before the caller's disable()."""
    before = state.pop("v_undo_before")
    after_release = nuke.Undo.disabled()
    _check_bulk(sel, "vundo")
    nuke.Undo.enable()
    after_enable = nuke.Undo.disabled()
    extra = 0
    while nuke.Undo.disabled() and extra < 5:
        nuke.Undo.enable()
        extra += 1
    log("      Undo.disabled(): before={} after release={} (want True) after enable={} (want {})  ->  {}{}".format(
        before, after_release, after_enable, before, "OK" if after_release and after_enable == before else "MISMATCH",
        "  (recovered with {} extra enable())".format(extra) if extra else ""))


def _v_fail_poke(victim):
    state["v_victim"] = victim.fullName()
    state["v_warnings"] = []
    nuke.warning = _recording_warning
    nuke.toNode = _failing_toNode


def _v_check_e(impl, sel, victim):
    nuke.toNode = _orig_toNode
    nuke.warning = _orig_warning
    warned = _v_warnings("could not refresh")
    if impl != "on":
        log("      (stock: nothing is poked; {} warnings, stale={})".format(len(warned), len(autolabeller._stale)))
        return
    log("      stale={} (want 0)  'could not refresh' warnings={} (want 1)  ->  {}".format(
        len(autolabeller._stale), len(warned), "OK" if not autolabeller._stale and len(warned) == 1 else "MISMATCH"))
    for w in warned:
        log("        " + w[:140])
    _check_bulk([n for n in sel if n is not victim], "vpoke")
    _check_bulk([victim], "vpoke", expect=0)


def _deepest(nodes):
    """The node with the longest upstream chain, and that chain's length."""
    depth = {}
    for start in nodes:
        stack = [start]
        while stack:
            node = stack[-1]
            key = node.fullName()
            if key in depth:
                stack.pop()
                continue
            inputs = [node.input(i) for i in range(node.inputs())]
            inputs = [n for n in inputs if n is not None]
            pending = [n for n in inputs if n.fullName() not in depth]
            if pending:
                stack.extend(pending)
                continue
            depth[key] = 1 + max([depth[n.fullName()] for n in inputs] or [-1])
            stack.pop()
    best = max(nodes, key=lambda n: depth[n.fullName()])
    return best, depth[best.fullName()]


def _widget(class_name, object_name=None):
    app = QtWidgets.QApplication.instance()
    for w in app.allWidgets():
        if class_name in w.metaObject().className() and (object_name is None or w.objectName() == object_name):
            return w
    return None


def _v_show_dope_sheet():
    """Float the Dope Sheet in its own window: it is a tab of the DAG's own
    dock, so raising it there would hide the DAG and stop the label pass."""
    view = _widget("LinkedView", "DopeSheet.1")
    dag = _widget("DAGNukeWindow", "DAG.1")
    stack = view.parent()
    state["v_dope"] = view
    if stack is None:
        view.show()
        return
    view.setParent(None)
    view.setWindowFlags(QtCore.Qt.Window)
    view.resize(640, 480)
    view.move(1270, 60)
    view.show()
    stack.setCurrentWidget(dag)


def _v_hide_dope_sheet():
    """Hide the floated window rather than re-dock it: a widget inserted
    back into the pane's stack leaves every DAG tab Nuke opens afterwards
    (showDag on a group) hidden and unpainted, and the pane is fine with
    the sheet staying out."""
    state.pop("v_dope").hide()


def _v_check_pane(want_open):
    sheet = _widget("Dope_Sheet")
    shown = sheet is not None and sheet.isVisible()
    dag_shown = state["dag"].isVisible()
    log("      Dope Sheet visible={} (want {})  DAG visible={} (want True)  ->  {}".format(
        shown, want_open, dag_shown, "OK" if shown == want_open and dag_shown else "MISMATCH"))


def _v_pane_snapshot(key):
    """The previous step's census line, kept for the open-vs-closed compare."""
    label, calls, builds, hits, action_s, settle_s, lm, deov = results[-1]
    stats = state.get("last_stats", {})
    state.setdefault("v_pane", {})[key] = {
        "calls": calls, "settle": settle_s, "label_ms": lm * 1000,
        "pokes": stats.get("pokes", 0), "poke_ms": stats.get("poke_s", 0.0) * 1000}


def _v_check_f():
    closed = state["v_pane"]["closed"]
    opened = state["v_pane"]["open"]
    ok = opened["calls"] == closed["calls"] and opened["settle"] <= 2 * closed["settle"] + 0.25
    log("      pane open vs closed: calls {} vs {} (want equal)  settle {:.2f}s vs {:.2f}s (want open <= 2x closed + 0.25s)  "
        "label_py {:.1f} vs {:.1f} ms  pokes {} in {:.1f} vs {} in {:.1f} ms  ->  {}".format(
            opened["calls"], closed["calls"], opened["settle"], closed["settle"], opened["label_ms"], closed["label_ms"],
            opened["pokes"], opened["poke_ms"], closed["pokes"], closed["poke_ms"], "OK" if ok else "SLOWER"))


def _v_viewer_node():
    return next(iter(nuke.allNodes("Viewer")), None)


def _v_record_viewer():
    viewer = _v_viewer_node()
    if viewer is None:
        state["v_viewer0"] = None
        return
    inputs = [viewer.input(i).name() if viewer.input(i) else None for i in range(2)]
    state["v_viewer0"] = (viewer.name(), inputs, int(viewer["input_number"].value()))
    nuke.show(viewer)


def _v_restore_viewer():
    rec = state.pop("v_viewer0", None)
    if rec is None:
        log("      (no viewer to restore)")
        return
    name, inputs, active = rec
    viewer = nuke.toNode(name)
    for i, input_name in enumerate(inputs):
        viewer.setInput(i, nuke.toNode(input_name) if input_name else None)
    viewer["input_number"].setValue(active)
    log("      {} inputs back to {} active input {}".format(name, inputs, active))


def _v_playback(seconds, tick_ms=40, step_frames=False):
    """Viewer playback for `seconds` (frame-stepping from the timer when asked
    or when no Viewer is active); busy until stopped, so the step's settle
    time counts from the start of playback."""
    viewer = None if step_frames else nuke.activeViewer()
    play = {"mode": "activeViewer.play" if viewer else "nuke.frame step ({} ms)".format(tick_ms),
            "seconds": seconds, "changes": 0, "t0": time.perf_counter()}
    state["v_play"] = play
    if viewer:
        viewer.play(1)
    state["busy"] = True
    state["ticks"] = []
    last = {"t": time.perf_counter(), "n": 0, "frame": nuke.frame()}

    def tick():
        now = time.perf_counter()
        state["ticks"].append(now - last["t"] - tick_ms / 1000.0)
        last["t"] = now
        last["n"] += 1
        if viewer is None:
            nuke.frame(nuke.frame() + 1)
        if nuke.frame() != last["frame"]:
            play["changes"] += 1
            last["frame"] = nuke.frame()
        if last["n"] * tick_ms / 1000.0 >= seconds:
            if viewer:
                viewer.stop()
            play["ran_s"] = time.perf_counter() - play["t0"]
            state["busy"] = False
            return
        QtCore.QTimer.singleShot(tick_ms, tick)

    QtCore.QTimer.singleShot(tick_ms, tick)


def _v_check_g(after_stop_max_s=6.0):
    play = state.pop("v_play")
    label, calls, builds, hits, action_s, settle_s, lm, deov = results[-1]
    frames = play["changes"]
    ran_s = play.get("ran_s", play["seconds"])
    after_stop = settle_s - ran_s
    want_frames = int(play["seconds"] * 10)
    ok = frames >= want_frames and after_stop <= after_stop_max_s
    log("      playback via {}: {} frame changes seen in {:.2f}s ({:.0f}s asked; want >= {} changes)  settled {:.2f}s after playback stopped (want <= {:.0f}s)  ->  {}".format(
        play["mode"], frames, ran_s, play["seconds"], want_frames, after_stop, after_stop_max_s, "OK" if ok else "MISMATCH"))


def _v_check_h(impl, want_calls=20):
    times = state.pop("v_viewer_log", [])
    ref = state.setdefault("v_viewer_ref", {})
    ref[impl] = times
    n = len(times)
    mean_ms = sum(times) / n * 1000 if n else 0.0
    max_ms = max(times) * 1000 if n else 0.0
    ticks = state.get("last_ticks") or []
    lat = sorted(t * 1000 for t in ticks[1:])
    lat_mean = sum(lat) / len(lat) if lat else 0.0
    verdict = ["want {} calls: {}".format(want_calls, "OK" if n == want_calls else "MISMATCH")]
    if impl == "on" and ref.get("stock"):
        stock_mean = sum(ref["stock"]) / len(ref["stock"]) * 1000
        verdict.append("mean <= 3x stock ({:.2f}ms): {}".format(stock_mean, "OK" if mean_ms <= 3 * stock_mean else "NO"))
    log("      Viewer label calls={} per-call mean={:.2f}ms max={:.2f}ms  tick latency mean={:.1f}ms max={:.1f}ms  ->  {}".format(
        n, mean_ms, max_ms, lat_mean, lat[-1] if lat else 0.0, "  ".join(verdict)))


def _v_watch(sel):
    """Count label requests per node for `sel` until _v_watched() reads them."""
    state["v_watch"] = {}
    state["v_watch_names"] = [n.fullName() for n in sel]


def _v_watched():
    """(nodes requested at least once, requests in all) among the watched."""
    watch = state.pop("v_watch", {})
    names = state.pop("v_watch_names", [])
    return sum(1 for nm in names if watch.get(nm)), sum(watch.get(nm, 0) for nm in names)


def _v_make_groups():
    """A LiveGroup and a Precomp with Grades inside, and a Group whose
    onCreate has run (pasted: paste is a creation, the knob itself is set
    after the first creation)."""
    state["v_warnings"] = []
    nuke.warning = _recording_warning
    for n in nuke.allNodes():
        n.setSelected(False)
    made = []
    for kind, count in (("LiveGroup", 20), ("Precomp", 5), ("Group", 20)):
        node = getattr(nuke.nodes, kind)(name="V" + kind)
        with node:
            for i in range(count):
                nuke.nodes.Grade(xpos=i * 120, ypos=0)
        if kind == "Group":
            node["onCreate"].setValue("nuke.tcl('knob label \"made by [value name]\"')")
            node.setSelected(True)
            nuke.nodeCopy("%clipboard%")
            nuke.delete(node)
            for n in nuke.allNodes():
                n.setSelected(False)
            nuke.nodePaste("%clipboard%")
            node = nuke.selectedNodes()[0]
            node.setSelected(False)
            made_by = node["label"].value()
            log("      pasted {}: onCreate Tcl wrote label={!r}  ->  {}".format(
                node.name(), made_by, "OK" if made_by == "made by " + node.name() else "MISMATCH"))
        made.append((kind, node, node.nodes()))
    state["v_groups"] = made
    log("      created " + ", ".join("{} {} ({} inner)".format(kind, node.name(), len(inner)) for kind, node, inner in made))


def _v_show_group(index):
    """Enter the group's DAG (its own Node Graph tab) from root; the label
    requests only come from a paint, so a tab that opened hidden is raised
    by hand and the quirk logged."""
    kind, node, inner = state["v_groups"][index]
    _v_watch(inner)
    nuke.showDag(node)
    tab = _widget("DAG_Window", "DAG." + node.name())
    if tab is None:
        log("      (no DAG tab for {})".format(node.name()))
        return
    was_visible = tab.isVisible()
    if not was_visible:
        child, parent = tab, tab.parent()
        while parent is not None:
            if isinstance(parent, QtWidgets.QStackedWidget):
                parent.setCurrentWidget(child)
            child, parent = parent, parent.parent()
    nuke.zoom(1.0, (1200, 0))
    log("      DAG tab {}: visible after showDag={}{}  root DAG visible={}".format(
        tab.objectName(), was_visible, "" if was_visible else " (raised by hand: {})".format(tab.isVisible()), state["dag"].isVisible()))


def _v_leave_group(index):
    kind, node, inner = state["v_groups"][index]
    hit, total = _v_watched()
    log("      {} {}: {}/{} inner nodes requested while shown ({} requests)".format(kind, node.name(), hit, len(inner), total))
    nuke.showDag(nuke.root())
    log("      root DAG visible after showDag(root)={}".format(state["dag"].isVisible()))


def _v_edit_inner(value):
    # a node created from Python keeps its creation-time label until a knob
    # changes, so showing the group draws it without a label request
    for kind, node, inner in state["v_groups"]:
        for n in inner:
            n["white"].setValue(value)


def _v_check_i(impl, index):
    kind, node, inner = state["v_groups"][index]
    hit, total = _v_watched()
    calls = results[-1][1]
    if hit == len(inner):
        verdict = "OK"
    elif kind == "Precomp" and total == 0:
        verdict = "n/a (Precomp DAG not entered by showDag)"
    else:
        verdict = "MISMATCH"
    log("      {} {}: show DAG made {} label requests, {}/{} inner nodes requested ({} requests)  ->  {}".format(
        kind, node.name(), calls, hit, len(inner), total, verdict))
    if impl == "on" and hit:
        _check_bulk(inner, "2.500")
    nuke.showDag(nuke.root())


def _v_finish_i(impl):
    nuke.warning = _orig_warning
    warned = _v_warnings("could not verify") + _v_warnings("runIn")
    log("      'could not verify' / runIn warnings={} (want 0)  ->  {}".format(len(warned), "OK" if not warned else "MISMATCH"))
    for w in warned:
        log("        " + w[:140])
    for kind, node, inner in state.pop("v_groups"):
        nuke.delete(node)
    log("      groups deleted; allNodes={} (recurse {})".format(len(nuke.allNodes()), len(nuke.allNodes(recurseGroups=True))))


def _v_link_white(deps, src):
    state["v_expr_orig"] = [(n, n["white"].value()) for n in deps]
    state["v_expr_src"] = (src, src["white"].value())
    for n in deps:
        n["white"].setExpression("{}.white".format(src.name()))


def _v_check_j(impl, deps, needle, before):
    hit, total = _v_watched()
    calls = results[-1][1]
    if before:
        # only the edited node is re-requested; the dependents' new value
        # waits for a pass (frame change, viewer connect) under either impl
        showing = sum(needle in autolabeller._shown.get(n.fullName(), "") for n in deps) if impl == "on" else "n/a"
        log("      source edit: {} label requests in all, {}/{} dependents re-requested ({} requests), dependents showing {!r}: {} (0 expected: no re-request on an expression-driven change)".format(
            calls, hit, len(deps), total, needle, showing))
        return
    log("      pass: {} label requests in all, {}/{} dependents re-requested ({} requests; want {}/{})  ->  {}".format(
        calls, hit, len(deps), total, len(deps), len(deps), "OK" if hit == len(deps) else "MISMATCH"))
    if impl == "on":
        _check_bulk(deps, needle)


def _v_unlink_white():
    for n, value in state.pop("v_expr_orig", []):
        n["white"].clearAnimated()
        n["white"].setValue(value)
    src, value = state.pop("v_expr_src")
    src["white"].setValue(value)


def _v_overlaps(names):
    """Pairs (name, other) whose DAG boxes overlap, for the named nodes."""
    boxes = {}
    for n in nuke.allNodes():
        if n.Class() in ("BackdropNode", "Viewer"):
            continue
        x, y = n.xpos(), n.ypos()
        boxes[n.name()] = (x, y, x + n.screenWidth(), y + n.screenHeight())
    by_top = sorted(boxes.items(), key=lambda kv: kv[1][1])
    tops = [box[1] for _, box in by_top]
    max_h = max(box[3] - box[1] for box in boxes.values())
    pairs = set()
    for name in names:
        a = boxes.get(name)
        if a is None:
            continue
        lo = bisect.bisect_left(tops, a[1] - max_h)
        hi = bisect.bisect_right(tops, a[3])
        for other, b in by_top[lo:hi]:
            if other != name and a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                pairs.add(tuple(sorted((name, other))))
    return pairs


def _v_deov_edit(sel):
    names = [n.name() for n in sel]
    rec = {"names": names, "fires": [], "peak": 0,
           "baseline": _v_overlaps(names),
           "ypos0": {n.name(): n.ypos() for n in nuke.allNodes()}}
    state["v_deov"] = rec

    def sample():
        if state.get("v_deov") is not rec:
            return
        rec["peak"] = max(rec["peak"], len(autolabeller._pending_deoverlap))
        QtCore.QTimer.singleShot(20, sample)

    QtCore.QTimer.singleShot(20, sample)
    _v_set_labels(sel, "vdeov 1\nvdeov 2\nvdeov 3\nvdeov 4")
    rec["after_edit"] = len(autolabeller._pending_deoverlap)
    log("      pending_deoverlap right after the edit = {}".format(rec["after_edit"]))


def _v_check_k(impl, sel):
    rec = state.pop("v_deov")
    after = _v_overlaps(rec["names"])
    new = after - rec["baseline"]
    fed = sum(pending for pending, _ in rec["fires"])
    fires = " ".join("({} pending, {} overlapping)".format(p, o) for p, o in rec["fires"]) or "none"
    if impl != "on":
        log("      (stock: no de-overlap) pending after edit={} peak={} fires={}  overlapping pairs: baseline={} after={} new={}".format(
            rec["after_edit"], rec["peak"], len(rec["fires"]), len(rec["baseline"]), len(after), len(new)))
        return
    ok = fed == len(sel) and not new
    log("      pending after edit={} peak={} at fire: {}  fed in all={} (want {})  overlapping pairs: baseline={} after={} new={} (want 0)  ->  {}".format(
        rec["after_edit"], rec["peak"], fires, fed, len(sel), len(rec["baseline"]), len(after), len(new), "OK" if ok else "MISMATCH"))
    for pair in sorted(new)[:10]:
        log("        new overlap: {} / {}".format(*pair))
    state["v_deov_ypos0"] = rec["ypos0"]


def _v_deov_restore_layout():
    ypos0 = state.pop("v_deov_ypos0", None)
    if ypos0 is None:
        log("      (layout unchanged)")
        return
    moved = 0
    nuke.Undo.disable()
    try:
        for n in nuke.allNodes():
            y = ypos0.get(n.name())
            if y is not None and n.ypos() != y:
                n.setYpos(y)
                moved += 1
    finally:
        nuke.Undo.enable()
    log("      {} nodes moved back".format(moved))


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
