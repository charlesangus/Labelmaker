"""Generate a large, diverse .nk script for Labelmaker profiling.

    LM_OUT=/tmp/lm_3000.nk LM_NODES=3000 nuke -t .profiling/build_diverse_script.py

Builds N top-level nodes from a mix of comp-chain templates: plates, Keylight
(OFX) keys, roto/copy/premult, CG shuffles, FX branches, and Groups (a few
nested) with real innards. A handful of shared "control" NoOps carry user
knobs that many nodes reference through expressions; some knobs are animated;
a few nodes are clones; some chains sit in backdrops; some labels use TCL.
Reports top-level and recursive node counts.
"""
import os
import random
import sys

import nuke

OUT = os.environ.get("LM_OUT") or (sys.argv[1] if len(sys.argv) > 1 else "/tmp/lm_diverse.nk")
TARGET = int(os.environ.get("LM_NODES", "3000"))

random.seed(11)

PLATES = [
    "/jobs/SHOW/seq{s:03d}/sh{s:03d}_{n:04d}/plates/main/v{v:03d}/SHOW_{s:03d}_{n:04d}_main_v{v:03d}.%04d.exr",
    "/jobs/SHOW/seq{s:03d}/sh{s:03d}_{n:04d}/renders/cg/beauty/v{v:03d}/beauty.%04d.exr",
    "/jobs/SHOW/seq{s:03d}/sh{s:03d}_{n:04d}/renders/cg/utility/v{v:03d}/utility.%04d.exr",
    "/jobs/SHOW/assets/textures/grunge_{n:03d}_v{v:03d}.tif",
    "/jobs/SHOW/elements/smoke/smoke_{n:03d}/smoke_{n:03d}.%04d.exr",
]
OFX_KEYLIGHT = "OFXuk.co.thefoundry.keylight.keylight_v201"

controls = []     # shared NoOps with user knobs (expression targets)
grades = []       # Grade nodes that later expressions may link to
made_count = {"top": 0}


def new(cls, **knobs):
    node = nuke.createNode(cls, inpanel=False)
    for name, value in knobs.items():
        with_knob = node.knob(name)
        if with_knob is not None:
            with_knob.setValue(value)
    return node


def plate(index):
    node = new("Read")
    node["file"].setValue(random.choice(PLATES).format(s=index % 40 + 10, n=index % 90, v=index % 12 + 1))
    node["first"].setValue(1001)
    node["last"].setValue(1001 + random.choice([48, 96, 120, 240]))
    node["origfirst"].setValue(1001)
    node["origlast"].setValue(node["last"].value())
    node["colorspace"].setValue(random.choice(["scene_linear", "sRGB", "rec709", "ACES - ACEScg"]))
    return node


def animate(node, knob, first=1001, last=1100):
    k = node[knob]
    k.setAnimated()
    for f in range(first, last + 1, random.choice([12, 24, 48])):
        k.setValueAt(round(random.uniform(0.5, 2.0), 3), f)


def link(node, knob, target, target_knob, scale=1.0):
    node[knob].setExpression("{}.{}*{}".format(target.name(), target_knob, scale))


def make_control(index):
    node = new("NoOp", name="CTRL_{:02d}".format(index))
    node.addKnob(nuke.Double_Knob("gain", "gain"))
    node.addKnob(nuke.Double_Knob("softness", "softness"))
    node.addKnob(nuke.Double_Knob("amount", "amount"))
    node["gain"].setValue(round(random.uniform(0.6, 1.6), 3))
    node["softness"].setValue(round(random.uniform(1, 30), 1))
    node["amount"].setValue(round(random.uniform(0, 1), 2))
    node["label"].setValue("gain [value gain]\nsoft [value softness]")
    node["tile_color"].setValue(0xAA5500FF)
    return [node]


# ----------------------------------------------------------------------
# chain templates: each returns the nodes it created (top level only)
# ----------------------------------------------------------------------
def chain_plate(index):
    nodes = []
    read = plate(index)
    nodes.append(read)
    nodes.append(new("Reformat", type="to box", box_width=2048, box_height=1080))
    grade = new("Grade", white=round(random.uniform(0.5, 2.0), 3), gamma=round(random.uniform(0.8, 1.4), 3),
                mix=round(random.uniform(0.3, 1.0), 2))
    nodes.append(grade)
    grades.append(grade)
    if controls and index % 3 == 0:
        link(grade, "multiply", random.choice(controls), "gain")
    elif index % 5 == 0:
        animate(grade, "white")
    nodes.append(new("ColorCorrect", saturation=round(random.uniform(0.5, 1.5), 3)))
    blur = new("Blur", size=round(random.uniform(1, 40), 1), channels=random.choice(["rgba", "rgb", "alpha"]))
    nodes.append(blur)
    if controls and index % 4 == 0:
        link(blur, "size", random.choice(controls), "softness")
    transform = new("Transform", scale=round(random.uniform(0.8, 1.2), 3))
    transform["translate"].setValue([random.uniform(-200, 200), random.uniform(-200, 200)])
    if index % 6 == 0:
        transform["translate"].setExpression("frame*2", 0)
    nodes.append(transform)
    bg = new("Constant")
    nodes.append(bg)
    merge = new("Merge2", operation=random.choice(["over", "plus", "screen", "multiply"]),
                mix=round(random.uniform(0.2, 1.0), 2))
    merge.setInput(0, bg)
    merge.setInput(1, transform)
    nodes.append(merge)
    nodes.append(new("Dot"))
    return nodes


def chain_key(index):
    nodes = []
    nodes.append(plate(index))
    try:
        key = new(OFX_KEYLIGHT)
        key["screenColour"].setValue([0.1, 0.7, 0.2])
        nodes.append(key)
    except RuntimeError:
        nodes.append(new("Keyer"))
    nodes.append(new("Premult"))
    nodes.append(new("EdgeBlur", size=round(random.uniform(1, 8), 1)))
    holdout = new("Roto")
    nodes.append(holdout)
    merge = new("Merge2", operation="over")
    merge.setInput(1, nodes[-2])
    merge.setInput(0, new("Constant", color=[0.1, 0.1, 0.1, 1]))
    nodes.append(merge.input(0))
    merge.setInput(2, holdout)   # mask input connected -> Labelmaker's channels line
    nodes.append(merge)
    if index % 2 == 0:
        nodes.append(new("Grade", label="[value mix] mix", mix=round(random.uniform(0.2, 0.9), 2)))
    nodes.append(new("Write", file="/jobs/SHOW/comp/v001/comp.%04d.exr", channels="rgba"))
    return nodes


def chain_roto(index):
    nodes = []
    nodes.append(new("Roto"))
    nodes.append(new("Blur", size=round(random.uniform(0.5, 5), 1), channels="alpha"))
    nodes.append(plate(index))
    copy = new("Copy", from0="rgba.alpha", to0="rgba.alpha")
    copy.setInput(1, nodes[1])
    copy.setInput(0, nodes[2])
    nodes.append(copy)
    nodes.append(new("Premult"))
    nodes.append(new("Unpremult"))
    nodes.append(new("Dot"))
    return nodes


def chain_cg(index):
    nodes = []
    nodes.append(plate(index))
    nodes.append(new("Shuffle2", label="[value in1] -> [value out1]"))
    nodes.append(new("Unpremult"))
    grade = new("Grade", white=round(random.uniform(0.6, 1.8), 3), maskChannelInput=random.choice(["none", "rgba.alpha", "alpha"]))
    nodes.append(grade)
    grades.append(grade)
    if grades and index % 2 == 0:
        link(grade, "gamma", random.choice(grades), "gamma", 0.5)
    nodes.append(new("HueCorrect"))
    nodes.append(new("Premult"))
    nodes.append(new("ZDefocus2", size=round(random.uniform(5, 30), 1)))
    nodes.append(new("Crop"))
    nodes.append(new("Dot"))
    return nodes


def chain_fx(index):
    nodes = []
    nodes.append(new("Noise", size=round(random.uniform(50, 400), 1)))
    nodes.append(new("Radial"))
    nodes.append(new("Multiply", value=round(random.uniform(0.2, 2.0), 2)))
    glow = new("Glow2", tolerance=round(random.uniform(0.3, 0.9), 2))
    if controls:
        link(glow, "brightness", random.choice(controls), "amount", 2.0)
    nodes.append(glow)
    nodes.append(new("Saturation", saturation=round(random.uniform(0, 1.5), 2)))
    nodes.append(new("Switch", which=random.choice([0, 1])))
    nodes.append(new("Dissolve", which=round(random.uniform(0, 1), 2)))
    nodes.append(new("TimeOffset", time_offset=random.randint(-24, 24)))
    nodes.append(new("FrameHold", first_frame=1001 + random.randint(0, 60)))
    nodes.append(new("Dot"))
    return nodes


def chain_group(index, depth=0):
    """A Group with real innards; every third one nests another Group."""
    nodes = [plate(index)]
    group = nuke.createNode("Group", inpanel=False)
    group.setName("Comp_{}{}".format(index, "_inner" if depth else ""))
    if index % 4 == 0:
        group["label"].setValue("[value name] / [value mix]")
    with group:
        inp = nuke.createNode("Input", inpanel=False)
        inner = [inp]
        inner.append(new("Grade", white=round(random.uniform(0.6, 1.8), 3)))
        inner.append(new("Blur", size=round(random.uniform(1, 20), 1)))
        inner.append(new("Transform", rotate=round(random.uniform(-10, 10), 2)))
        if depth == 0 and index % 3 == 0:
            inner_group = nuke.createNode("Group", inpanel=False)
            inner_group.setName("Inner_{}".format(index))
            with inner_group:
                gi = nuke.createNode("Input", inpanel=False)
                g1 = new("Grade", multiply=round(random.uniform(0.5, 1.5), 2))
                g2 = new("Sharpen")
                nuke.createNode("Output", inpanel=False)
                for row, n in enumerate([gi, g1, g2]):
                    n.setXYpos(0, row * 90)
            inner.append(inner_group)
        inner.append(new("ColorCorrect", saturation=round(random.uniform(0.5, 1.5), 2)))
        inner.append(new("Merge2", operation="plus"))
        inner[-1].setInput(1, inner[-3])
        nuke.createNode("Output", inpanel=False)
        for row, n in enumerate(inner):
            n.setXYpos(0, row * 90)
    group.setInput(0, nodes[0])
    nodes.append(group)
    nodes.append(new("Grade", label="post [value white]", white=round(random.uniform(0.8, 1.2), 3)))
    nodes.append(new("Dot"))
    return nodes


CHAINS = [
    (chain_plate, 5),
    (chain_key, 2),
    (chain_roto, 2),
    (chain_cg, 3),
    (chain_fx, 2),
    (chain_group, 2),
]


def pick_chain():
    total = sum(w for _, w in CHAINS)
    r = random.uniform(0, total)
    for fn, w in CHAINS:
        r -= w
        if r <= 0:
            return fn
    return CHAINS[0][0]


def main():
    nuke.scriptClear()
    columns = max(1, int((TARGET / 9.0) ** 0.5))
    index = 0
    clones_made = 0
    while made_count["top"] < TARGET:
        col = index % columns
        row = index // columns
        x, y = col * 260, row * 1200
        if index % 25 == 0:
            made = make_control(len(controls))
            controls.extend(made)
        else:
            made = pick_chain()(index)
        # every 9th chain gets a couple of clones of its Grade
        if index % 9 == 4 and clones_made < TARGET // 50:
            src = next((n for n in made if n.Class() == "Grade"), None)
            if src is not None:
                clone = nuke.clone(src)
                made.append(clone)
                clones_made += 1
        for r, node in enumerate(made):
            node.setXYpos(x, y + r * 90)
        if index % 8 == 0:
            bd = nuke.createNode("BackdropNode", inpanel=False)
            bd.setXYpos(x - 30, y - 60)
            bd["bdwidth"].setValue(200)
            bd["bdheight"].setValue(len(made) * 90 + 90)
            bd["label"].setValue("chain {}".format(index))
            bd["z_order"].setValue(-1)
            made.append(bd)
        made_count["top"] += len(made)
        index += 1
    for node in nuke.allNodes():
        node.setSelected(False)
    nuke.scriptSaveAs(OUT, overwrite=1)
    top = nuke.allNodes()
    every = nuke.allNodes(recurseGroups=True)
    classes = {}
    for n in every:
        classes[n.Class()] = classes.get(n.Class(), 0) + 1
    print("wrote {}: {} top-level nodes, {} including group innards".format(OUT, len(top), len(every)))
    print("classes: " + ", ".join("{} {}".format(c, k) for k, c in sorted(classes.items(), key=lambda kv: -kv[1])))


main()
