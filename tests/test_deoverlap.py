
from labelmaker_deoverlap import MINIMUM_GAP, _bboxes_overlap, _bboxes_overlap_horizontally

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
