import pytest

from labelmaker import AutolabelReplacement


class _EmptyConfig:
    def get(self, key, default=None):
        return default


@pytest.fixture
def labeller():
    return AutolabelReplacement(_EmptyConfig())


# --- format_knob_value ---


def test_format_knob_value_float_rounds_to_three_decimals(labeller):
    assert labeller.format_knob_value(1.23456789) == "1.235"


def test_format_knob_value_exact_float(labeller):
    assert labeller.format_knob_value(0.5) == "0.500"


def test_format_knob_value_integer_becomes_string(labeller):
    assert labeller.format_knob_value(42) == "42"


def test_format_knob_value_string_passes_through(labeller):
    assert labeller.format_knob_value("rgba") == "rgba"


def test_format_knob_value_zero(labeller):
    assert labeller.format_knob_value(0.0) == "0.000"


# --- format_knob_values ---


def test_format_knob_values_list_of_floats(labeller):
    result = labeller.format_knob_values([1.0, 2.5, 0.123456])
    assert result == "1.000, 2.500, 0.123"


def test_format_knob_values_single_element(labeller):
    assert labeller.format_knob_values([3.14159]) == "3.142"


def test_format_knob_values_mixed_types(labeller):
    result = labeller.format_knob_values([1.5, "rgba"])
    assert result == "1.500, rgba"


# --- clamp ---


def test_clamp_value_in_range(labeller):
    assert labeller.clamp(0.5) == 0.5


def test_clamp_below_min(labeller):
    assert labeller.clamp(-1.0) == 0.0


def test_clamp_above_max(labeller):
    assert labeller.clamp(2.0) == 1.0


def test_clamp_at_boundaries(labeller):
    assert labeller.clamp(0.0) == 0.0
    assert labeller.clamp(1.0) == 1.0


def test_clamp_custom_range(labeller):
    assert labeller.clamp(5.0, low=2.0, high=10.0) == 5.0
    assert labeller.clamp(1.0, low=2.0, high=10.0) == 2.0
    assert labeller.clamp(15.0, low=2.0, high=10.0) == 10.0


# --- colorize_knob_readout ---


def test_colorize_float_produces_html_span(labeller):
    result = labeller.colorize_knob_readout(0.5, "Gain", "0.500")
    assert "<span" in result
    assert "background-color" in result
    assert "Gain" in result


def test_colorize_rgb_list_produces_html_span(labeller):
    result = labeller.colorize_knob_readout([1.0, 0.0, 0.0, 1.0], "Color", "1.000, 0.000, 0.000")
    assert "<span" in result
    assert "Color" in result


def test_colorize_non_numeric_returns_formatted_value(labeller):
    result = labeller.colorize_knob_readout("rgba", "Channels", "rgba")
    assert result == "rgba"


def test_colorize_bright_color_uses_black_text(labeller):
    result = labeller.colorize_knob_readout([1.0, 1.0, 1.0, 1.0], "Color", "1.000")
    assert "black" in result


def test_colorize_dark_color_uses_white_text(labeller):
    result = labeller.colorize_knob_readout([0.0, 0.0, 0.0, 1.0], "Color", "0.000")
    assert "white" in result


def test_colorize_grey_float_produces_grey_background(labeller):
    result = labeller.colorize_knob_readout(0.0, "Gain", "0.000")
    assert "#000000" in result or "00;00;00" in result or "color" in result


# --- name_line_creator ---


def test_name_line_creator_default_name(labeller):
    labeller.lines = []
    labeller.node_true_class = "Blur"
    labeller.node_class = "Blur"
    labeller.node_name = "Blur1"
    labeller.name_line_creator()
    assert labeller.lines == ["Blur1"]


def test_name_line_creator_renamed_node_shows_class(labeller):
    labeller.lines = []
    labeller.node_true_class = "Blur"
    labeller.node_class = "Blur"
    labeller.node_name = "MySoftenNode"
    labeller.name_line_creator()
    assert labeller.lines == ["Blur | MySoftenNode"]


def test_name_line_creator_nameless_node_skips(labeller):
    labeller.lines = []
    labeller.node_true_class = "Dot"
    labeller.node_class = "Dot"
    labeller.node_name = "Dot1"
    result = labeller.name_line_creator()
    assert result is None
    assert labeller.lines == []


def test_name_line_creator_group_never_shows_class(labeller):
    labeller.lines = []
    labeller.node_true_class = "Group"
    labeller.node_class = "Group"
    labeller.node_name = "MyEffect"
    labeller.name_line_creator()
    assert labeller.lines == ["MyEffect"]
