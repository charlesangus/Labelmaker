"""Generate a large, realistic .nk script for Labelmaker profiling.

    LM_OUT=/tmp/lm_big.nk LM_NODES=800 nuke -t .profiling/build_big_script.py

Builds N nodes across a grid of independent chains, using the node classes and
knob values Labelmaker actually reads (Read file paths, Merge mixes, Grade/
ColorCorrect values, masks, node labels with TCL expressions).
"""
import random
import sys

import nuke

# Nuke only forwards the first argument after the script, so take the settings
# from the environment instead:  LM_OUT=/tmp/lm_big.nk LM_NODES=800 nuke -t ...
import os

OUT = os.environ.get("LM_OUT") or (sys.argv[1] if len(sys.argv) > 1 else "/tmp/lm_big.nk")
TARGET = int(os.environ.get("LM_NODES", "800"))

random.seed(4)

FILES = [
    "/jobs/SHOW/seq010/sh0{0:02d}/plates/main/v0{1:02d}/SHOW_010_0{0:02d}_main_v0{1:02d}.%04d.exr",
    "/jobs/SHOW/seq010/sh0{0:02d}/renders/cg/v0{1:02d}/beauty.%04d.exr",
    "/jobs/SHOW/assets/textures/grunge_0{0:02d}_v0{1:02d}.tif",
]


def make_chain(x, y, index):
    """One realistic comp chain; returns the nodes created."""
    made = []
    read = nuke.createNode("Read", inpanel=False)
    read.knob("file").setValue(random.choice(FILES).format(index % 60, index % 12))
    read.knob("first").setValue(1001)
    read.knob("last").setValue(1120)
    read.knob("origfirst").setValue(1001)
    read.knob("origlast").setValue(1120)
    read.knob("colorspace").setValue(random.choice(["scene_linear", "sRGB", "rec709"]))
    made.append(read)

    grade = nuke.createNode("Grade", inpanel=False)
    grade.knob("white").setValue(round(random.uniform(0.5, 2.0), 3))
    grade.knob("gamma").setValue(round(random.uniform(0.8, 1.4), 3))
    grade.knob("mix").setValue(round(random.uniform(0.3, 1.0), 2))
    made.append(grade)

    cc = nuke.createNode("ColorCorrect", inpanel=False)
    cc.knob("saturation").setValue(round(random.uniform(0.5, 1.5), 3))
    made.append(cc)

    blur = nuke.createNode("Blur", inpanel=False)
    blur.knob("size").setValue(round(random.uniform(1, 40), 1))
    blur.knob("channels").setValue(random.choice(["rgba", "rgb", "alpha"]))
    made.append(blur)

    transform = nuke.createNode("Transform", inpanel=False)
    transform.knob("translate").setValue([random.uniform(-200, 200), random.uniform(-200, 200)])
    transform.knob("scale").setValue(round(random.uniform(0.8, 1.2), 3))
    made.append(transform)

    shuffle = nuke.createNode("Shuffle2", inpanel=False)
    made.append(shuffle)

    bg = nuke.createNode("Constant", inpanel=False)
    made.append(bg)

    merge = nuke.createNode("Merge2", inpanel=False)
    merge.setInput(0, bg)
    merge.setInput(1, transform)
    merge.knob("operation").setValue(random.choice(["over", "plus", "screen", "multiply"]))
    merge.knob("mix").setValue(round(random.uniform(0.2, 1.0), 2))
    made.append(merge)

    dot = nuke.createNode("Dot", inpanel=False)
    made.append(dot)

    # every third chain gets a hand-written label, one with a TCL expression
    if index % 3 == 0:
        grade.knob("label").setValue("key light\nbalance pass")
    if index % 7 == 0:
        cc.knob("label").setValue("[value saturation] sat")

    # lay the chain out in a column
    for row, node in enumerate(made):
        node.setXYpos(x, y + row * 90)
    return made


def main():
    nuke.scriptClear()
    total = 0
    index = 0
    per_chain = 9
    columns = max(1, int((TARGET / per_chain) ** 0.5))
    while total < TARGET:
        col = index % columns
        row = index // columns
        made = make_chain(col * 260, row * (per_chain * 90 + 200), index)
        total += len(made)
        index += 1
    for node in nuke.allNodes():
        node.setSelected(False)
    nuke.scriptSaveAs(OUT, overwrite=1)
    print("wrote {} with {} nodes".format(OUT, len(nuke.allNodes())))


main()
