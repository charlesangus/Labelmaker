import nuke

NODES_TO_SKIP = ('BackdropNode', 'Viewer')
MINIMUM_GAP = 6  # DAG units of breathing room between nodes after de-overlap


def deoverlap_from_nodes(source_node_names):
    """Push nodes below the given source nodes down if they overlap them.

    Spatial cascade: starting from the nodes whose labels grew, any node
    physically below that overlaps a grown (or subsequently pushed) node is
    pushed down, regardless of graph connectivity — same philosophy as
    deoverlap_all. The sweep visits nodes top-to-bottom; each node is pushed
    below the lowest bottom edge among the "dirty" nodes (sources plus nodes
    already pushed) that sit above it and actually overlap it. Nodes that
    were not grown or pushed never displace anything, so pre-existing
    overlaps elsewhere in the script are left alone.

    All node positions are read from Nuke exactly once up front; the cascade
    runs on cached data and only nodes that actually moved get a setYpos
    call. This keeps the cost proportional to the number of nodes involved,
    not to graph topology.

    Undo is disabled around repositioning so automatic layout adjustments do
    not pollute the undo stack.
    """
    nuke.Undo.disable()
    try:
        all_nodes = [n for n in nuke.allNodes() if n.Class() not in NODES_TO_SKIP]
        if not all_nodes:
            return

        # Read all node positions once upfront. Store as mutable lists so
        # pushes update the cache without re-reading from Nuke.
        # Layout: [left, top, right, bottom]
        nodes_by_name = {}
        position_cache = {}
        original_tops = {}
        for node in all_nodes:
            node_name = node.name()
            x = node.xpos()
            y = node.ypos()
            nodes_by_name[node_name] = node
            position_cache[node_name] = [x, y, x + node.screenWidth(), y + node.screenHeight()]
            original_tops[node_name] = y

        # Source names may be stale (node deleted between the label-change
        # trigger and the debounce timer firing) — silently skip those.
        dirty_names = set(
            name for name in source_node_names if name in position_cache
        )
        if not dirty_names:
            return

        # Sweep top-to-bottom by original position (name as tiebreaker for
        # determinism). Visiting in this order means every potential pusher
        # is finalized before the nodes below it are considered.
        sorted_node_names = sorted(
            position_cache,
            key=lambda node_name: (original_tops[node_name], node_name)
        )

        for node_name in sorted_node_names:
            node_bbox = position_cache[node_name]
            max_pusher_bottom = None
            for pusher_name in dirty_names:
                if pusher_name == node_name:
                    continue
                # Only nodes that started strictly above may push this one,
                # so side-by-side neighbours are never moved.
                if original_tops[pusher_name] >= original_tops[node_name]:
                    continue
                pusher_bbox = position_cache[pusher_name]
                if not _bboxes_overlap_horizontally(node_bbox, pusher_bbox):
                    continue
                # No actual overlap: the pusher's bottom is above this node.
                if pusher_bbox[3] < node_bbox[1]:
                    continue
                # No actual overlap: the pusher was pushed entirely below
                # this node earlier in the sweep.
                if pusher_bbox[1] > node_bbox[3]:
                    continue
                if max_pusher_bottom is None or pusher_bbox[3] > max_pusher_bottom:
                    max_pusher_bottom = pusher_bbox[3]

            if max_pusher_bottom is None:
                continue

            required_top = max_pusher_bottom + MINIMUM_GAP
            if required_top > node_bbox[1]:
                push_amount = required_top - node_bbox[1]
                node_bbox[1] += push_amount
                node_bbox[3] += push_amount
                # This node may now overlap nodes below it in turn.
                dirty_names.add(node_name)

        for node_name, node_bbox in position_cache.items():
            if node_bbox[1] != original_tops[node_name]:
                nodes_by_name[node_name].setYpos(int(node_bbox[1]))
    finally:
        nuke.Undo.enable()


def deoverlap_all(undoable=False):
    """De-overlap all nodes in the script using a ypos-sorted spatial sweep.

    Sorts all nodes top-to-bottom, then for each node finds the maximum bottom
    edge among all preceding nodes that overlap it horizontally, and pushes the
    node down if needed. This handles disconnected clusters naturally because it
    uses spatial position rather than graph topology.

    When undoable is True, undo is left enabled so repositioning is tracked
    by Nuke's undo system. When False (the default, used for automatic
    label-change triggers) undo is disabled so the undo stack is not polluted.
    """
    if not undoable:
        nuke.Undo.disable()
    try:
        all_nodes = [n for n in nuke.allNodes() if n.Class() not in NODES_TO_SKIP]
        if not all_nodes:
            return

        # Read all node positions once upfront. Store as mutable lists so
        # in-flight pushes update the cache without re-reading from Nuke.
        # Layout: [left, top, right, bottom]
        nodes_by_name = {}
        position_cache = {}
        for node in all_nodes:
            node_name = node.name()
            x = node.xpos()
            y = node.ypos()
            nodes_by_name[node_name] = node
            position_cache[node_name] = [x, y, x + node.screenWidth(), y + node.screenHeight()]

        # Sort top-to-bottom; use name as tiebreaker for determinism.
        sorted_node_names = sorted(
            position_cache,
            key=lambda node_name: (position_cache[node_name][1], node_name)
        )

        for node_index, node_name in enumerate(sorted_node_names):
            node_bbox = position_cache[node_name]

            # Find the maximum bottom edge among all preceding nodes that
            # overlap this node horizontally.
            max_predecessor_bottom = None
            for predecessor_index in range(node_index):
                predecessor_bbox = position_cache[sorted_node_names[predecessor_index]]
                if _bboxes_overlap_horizontally(node_bbox, predecessor_bbox) and (
                    max_predecessor_bottom is None or predecessor_bbox[3] > max_predecessor_bottom
                ):
                    max_predecessor_bottom = predecessor_bbox[3]

            if max_predecessor_bottom is None:
                continue

            required_top = max_predecessor_bottom + MINIMUM_GAP
            if required_top > node_bbox[1]:
                push_amount = required_top - node_bbox[1]
                node_bbox[1] += push_amount
                node_bbox[3] += push_amount
                nodes_by_name[node_name].setYpos(int(node_bbox[1]))

    finally:
        if not undoable:
            nuke.Undo.enable()


def _bboxes_overlap(bbox_a, bbox_b):
    return not (
        bbox_a[0] > bbox_b[2] or bbox_a[2] < bbox_b[0]
        or bbox_a[1] > bbox_b[3] or bbox_a[3] < bbox_b[1]
    )


def _bboxes_overlap_horizontally(bbox_a, bbox_b):
    """True if the X ranges of two bboxes intersect (touching edges excluded)."""
    return bbox_a[2] > bbox_b[0] and bbox_a[0] < bbox_b[2]
