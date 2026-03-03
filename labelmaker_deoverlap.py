import nuke

NODES_TO_SKIP = ('BackdropNode', 'Viewer')
MINIMUM_GAP = 6  # DAG units of breathing room between nodes after de-overlap


def deoverlap_downstream(source_node):
    """Push graph descendants of source_node down if they overlap it after a height increase."""
    nuke.Undo.disable()
    try:
        _deoverlap_chain(source_node, set())
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
        position_cache = {}
        for node in all_nodes:
            x = node.xpos()
            y = node.ypos()
            position_cache[node.name()] = [x, y, x + node.screenWidth(), y + node.screenHeight()]

        # Sort top-to-bottom; use name as tiebreaker for determinism.
        sorted_nodes = sorted(
            all_nodes,
            key=lambda node: (position_cache[node.name()][1], node.name())
        )

        for node_index, node in enumerate(sorted_nodes):
            node_name = node.name()
            node_bbox = position_cache[node_name]

            # Find the maximum bottom edge among all preceding nodes that
            # overlap this node horizontally.
            max_predecessor_bottom = None
            for predecessor in sorted_nodes[:node_index]:
                predecessor_bbox = position_cache[predecessor.name()]
                if _bboxes_overlap_horizontally(node_bbox, predecessor_bbox):
                    if max_predecessor_bottom is None or predecessor_bbox[3] > max_predecessor_bottom:
                        max_predecessor_bottom = predecessor_bbox[3]

            if max_predecessor_bottom is None:
                continue

            required_top = max_predecessor_bottom + MINIMUM_GAP
            if required_top > node_bbox[1]:
                push_amount = required_top - node_bbox[1]
                node_bbox[1] += push_amount
                node_bbox[3] += push_amount
                node.setYpos(int(node_bbox[1]))

    finally:
        if not undoable:
            nuke.Undo.enable()


def _deoverlap_chain(node, visited):
    if node.name() in visited:
        return
    visited.add(node.name())
    source_bbox = _node_bbox(node)
    for downstream_node in node.dependent(nuke.INPUTS):
        if downstream_node.Class() in NODES_TO_SKIP:
            continue
        down_bbox = _node_bbox(downstream_node)
        # Only consider nodes physically below the source to avoid pushing sideways branches
        if down_bbox[1] <= source_bbox[1]:
            continue
        if _bboxes_overlap(source_bbox, down_bbox):
            push_amount = source_bbox[3] - down_bbox[1] + MINIMUM_GAP
            downstream_node.setYpos(downstream_node.ypos() + push_amount)
        # Always recurse in case this node now overlaps its own descendants
        _deoverlap_chain(downstream_node, visited)


def _node_bbox(node):
    """Return bounding box in DAG node coordinates (same units as xpos/ypos)."""
    x = node.xpos()
    y = node.ypos()
    return (x, y, x + node.screenWidth(), y + node.screenHeight())


def _bboxes_overlap(bbox_a, bbox_b):
    return not (
        bbox_a[0] > bbox_b[2] or bbox_a[2] < bbox_b[0]
        or bbox_a[1] > bbox_b[3] or bbox_a[3] < bbox_b[1]
    )


def _bboxes_overlap_horizontally(bbox_a, bbox_b):
    """True if the X ranges of two bboxes intersect (touching edges excluded)."""
    return bbox_a[2] > bbox_b[0] and bbox_a[0] < bbox_b[2]
