"""Build docs/screenshots/features.nk — the DAG that the screenshotter captures.

Run headlessly to regenerate the .nk after changing which features are shown:

    nuke -t docs/screenshots/build_features_nk.py

Each cluster of nodes is wrapped in a BackdropNode whose label starts with
``screenshot:``; nuke-screenshotter turns one backdrop into one PNG named
after the slug of the label (e.g. ``screenshot:regular knobs`` -> ``regular_knobs.png``).
Knob values are chosen so the relevant Labelmaker lines appear on each node.

The committed features.nk is what the screenshotter consumes — Nuke is only needed
to regenerate it, not to use it.
"""
import os

import nuke

# Grid layout: clusters are laid out in a grid of cells; each cell holds one
# cluster. The backdrop is NOT the cell — it is sized to hug the cluster's nodes
# (see wrap_backdrop) so the screenshotter, which crops to the backdrop, frames
# each example the same way regardless of how many nodes it has. Coordinates are
# DAG units. Cells are only spacers: they must be larger than the biggest
# backdrop so neighbouring backdrops never overlap.
CELL_WIDTH = 560
CELL_HEIGHT = 740
COLUMNS = 4
NODE_Y_STEP = 95

# Where the first node of a cluster sits inside its cell. Chosen so the backdrop
# (node bounding box grown by BACKDROP_MARGIN, plus the label/arrow allowances
# below) starts at the cell's own origin and stays inside the cell.
NODE_X_OFFSET = 150
NODE_Y_OFFSET = 165

# The captured backdrop = the cluster's node bounding box, grown by this margin
# on every side so the example is centred with even breathing room around it.
BACKDROP_MARGIN = 150

# A bare node tile is roughly this size in DAG units; Nuke reports 0 for
# screenWidth/screenHeight in headless `nuke -t`, so we use nominal values.
NODE_WIDTH = 80
NODE_HEIGHT = 18

# Labelmaker's autolabel draws several lines BELOW the tile and Nuke draws an
# input-arrow stub ABOVE the top node. Those are not loaded when this script
# builds the .nk, so we reserve room for them explicitly; this keeps the visible
# content (arrow + tiles + label block) centred inside the margin rather than
# the bare tiles.
LABEL_BLOCK_BELOW = 60
INPUT_ARROW_ABOVE = 15

# Shift the whole grid off the origin. Nuke omits xpos/ypos knobs when they are 0
# (their default), and the screenshotter's parser skips any backdrop missing those
# knobs — so a backdrop whose left/top edge lands on 0 silently drops out of the
# capture. Keeping every coordinate positive and non-zero avoids that.
GRID_ORIGIN_X = 100
GRID_ORIGIN_Y = 100

_cluster_index = 0


def _cell_origin(index):
    column = index % COLUMNS
    row = index // COLUMNS
    return GRID_ORIGIN_X + column * CELL_WIDTH, GRID_ORIGIN_Y + row * CELL_HEIGHT


def start_cluster():
    """Return (node_x, node_top) — the top-left anchor for the next cluster's nodes."""
    global _cluster_index
    origin_x, origin_y = _cell_origin(_cluster_index)
    _cluster_index += 1
    return origin_x + NODE_X_OFFSET, origin_y + NODE_Y_OFFSET


def place(node, node_x, node_top, row):
    node["xpos"].setValue(node_x)
    node["ypos"].setValue(node_top + row * NODE_Y_STEP)
    return node


def wrap_backdrop(label, nodes):
    """Draw a screenshot: backdrop hugging ``nodes`` with an even margin around them."""
    min_x = min(node.xpos() for node in nodes)
    min_y = min(node.ypos() for node in nodes)
    max_x = max(node.xpos() + NODE_WIDTH for node in nodes)
    max_y = max(node.ypos() + NODE_HEIGHT for node in nodes)

    left = min_x - BACKDROP_MARGIN
    top = min_y - INPUT_ARROW_ABOVE - BACKDROP_MARGIN
    right = max_x + BACKDROP_MARGIN
    bottom = max_y + LABEL_BLOCK_BELOW + BACKDROP_MARGIN

    backdrop = nuke.nodes.BackdropNode()
    backdrop["label"].setValue("screenshot:" + label)
    backdrop["note_font_size"].setValue(28)
    backdrop["tile_color"].setValue(0x556699FF)
    backdrop["xpos"].setValue(int(left))
    backdrop["ypos"].setValue(int(top))
    backdrop["bdwidth"].setValue(int(right - left))
    backdrop["bdheight"].setValue(int(bottom - top))
    return backdrop


# --- example (hero): a small connected comp showing several label kinds --------
node_x, node_top = start_cluster()
read_hero = place(nuke.nodes.Read(), node_x, node_top, 0)
read_hero["file"].setValue("/jobs/ENG/sh010/plates/bg_main_v003.%04d.exr")
grade_hero = place(nuke.nodes.Grade(), node_x, node_top, 1)
grade_hero["white"].setValue([0.85, 0.45, 0.2, 1.0])
grade_hero["multiply"].setValue([1.1, 0.95, 0.8, 1.0])
grade_hero.setInput(0, read_hero)
blur_hero = place(nuke.nodes.Blur(), node_x, node_top, 2)
blur_hero["size"].setValue(8)
blur_hero.setInput(0, grade_hero)
write_hero = place(nuke.nodes.Write(), node_x, node_top, 3)
write_hero["file"].setValue("/jobs/ENG/sh010/comp/sh010_comp_v012.%04d.exr")
write_hero.setInput(0, blur_hero)
wrap_backdrop("example", [read_hero, grade_hero, blur_hero, write_hero])

# --- regular knobs: Labelmaker reads out ordinary knob values -----------------
# The core behaviour: adjusted knobs show right on the tile. A Blur's size and a
# Transform's translate/rotate appear without opening a single properties panel.
node_x, node_top = start_cluster()
blur_regular = place(nuke.nodes.Blur(), node_x, node_top, 0)
blur_regular["size"].setValue(8)
transform_regular = place(nuke.nodes.Transform(), node_x, node_top, 1)
transform_regular["translate"].setValue([35, 14])
transform_regular["rotate"].setValue(8)
transform_regular.setInput(0, blur_regular)
wrap_backdrop("regular knobs", [blur_regular, transform_regular])

# --- grade: colour swatches on Color knobs ------------------------------------
node_x, node_top = start_cluster()
grade_colour = place(nuke.nodes.Grade(), node_x, node_top, 0)
grade_colour["white"].setValue([0.85, 0.4, 0.2, 1.0])
grade_colour["multiply"].setValue([1.15, 0.95, 0.75, 1.0])
grade_colour["gamma"].setValue([1.1, 1.0, 0.95, 1.0])
wrap_backdrop("grade", [grade_colour])

# --- tcl: config-driven TCL string on a Shuffle -------------------------------
node_x, node_top = start_cluster()
shuffle_node = place(nuke.nodes.Shuffle(), node_x, node_top, 0)
wrap_backdrop("tcl", [shuffle_node])

# --- channel ops: channels, mask/unpremult and mix on one Grade, plus a Merge --
# One example carrying every channel-related readout: the Grade shows its channel
# subset, channel mask, (un)premult and mix; the Merge shows its operation and the
# channels flowing through it.
node_x, node_top = start_cluster()
channel_read_a = nuke.nodes.Read()
channel_read_a["file"].setValue("/jobs/ENG/sh010/elements/fx_smoke_v002.%04d.exr")
channel_read_a["xpos"].setValue(node_x + 120)
channel_read_a["ypos"].setValue(node_top)
channel_read_b = place(nuke.nodes.Read(), node_x, node_top, 0)
channel_read_b["file"].setValue("/jobs/ENG/sh010/plates/bg_main_v003.%04d.exr")
channel_grade = place(nuke.nodes.Grade(), node_x, node_top, 2)
channel_grade["channels"].setValue("rgb")
channel_grade["maskChannelInput"].setValue("rgba.red")
channel_grade["unpremult"].setValue("rgba.alpha")
channel_grade["white"].setValue(0.8)
channel_grade["mix"].setValue(0.5)
channel_grade.setInput(0, channel_read_b)
channel_merge = place(nuke.nodes.Merge2(), node_x, node_top, 4)
channel_merge["operation"].setValue("plus")
channel_merge.setInput(0, channel_grade)
channel_merge.setInput(1, channel_read_a)
wrap_backdrop("channel ops", [channel_read_a, channel_read_b, channel_grade, channel_merge])

# --- regular label: the node label knob still works ---------------------------
node_x, node_top = start_cluster()
grade_label = place(nuke.nodes.Grade(), node_x, node_top, 0)
grade_label["label"].setValue("key light\nbalance to hero")
grade_label["white"].setValue(0.9)
wrap_backdrop("regular label", [grade_label])

# --- other: node-class disambiguation + Read/Write file basenames -------------
# The grab-bag shot: a renamed Transform reading "Transform | guy" (node class),
# above a Read -> Write chain showing their file basenames (file readout).
node_x, node_top = start_cluster()
transform_named = place(nuke.nodes.Transform(), node_x, node_top, 0)
transform_named.setName("guy")
transform_named["translate"].setValue([35, 14])
transform_named["rotate"].setValue(8)
other_read = place(nuke.nodes.Read(), node_x, node_top, 2)
other_read["file"].setValue("/jobs/ENG/sh010/plates/bg_main_v003.%04d.exr")
other_write = place(nuke.nodes.Write(), node_x, node_top, 3)
other_write["file"].setValue("/jobs/ENG/sh010/comp/sh010_comp_v012.%04d.exr")
other_write.setInput(0, other_read)
wrap_backdrop("other", [transform_named, other_read, other_write])

# --- deoverlap: a clean, tidy vertical chain ----------------------------------
node_x, node_top = start_cluster()
read_clean = place(nuke.nodes.Read(), node_x, node_top, 0)
read_clean["file"].setValue("/jobs/ENG/sh020/plates/bg_v001.%04d.exr")
grade_clean = place(nuke.nodes.Grade(), node_x, node_top, 1)
grade_clean["white"].setValue(0.95)
grade_clean.setInput(0, read_clean)
blur_clean = place(nuke.nodes.Blur(), node_x, node_top, 2)
blur_clean["size"].setValue(4)
blur_clean.setInput(0, grade_clean)
write_clean = place(nuke.nodes.Write(), node_x, node_top, 3)
write_clean["file"].setValue("/jobs/ENG/sh020/comp/sh020_comp_v001.%04d.exr")
write_clean.setInput(0, blur_clean)
wrap_backdrop("deoverlap", [read_clean, grade_clean, blur_clean, write_clean])

output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "features.nk")
nuke.scriptSaveToTemp(output_path)
print("WROTE " + output_path)
