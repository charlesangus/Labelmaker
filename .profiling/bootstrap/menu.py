"""Headless profiling session for Labelmaker (GUI Nuke driven under xvfb).

Driven by .profiling/run.sh, which puts this folder on NUKE_PATH. Controlled by:

  LM_PROFILE_SCRIPT      .nk script to profile against
  LM_PROFILE_MODE        "on" (Labelmaker registered) | "off" (stock autolabel)
  LM_PROFILE_SCENARIOS   comma-separated subset of SCENARIOS (default: all)
  LM_PROFILE_OUT         where to write the cProfile .prof (optional)

Why the labels are driven by hand rather than by panning the DAG: Nuke only
evaluates a node's autolabel when it *draws* the node, and under xvfb the Node
Graph's GL window never renders (repaint(), requestUpdate(), even a screen grab
produce zero autolabel calls — verified by the "diag" scenario). So the harness
calls the autolabel in each node's own context with nuke.runIn, which is exactly
what Nuke's draw path does, minus the drawing. One pass over N nodes therefore
equals one full-screen DAG redraw showing N nodes.
"""
import cProfile
import collections
import os
import pstats
import statistics
import sys
import time

import nuke

SCRIPT = os.environ.get("LM_PROFILE_SCRIPT", "")
MODE = os.environ.get("LM_PROFILE_MODE", "on")
PROF_OUT = os.environ.get("LM_PROFILE_OUT", "")
WANTED = [s for s in os.environ.get("LM_PROFILE_SCENARIOS", "").split(",") if s]

repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if repo not in sys.path:
    sys.path.insert(0, repo)
nuke.pluginAddPath(repo)

import labelmaker  # noqa: E402
import labelmaker_prefs  # noqa: E402

# auto-deoverlap moves nodes as labels grow; keep it out of the measurement
labelmaker_prefs.prefs_singleton._prefs["deoverlap_enabled"] = False

autolabeller = labelmaker.autolabeller_singleton
autolabeller.set_enabled(MODE != "off")

BUILD = "labelmaker.autolabeller_singleton.create_autolabel()"
NOOP = "None"


def _process_events():
    from PySide6 import QtWidgets
    QtWidgets.QApplication.instance().processEvents()


def _stats_line(name, times, extra=""):
    if not times:
        print("{:<26} (no samples)".format(name))
        return
    ordered = sorted(times)
    print("{:<26} n={:<5} total={:8.1f}ms  mean={:7.3f}ms  median={:7.3f}ms  "
          "p95={:7.3f}ms  max={:7.3f}ms {}".format(
              name, len(times), sum(times) * 1000,
              statistics.mean(times) * 1000, statistics.median(times) * 1000,
              ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] * 1000,
              ordered[-1] * 1000, extra))


def _runin_overhead(nodes, samples=100):
    """Cost of nuke.runIn itself, so the label numbers can be read net of it."""
    times = []
    for node in nodes[:samples]:
        start = time.perf_counter()
        nuke.runIn(node.fullName(), NOOP)
        times.append(time.perf_counter() - start)
    return statistics.median(times) if times else 0.0


def _build_pass(nodes):
    """One 'DAG redraw': evaluate the autolabel for every node, timed per node."""
    times = []
    for node in nodes:
        start = time.perf_counter()
        nuke.runIn(node.fullName(), BUILD)
        times.append(time.perf_counter() - start)
    return times


# ----------------------------------------------------------------------
# scenarios
# ----------------------------------------------------------------------
def scenario_load(nodes):
    """Cold script open, with Labelmaker registered vs not."""
    nuke.scriptClear()
    _process_events()
    start = time.perf_counter()
    nuke.scriptOpen(SCRIPT)
    elapsed = time.perf_counter() - start
    _stats_line("script open", [elapsed], "({} nodes)".format(len(nuke.allNodes())))
    return None


def scenario_build(nodes):
    """Cold label build for every node — one full-screen redraw's worth of work."""
    overhead = _runin_overhead(nodes)
    autolabeller.reset_stats()
    times = _build_pass(nodes)
    _stats_line("label build (cold)", times,
                "| nuke.runIn overhead ~{:.3f}ms/node".format(overhead * 1000))
    net = sum(times) - overhead * len(times)
    print("{:<26} {:.1f}ms for {} nodes ({:.3f}ms/node net of runIn)".format(
        "  net Labelmaker cost", net * 1000, len(times), net * 1000 / max(1, len(times))))

    by_class = collections.defaultdict(list)
    for node, elapsed in zip(nodes, times):
        by_class[node.Class()].append(elapsed)
    print("\n  per node class (slowest first)")
    print("  {:<24} {:>6} {:>12} {:>12}".format("class", "nodes", "total ms", "mean ms"))
    for cls, cls_times in sorted(by_class.items(), key=lambda kv: -sum(kv[1]))[:15]:
        print("  {:<24} {:>6} {:>12.1f} {:>12.3f}".format(
            cls, len(cls_times), sum(cls_times) * 1000,
            statistics.mean(cls_times) * 1000))

    print("\n  slowest individual nodes")
    for node, elapsed in sorted(zip(nodes, times), key=lambda pair: -pair[1])[:10]:
        print("  {:<28} {:<16} {:7.3f}ms".format(node.name(), node.Class(), elapsed * 1000))

    if MODE != "off":
        autolabeller.dump_stats()
    return None


def scenario_cache(nodes):
    """Second pass with the debounce window still open — the cache-hit path."""
    autolabeller.reset_stats()
    times = _build_pass(nodes)
    _stats_line("label build (cached)", times)
    if MODE != "off":
        autolabeller.dump_stats()
    return None


def scenario_repeat(nodes):
    """Ten redraws' worth of label work, as a user panning for a second would see."""
    passes = []
    for _ in range(10):
        # let the debounce timer fire so each pass is a genuine rebuild
        autolabeller._label_fresh.clear()
        start = time.perf_counter()
        _build_pass(nodes)
        passes.append(time.perf_counter() - start)
    _stats_line("full redraw (rebuild)", passes)


def scenario_diag(nodes):
    """Evidence that DAG paints never reach the autolabel in a headless session."""
    counter = {"calls": 0}

    def _counting_autolabel():
        counter["calls"] += 1
        return None

    nuke.addAutolabel(_counting_autolabel)
    node = nodes[len(nodes) // 2]
    from PySide6 import QtGui, QtWidgets
    dag = [w for w in QtWidgets.QApplication.instance().allWidgets()
           if w.metaObject().className() == "DAGNukeWindow"][0]
    nuke.zoom(1.5, [node.xpos(), node.ypos()])
    dag.repaint()
    _process_events()
    node.setXYpos(node.xpos() + 20, node.ypos())
    dag.repaint()
    QtGui.QGuiApplication.primaryScreen().grabWindow(0)
    _process_events()
    print("  autolabel calls from painting the DAG: {}".format(counter["calls"]))
    nuke.runIn(node.fullName(), BUILD)
    print("  autolabel calls via nuke.runIn       : {}".format(counter["calls"]))
    nuke.removeAutolabel(_counting_autolabel)


SCENARIOS = [
    ("load", scenario_load),
    ("build", scenario_build),
    ("cache", scenario_cache),
    ("repeat", scenario_repeat),
    ("diag", scenario_diag),
]


def run():
    # Nuke's GUI redirects sys.stdout to the Script Editor pane; point it at the
    # real stderr so everything (including labelmaker's prints) reaches the shell.
    sys.stdout = sys.__stderr__
    print("\n" + "#" * 78)
    print("# Labelmaker profile — mode={} script={}".format(MODE, SCRIPT))
    print("#" * 78)

    nuke.scriptOpen(SCRIPT)
    _process_events()
    nodes = [n for n in nuke.allNodes() if n.Class() != "Viewer"]
    print("{} nodes\n".format(len(nodes)))

    profiler = cProfile.Profile()
    for name, func in SCENARIOS:
        if WANTED and name not in WANTED:
            continue
        print("--- {} ---".format(name))
        profiler.enable()
        func(nodes)
        profiler.disable()
        print("")
        nodes = [n for n in nuke.allNodes() if n.Class() != "Viewer"]

    print("\ncProfile — top 30 by internal time")
    pstats.Stats(profiler, stream=sys.stdout).sort_stats("tottime").print_stats(30)
    if PROF_OUT:
        profiler.dump_stats(PROF_OUT)
        print("wrote {}".format(PROF_OUT))

    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


from PySide6 import QtCore  # noqa: E402
QtCore.QTimer.singleShot(3000, run)
