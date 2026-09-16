"""Side-effect counter for the label-knob Tcl cases in drawpath/menu.py.

A label knob set to "[python {__import__('lm_probe').tick()}]" bumps `count`
every time an autolabel implementation substitutes the knob's Tcl.
"""
count = 0


def tick():
    global count
    count += 1
    return "probe"
