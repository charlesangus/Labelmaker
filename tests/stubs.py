class StubKnob:
    def __init__(self, name, value=None, knob_class="Double_Knob"):
        self._name = name
        self._value = value
        self._class = knob_class

    def name(self):
        return self._name

    def value(self):
        return self._value

    def getValue(self):
        return self._value

    def Class(self):
        return self._class


class StubNode:
    def __init__(self, class_name, knobs=None, xpos=0, ypos=0, width=80, height=28):
        self._class = class_name
        self._knobs = knobs or {}
        self._xpos = xpos
        self._ypos = ypos
        self._width = width
        self._height = height

    def Class(self):
        return self._class

    def __getitem__(self, knob_name):
        return self._knobs[knob_name]

    def knobs(self):
        return self._knobs

    def xpos(self):
        return self._xpos

    def ypos(self):
        return self._ypos

    def screenWidth(self):
        return self._width

    def screenHeight(self):
        return self._height

    def minInputs(self):
        return 0

    def optionalInput(self):
        return 1

    def input(self, index):
        return None

    def name(self):
        name_knob = self._knobs.get("name")
        if name_knob is not None:
            return name_knob.value()
        return self._class

    def setYpos(self, value):
        self._ypos = value
