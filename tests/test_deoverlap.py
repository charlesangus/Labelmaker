
import nuke

from labelmaker_deoverlap import (
    MINIMUM_GAP,
    _bboxes_overlap,
    _bboxes_overlap_horizontally,
    deoverlap_from_nodes,
)

# --- _bboxes_overlap ---
# bbox format: [left, top, right, bottom]


def test_clearly_overlapping_boxes():
    a = [0, 0, 100, 100]
    b = [50, 50, 150, 150]
    assert _bboxes_overlap(a, b) is True


def test_clearly_separated_boxes_horizontally():
    a = [0, 0, 100, 100]
    b = [200, 0, 300, 100]
    assert _bboxes_overlap(a, b) is False


def test_clearly_separated_boxes_vertically():
    a = [0, 0, 100, 50]
    b = [0, 200, 100, 300]
    assert _bboxes_overlap(a, b) is False


def test_touching_edges_count_as_overlapping():
    a = [0, 0, 100, 100]
    b = [100, 0, 200, 100]
    assert _bboxes_overlap(a, b) is True


def test_one_box_inside_another():
    outer = [0, 0, 200, 200]
    inner = [50, 50, 150, 150]
    assert _bboxes_overlap(outer, inner) is True


def test_overlap_is_symmetric():
    a = [0, 0, 100, 100]
    b = [80, 80, 180, 180]
    assert _bboxes_overlap(a, b) == _bboxes_overlap(b, a)


def test_zero_size_box_touching_another():
    point = [50, 50, 50, 50]
    box = [0, 0, 100, 100]
    assert _bboxes_overlap(point, box) is True


# --- _bboxes_overlap_horizontally ---
# touching edges are NOT counted (strict less-than comparison)


def test_horizontally_overlapping():
    a = [0, 0, 100, 100]
    b = [50, 200, 150, 300]
    assert _bboxes_overlap_horizontally(a, b) is True


def test_horizontally_separated():
    a = [0, 0, 100, 100]
    b = [200, 0, 300, 100]
    assert _bboxes_overlap_horizontally(a, b) is False


def test_horizontally_touching_edges_not_overlapping():
    a = [0, 0, 100, 100]
    b = [100, 0, 200, 100]
    assert _bboxes_overlap_horizontally(a, b) is False


def test_horizontal_overlap_symmetric():
    a = [0, 0, 100, 50]
    b = [80, 200, 180, 300]
    assert _bboxes_overlap_horizontally(a, b) == _bboxes_overlap_horizontally(b, a)


def test_horizontal_overlap_ignores_y():
    a = [0, 0, 100, 10]
    b = [50, 999, 150, 1999]
    assert _bboxes_overlap_horizontally(a, b) is True


# --- MINIMUM_GAP constant ---


def test_minimum_gap_is_positive():
    assert MINIMUM_GAP > 0


# --- deoverlap_from_nodes ---


class _FakeNode:
    def __init__(self, name, x, y, width, height, node_class="Blur"):
        self._name = name
        self._x = x
        self._y = y
        self._width = width
        self._height = height
        self._class = node_class

    def name(self):
        return self._name

    def Class(self):
        return self._class

    def xpos(self):
        return self._x

    def ypos(self):
        return self._y

    def screenWidth(self):
        return self._width

    def screenHeight(self):
        return self._height

    def setYpos(self, y):
        self._y = y

    def set_height(self, height):
        self._height = height


def _bbox(node):
    return [
        node.xpos(),
        node.ypos(),
        node.xpos() + node.screenWidth(),
        node.ypos() + node.screenHeight(),
    ]


def _no_overlaps(nodes):
    return all(
        not _bboxes_overlap(_bbox(a), _bbox(b))
        for i, a in enumerate(nodes)
        for b in nodes[i + 1:]
    )


def test_cascade_does_not_land_a_short_node_on_the_last_pusher(monkeypatch):
    grown = [_FakeNode(f"grown_{i}", 0, i * 90, 100, 110) for i in range(8)]
    dot = _FakeNode("dot", 0, 720, 100, 12, node_class="Dot")
    nodes = grown + [dot]
    monkeypatch.setattr(nuke, "allNodes", lambda: nodes)

    deoverlap_from_nodes([n.name() for n in grown])

    assert _no_overlaps(nodes)
    last = grown[-1]
    assert dot.ypos() == last.ypos() + last.screenHeight() + MINIMUM_GAP


def test_two_fires_leave_no_overlap(monkeypatch):
    nodes = [_FakeNode(f"n{i}", 0, i * 90, 100, 30) for i in range(8)]
    monkeypatch.setattr(nuke, "allNodes", lambda: nodes)

    for node in nodes[:4]:
        node.set_height(110)
    deoverlap_from_nodes([n.name() for n in nodes[:4]])

    for node in nodes[4:]:
        node.set_height(110)
    deoverlap_from_nodes([n.name() for n in nodes[4:]])

    assert _no_overlaps(nodes)


def test_pusher_that_jumped_past_a_bystander_leaves_it_alone(monkeypatch):
    pusher = _FakeNode("P", 0, 0, 50, 200)
    wide = _FakeNode("X", 0, 10, 300, 20)
    dot = _FakeNode("Dot", 100, 50, 50, 10)
    nodes = [pusher, wide, dot]
    monkeypatch.setattr(nuke, "allNodes", lambda: nodes)

    deoverlap_from_nodes(["P"])

    assert dot.ypos() == 50


def test_side_by_side_and_preexisting_overlaps_untouched(monkeypatch):
    grown = _FakeNode("G", 0, 0, 50, 110)
    side = _FakeNode("Side", 200, 20, 50, 50)
    a = _FakeNode("A", 500, 1000, 50, 50)
    b = _FakeNode("B", 500, 1020, 50, 50)
    nodes = [grown, side, a, b]
    monkeypatch.setattr(nuke, "allNodes", lambda: nodes)

    deoverlap_from_nodes(["G"])

    assert side.ypos() == 20
    assert a.ypos() == 1000
    assert b.ypos() == 1020
