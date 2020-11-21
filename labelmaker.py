import nuke
import os
import re
import json
import labelmaker_config
import labelmaker_prefs


# from https://gist.github.com/anonymous/a802f51391163a2bf0e3
def node_has_mask(node):
    """
    Does node have a mask input (on its right side)?
    @param node: the node object
    @return: True if node has a mask input, False if not
    """
    return "maskChannelMask" in node.knobs()


# from https://gist.github.com/anonymous/a802f51391163a2bf0e3
def get_actual_min_inputs(node):
    """
    Get actual minimum number of node inputs this node needs to do its job.
    Problem is, nodes with a maskChannelMask input falsely report number of
    inputs as one greater than actual minimum.
    IMO, we shouldn't regard a mask input as "required" because... it isn't.
    @param node: the node object
    @return: an integer representing the minimum total number of inputs,
             not including mask.
    """
    min_inputs = node.minInputs()
    return (min_inputs - 1) if (node_has_mask(node) and min_inputs > 0) else min_inputs


# from https://gist.github.com/anonymous/a802f51391163a2bf0e3
def get_actual_max_inputs(node):
    """
    Get actual maximum number of node inputs this node will accept,
    not including mask (if any) or optional inputs.
    @param node: the node object
    @return: an integer representing the maximum total number of inputs,
             not including mask.
    """
    return node.optionalInput()


# from https://gist.github.com/anonymous/a802f51391163a2bf0e3
def get_mask_input_index(node):
    """
    Get the index of node's mask input.
    For (all?) other nodes, it's minimumInputs minus 1
    if the node has a 'maskChannelMask' knob.
    """
    if node.Class() == "Merge2":
        # Merge2 is a special case. Mask input is always index 2.
        return 2
    elif node_has_mask(node):
        return node.minInputs() - 1
    else:
        # This node doesn't have a mask input.
        return None


def node_mask_input_plugged(n):
    if not node_has_mask(n):
        return False
    mask_index = get_mask_input_index(n)
    mask_input = n.input(mask_index)
    if mask_input is None:
        return False
    else:
        return True


class AutolabelReplacement(object):
    def __init__(self, config):
        super(AutolabelReplacement, self).__init__()
        self.config = config
        self.class_mappings = {
            "Merge2": "Merge",
            "Camera2": "Camera",
            "ReadGeo2": "ReadGeo",
            "Card2": "Card",
            "DeepColorCorrect2": "DeepColorCorrect",
            "CheckerBoard2": "CheckerBoard",
        }
        self.update_class_mappings_with_ofx_nodes()
        self.NAMELESS_NODES = ("Dot", "BackdropNode", "PostageStamp", "StickyNote")

    def register_autolabel(self):
        nuke.addAutolabel(self.create_autolabel)

    def unregister_autolabel(self):
        nuke.removeAutolabel(self.create_autolabel)

    def create_autolabel(self):
        self.update()
        self.set_indicators()
        self.name_line_creator()
        self.file_line_creator()
        self.channels_line_creator()
        self.knob_readout_creator()
        self.mix_line_creator()
        self.label_readout_creator()
        autolabel = "\n".join(self.lines)
        return autolabel

    def update(self):
        self.lines = []
        self.n = nuke.thisNode()
        self.node_name = self.n["name"].getValue()
        # sometimes we want the "true" class, i.e. Merge2, not Merge
        self.node_true_class = self.n.Class()
        # and sometimes we want to fudge the class a little
        # (so Merge2 becomes Merge), based on our class mapping
        # TODO: decide if we want to simply strip all trailing numbers...
        self.node_class = self.class_mappings.get(self.n.Class()) or self.n.Class()

    def set_indicators(self):
        # this function is copied from Foundry's autolabel.py and
        # is copyright Foundry, all rights reserved
        # seemingly more or less need to use this TCL code, as there doesn't
        # seem to be python equivalents for these functions
        ind = nuke.expression(
            "(keys?1:0)+(has_expression?2:0)+(clones?8:0)+(viewsplit?32:0)"
        )
        if int(nuke.numvalue("maskChannelInput", 0)):
            ind += 4
        if int(nuke.numvalue("this.mix", 1)) < 1:
            ind += 16
        nuke.knob("this.indicators", str(ind))

    def name_line_creator(self):
        # specialcase a few nodes which should not have names
        if self.node_true_class in self.NAMELESS_NODES:
            return None

        operation = nuke.value("this.operation", "none")

        # We should always know what class a node is.
        # If someone changes its name, display CLASS | NAME
        # instead of just NAME
        # Group nodes don't need their class id'ed,
        # their shape is unique and it makes Grizmos ugly
        if self.node_name.startswith(self.node_class) or self.node_class == "Group":
            name_line = self.node_name
        else:
            name_line = "{} | {}".format(self.node_class, self.node_name)

        if operation != "none" and operation:
            name_line = "{} ({})".format(name_line, operation)

        self.lines.append(name_line)

    def file_line_creator(self):
        file_path = nuke.value("this.file", "-")
        if file_path != "" and file_path != "-":
            file_name = os.path.basename(file_path)
            self.lines.append(file_name)

    def channels_line_creator(self):
        channels = nuke.value("this.channels", "-")
        mask_input_b_stream = nuke.value("this.maskChannelInput", "none")
        mask_input_side = nuke.value("this.maskChannelMask", "none")
        mask_connected = node_mask_input_plugged(self.n)
        mask_inverted = nuke.value("this.invert_mask", "false")
        if mask_inverted == "true":
            mask_string = "Minv"
        else:
            mask_string = "M"
        unpremult_and_premult = nuke.value("this.unpremult", "none")
        unpremult = "none"
        premult = "none"

        # special cases
        if self.node_class in ("Premult", "Unpremult"):
            # we want these to be consistent with the normal
            # display other nodes use
            channels = nuke.value("this.channels", "-")
            if self.node_class == "Unpremult":
                unpremult = nuke.value("this.alpha", "none")
            elif self.node_class == "Premult":
                premult = nuke.value("this.alpha", "none")

        elif self.node_class in ("Copy"):
            # Copy uses "channels" knob to do a layer copy from A to B
            # We'll handle with a tcl thingy in the normal routine
            channels = "-"

        elif self.node_class in ("Roto", "RotoPaint"):
            # roto uses "channels" knob to hold what channels to track
            # and uses "output" knob to hold the actual output
            channels = nuke.value("this.output", "-")

        if channels != "-":
            if mask_input_b_stream == "none" and not mask_connected:
                # no masking
                channel_line = "({})".format(channels)
            elif mask_input_b_stream != "none":
                # masking coming from B stream
                channel_line = "({} {} {})".format(
                    channels, mask_string, mask_input_b_stream,
                )
            elif mask_connected:
                # masking coming from side
                channel_line = "({} {} {})".format(
                    channels, mask_string, mask_input_side,
                )

            # values being (un)premulted or premulted etc
            if unpremult_and_premult != "none":
                channel_line = "{} /* {}".format(channel_line, unpremult_and_premult)
            elif unpremult != "none":
                channel_line = "{} / {}".format(channel_line, unpremult)
            elif premult != "none":
                channel_line = "{} * {}".format(channel_line, premult)
            self.lines.append(channel_line)

    def knob_readout_creator(self):
        # TODO: factor this monster class out
        knob_dict_list = self.config.get(self.node_true_class)

        if knob_dict_list is None:
            return

        knob_readouts = []
        for item in knob_dict_list:

            if "tcl_string" in item.keys():
                tcl_string = str(item["tcl_string"])
                try:
                    label_string = nuke.tcl("subst", tcl_string)
                except RuntimeError:
                    label_string = tcl_string
                if label_string is not None and label_string != "":
                    knob_readouts.append(label_string)
            else:
                knob_label = item.get("label", item["name"])
                # always show if user has selected to always show, otherwise fall back
                # to the node's setting from the config
                always_show = labelmaker_prefs.prefs_singleton.get_pref(
                    "always_show_all"
                ) or item.get("always_show", False)
                default = item.get("default", False)
                try:
                    knob_value = self.n[item["name"]].value()
                    knob_class = self.n[item["name"]].Class()
                except NameError:
                    # the knob does not exist on the node, just continue
                    continue
                show = False
                # handle knobs which should be colorized, if colorization isn't disabled
                colorize = False
                if knob_class in (
                    "Color_Knob",
                    "AColor_Knob",
                ) and not labelmaker_prefs.prefs_singleton.get_pref("colorize_disable"):
                    colorize = item.get("colorize", True)
                # this handles knobs like translate that return a list
                # and formats them nicely
                if isinstance(knob_value, (list, tuple)):
                    knob_value_formatted = self.format_knob_values(knob_value)
                    try:
                        formatted_default = self.format_knob_values(default)
                    except TypeError:
                        # default isn't iterable
                        formatted_default = self.format_knob_value(default)
                    if (
                        default is False
                        or knob_value_formatted != formatted_default
                        or always_show
                    ):
                        show = True
                else:
                    knob_value_formatted = self.format_knob_value(knob_value)

                    if default is False or knob_value != item["default"] or always_show:
                        show = True
                if show:
                    # if we want to colorize the knob readout, we need to manually
                    # centre the whole autolabel with a <div>, to work around a nuke
                    # bug where adding HTML to a node left-justifies everything
                    if not colorize:
                        label_string = "{} {}".format(knob_label, knob_value_formatted)
                    else:
                        label_string = self.colorize_knob_readout(
                            knob_value, knob_label, knob_value_formatted
                        )
                        # to avoid adding an extra line, we need to jam our wrapper onto the front of the first item
                        if len(self.lines) > 0:
                            self.lines[0] = "{}{}".format(
                                self.centre_wrapper(), self.lines[0]
                            )
                        elif len(knob_readouts) > 0:
                            # there is already a readout which will be the first thing in the node
                            knob_readouts[0] = "{}{}".format(
                                self.centre_wrapper(), knob_readouts[0]
                            )
                        else:
                            # this will be the first thin
                            label_string = "{}{}".format(
                                self.centre_wrapper(), label_string
                            )

                    knob_readouts.append(label_string)
        knob_readout = "\n".join(knob_readouts)

        if knob_readout != "":
            self.lines.append(knob_readout)

    def mix_line_creator(self):
        mix = nuke.value("this.mix", "none")
        if mix != "none" and float(mix) != 1.0:
            mix_line = "mix {:.3f}".format(float(mix))
            self.lines.append(mix_line)

    def label_readout_creator(self):
        node_label_value = nuke.value("this.label", "")
        try:
            node_label_value = nuke.tcl("subst", node_label_value)
        except RuntimeError:
            # TCL execution failed, so just use the label as-is
            pass
        if node_label_value != "" and node_label_value is not None:
            self.lines.append(node_label_value)

    def format_knob_values(self, values):
        values_string_list = [self.format_knob_value(value) for value in values]
        values_formatted = ", ".join(values_string_list)
        return values_formatted

    def format_knob_value(self, value):
        if isinstance(value, (float)):
            knob_value_formatted = "{:.3f}".format(value)
        else:
            knob_value_formatted = "{}".format(value)
        return knob_value_formatted

    def recurse_into_menu(self, m):
        menu_item_leaves = []
        for item in m.items():
            if isinstance(item, nuke.Menu):
                menu_item_leaves = menu_item_leaves + self.recurse_into_menu(item)
            elif isinstance(item, nuke.MenuItem):
                try:
                    script = item.script()
                    name = item.name()
                    leaf = (name, script)
                    menu_item_leaves.append(leaf)
                except Exception:
                    # TODO: don't except everything?
                    pass
        return menu_item_leaves

    def find_ofx_class(self, menu_script):
        regex = re.search(r"OFX.*(?=[\'\"])", menu_script)
        if regex:
            return regex.group(0)
        else:
            return None

    def update_class_mappings_with_ofx_nodes(self):
        m = nuke.toolbar("Nodes")
        all_toolbar_items = self.recurse_into_menu(m)
        ofx_items = [item for item in all_toolbar_items if "OFX" in item[1]]
        ofx_mappings = {self.find_ofx_class(item[1]): item[0] for item in ofx_items}
        self.class_mappings.update(ofx_mappings)

    def centre_wrapper(self):
        # work around nuke HTML wonkiness - need to explicitly set the alignment and font
        # this is still a little wonky; changing the font won't actually update colourized
        # nodes
        centre_style_div = '<div style="text-align: center"><font face="{note_font}">'.format(
            note_font=self.n["note_font"].value()
        )
        return centre_style_div

    def clamp(self, value, low=0.0, high=1.0):
        return min(max(value, low), high)

    def sRGBish(self, value):
        return self.clamp(value ** 0.454, 0, 1) * 255

    def alexToRecish(self, value):
        a = 0.023
        b = 0.888
        c = 0.293
        d = 1.02
        e = 0.023
        # very rough curve approximating alexa to rec709 function
        # clamp to 0 to 1 and mult by 255 for hexification
        value = self.clamp(value, 0.0, 12.0)
        luted_value = int(
            self.clamp((d + ((a - d) / (1 + pow(value / c, b)))) - e) * 255
        )
        return luted_value

    def colorize_knob_readout(self, knob_value, knob_label, knob_value_formatted):
        # TODO: should only colorize some part of this, to avoid covering
        #       the side inputs if the line is long
        #       or could maybe put a "color chip" made up of underscores
        #       or something above a colorized line... i kind of like that,
        #       although it will make the nodes even taller...
        #       maybe a color chip on the left side... nodes with left inputs
        #       rarely have color knobs
        basic_colorize_span = (
            '<span style="background-color: '
            "#{r:02X}{g:02X}{b:02X}; "
            'color: {text_color};">'
            "{knob_label}: </span> {knob_value_formatted}"
            "</span>"
        )

        if isinstance(knob_value, (list, tuple)) and len(knob_value) >= 3:
            color_tuple = (
                self.alexToRecish(knob_value[0]),
                self.alexToRecish(knob_value[1]),
                self.alexToRecish(knob_value[2]),
            )
        elif isinstance(knob_value, (int, float)):
            color_tuple = (
                self.alexToRecish(knob_value),
                self.alexToRecish(knob_value),
                self.alexToRecish(knob_value),
            )
        else:
            # not a number or list, can't be colorized
            return knob_value_formatted

        # do a rough approx of the luminance of our background color
        background_luminance = (
            color_tuple[0] * 0.34 + color_tuple[1] * 0.5 + color_tuple[2] * 0.16
        ) / 255.0
        # since we've sort of
        if background_luminance > 0.22:
            text_color = "black"
        else:
            text_color = "white"
        colorized_readout = basic_colorize_span.format(
            r=color_tuple[0],
            g=color_tuple[1],
            b=color_tuple[2],
            text_color=text_color,
            knob_label=knob_label,
            knob_value_formatted=knob_value_formatted,
        )
        return colorized_readout


config_object = labelmaker_config.composed_config_singleton
autolabeller_singleton = AutolabelReplacement(config_object)
autolabeller_singleton.register_autolabel()
