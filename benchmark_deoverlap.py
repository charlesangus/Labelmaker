"""Benchmark the old vs new Labelmaker de-overlap implementations.

WARNING: this clears the current Nuke script repeatedly. Run it in a
throwaway Nuke session only, from the Script Editor:

    import benchmark_deoverlap
    benchmark_deoverlap.run()

For each scenario and node count it builds a synthetic graph, then times the
old graph-traversal implementation (copied verbatim below, prefixed with
``old_``) against the new spatial-cascade ``deoverlap_from_nodes`` on the
identical layout. Node positions are restored from a snapshot between every
timed repetition so each rep does identical work; the median of the
repetitions is reported, along with how many nodes each implementation moved.
"""
import math
import statistics
import sys
import time

import nuke

import labelmaker_deoverlap
import labelmaker_prefs
from labelmaker_deoverlap import MINIMUM_GAP, NODES_TO_SKIP, _bboxes_overlap

NODE_COUNTS = [10, 20, 100]
REPETITIONS = 3
OVERLAP_PIXELS = 10  # how far the trigger node is pushed into the node below it


# --- Old implementation, copied verbatim from labelmaker_deoverlap.py as of
# commit dbaec83 (before the spatial-cascade rewrite), prefixed with old_. ---

def old_deoverlap_downstream(source_node):
    """Push graph descendants of source_node down if they overlap it after a height increase."""
    nuke.Undo.disable()
    try:
        _old_deoverlap_chain(source_node, set())
    finally:
        nuke.Undo.enable()


def _old_deoverlap_chain(node, visited):
    if node.name() in visited:
        return
    visited.add(node.name())
    source_bbox = _old_node_bbox(node)
    for downstream_node in node.dependent(nuke.INPUTS):
        if downstream_node.Class() in NODES_TO_SKIP:
            continue
        down_bbox = _old_node_bbox(downstream_node)
        # Only consider nodes physically below the source to avoid pushing sideways branches
        if down_bbox[1] <= source_bbox[1]:
            continue
        if _bboxes_overlap(source_bbox, down_bbox):
            push_amount = source_bbox[3] - down_bbox[1] + MINIMUM_GAP
            downstream_node.setYpos(downstream_node.ypos() + push_amount)
        # Always recurse in case this node now overlaps its own descendants
        _old_deoverlap_chain(downstream_node, visited)


def _old_node_bbox(node):
    """Return bounding box in DAG node coordinates (same units as xpos/ypos)."""
    x = node.xpos()
    y = node.ypos()
    return (x, y, x + node.screenWidth(), y + node.screenHeight())


# --- Benchmark helpers ---

def process_events():
    """Let Qt draw the freshly created nodes so screenWidth/screenHeight are valid."""
    from PySide6 import QtWidgets
    QtWidgets.QApplication.processEvents()


def snapshot_positions():
    return {node.name(): (node.xpos(), node.ypos()) for node in nuke.allNodes()}


def restore_positions(position_snapshot):
    for node in nuke.allNodes():
        saved_position = position_snapshot.get(node.name())
        if saved_position is not None:
            node.setXpos(saved_position[0])
            node.setYpos(saved_position[1])


def count_moved(position_snapshot):
    moved_count = 0
    for node in nuke.allNodes():
        saved_position = position_snapshot.get(node.name())
        if saved_position is not None and (node.xpos(), node.ypos()) != saved_position:
            moved_count += 1
    return moved_count


def _overlap_above(upper_node, lower_node):
    """Move upper_node down so its bottom edge intrudes into lower_node."""
    upper_node.setYpos(lower_node.ypos() - upper_node.screenHeight() + OVERLAP_PIXELS)


def _build_chain(node_count, vertical_spacing):
    chain_nodes = []
    previous_node = None
    for i in range(node_count):
        node = nuke.nodes.NoOp()
        node.setXpos(0)
        node.setYpos(i * vertical_spacing)
        if previous_node is not None:
            node.setInput(0, previous_node)
        previous_node = node
        chain_nodes.append(node)
    return chain_nodes


# --- Scenarios. Each builds a graph and returns the trigger node (the node
# whose label notionally grew), positioned so the scenario exercises the
# intended code path. ---

def scenario_linear_chain(node_count):
    """Long connected chain, one real overlap at the top.

    Worst case for the old implementation: it must traverse the entire
    downstream chain even though only one node needs pushing.
    """
    chain_nodes = _build_chain(node_count, vertical_spacing=80)
    process_events()
    _overlap_above(chain_nodes[0], chain_nodes[1])
    return chain_nodes[0]


def scenario_branching_tree(node_count):
    """Binary fan-out tree, trigger at the root overlapping its first child."""
    tree_nodes = []
    for i in range(node_count):
        depth = int(math.floor(math.log2(i + 1)))
        index_in_level = i - (2 ** depth - 1)
        node = nuke.nodes.NoOp()
        node.setXpos(int((index_in_level - (2 ** depth) / 2.0) * 120))
        node.setYpos(depth * 100)
        if i > 0:
            node.setInput(0, tree_nodes[(i - 1) // 2])
        tree_nodes.append(node)
    process_events()
    root_node = tree_nodes[0]
    if len(tree_nodes) > 1:
        first_child = tree_nodes[1]
        root_node.setXpos(first_child.xpos())
        _overlap_above(root_node, first_child)
    return root_node


def scenario_disconnected_grid(node_count):
    """Grid of unconnected nodes; trigger overlaps the unconnected node below it.

    The old implementation cannot fix this at all (no graph descendants), so
    expect old moved = 0 and new moved >= 1.
    """
    columns = 10
    grid_nodes = []
    for i in range(node_count):
        node = nuke.nodes.NoOp()
        node.setXpos((i % columns) * 150)
        node.setYpos((i // columns) * 90)
        grid_nodes.append(node)
    process_events()
    if node_count > columns:
        _overlap_above(grid_nodes[0], grid_nodes[columns])
    return grid_nodes[0]


def scenario_no_overlap_needed(node_count):
    """Connected chain with generous gaps: nothing should move.

    This is the common interactive case (label grows but the node below has
    room) and measures pure traversal overhead.
    """
    chain_nodes = _build_chain(node_count, vertical_spacing=100)
    process_events()
    return chain_nodes[0]


def scenario_cascade_push(node_count):
    """Tightly packed chain where the trigger forces a ripple of pushes."""
    chain_nodes = _build_chain(node_count, vertical_spacing=200)
    process_events()
    node_height = chain_nodes[0].screenHeight()
    tight_spacing = max(node_height - OVERLAP_PIXELS, 1)
    for i, node in enumerate(chain_nodes):
        node.setYpos(i * tight_spacing)
    return chain_nodes[0]


SCENARIOS = [
    ("linear_chain", scenario_linear_chain),
    ("branching_tree", scenario_branching_tree),
    ("disconnected_grid", scenario_disconnected_grid),
    ("no_overlap_needed", scenario_no_overlap_needed),
    ("cascade_push", scenario_cascade_push),
]

ROW_FORMAT = "{:<18} {:>6} {:>10} {:>10} {:>9} {:>10} {:>10}"


def _measure(deoverlap_callable, position_snapshot):
    timings_ms = []
    moved_count = 0
    for _ in range(REPETITIONS):
        restore_positions(position_snapshot)
        start_time = time.perf_counter()
        deoverlap_callable()
        timings_ms.append((time.perf_counter() - start_time) * 1000.0)
        moved_count = count_moved(position_snapshot)
    restore_positions(position_snapshot)
    return statistics.median(timings_ms), moved_count


def run(node_counts=None):
    node_counts = node_counts or NODE_COUNTS
    print("benchmark_deoverlap: this clears the current script between cases.")
    print(ROW_FORMAT.format(
        "scenario", "nodes", "old ms", "new ms", "speedup", "old moved", "new moved"
    ))

    previous_pref = labelmaker_prefs.prefs_singleton.get("deoverlap_enabled")
    labelmaker_prefs.prefs_singleton.set("deoverlap_enabled", False)
    original_recursion_limit = sys.getrecursionlimit()
    try:
        for scenario_name, scenario_builder in SCENARIOS:
            for node_count in node_counts:
                nuke.scriptClear()
                trigger_node = scenario_builder(node_count)
                trigger_node_name = trigger_node.name()
                position_snapshot = snapshot_positions()

                # The old implementation recurses once per chain node, which
                # overflows Python's default recursion limit on deep graphs.
                sys.setrecursionlimit(max(original_recursion_limit, node_count * 4 + 100))

                old_ms, old_moved = _measure(
                    lambda: old_deoverlap_downstream(trigger_node),
                    position_snapshot,
                )
                new_ms, new_moved = _measure(
                    lambda: labelmaker_deoverlap.deoverlap_from_nodes([trigger_node_name]),
                    position_snapshot,
                )

                speedup = "{:.1f}x".format(old_ms / new_ms) if new_ms > 0 else "inf"
                print(ROW_FORMAT.format(
                    scenario_name,
                    node_count,
                    "{:.2f}".format(old_ms),
                    "{:.2f}".format(new_ms),
                    speedup,
                    old_moved,
                    new_moved,
                ))
    finally:
        sys.setrecursionlimit(original_recursion_limit)
        labelmaker_prefs.prefs_singleton.set("deoverlap_enabled", previous_pref)
        nuke.scriptClear()
