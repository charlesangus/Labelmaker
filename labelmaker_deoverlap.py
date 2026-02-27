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
    return (
        node.xpos(),
        node.ypos(),
        node.xpos() + node.screenWidth(),
        node.ypos() + node.screenHeight(),
    )


def _bboxes_overlap(bbox_a, bbox_b):
    return not (
        bbox_a[0] > bbox_b[2] or bbox_a[2] < bbox_b[0]
        or bbox_a[1] > bbox_b[3] or bbox_a[3] < bbox_b[1]
    )
