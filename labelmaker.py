import contextlib
import os
import re
import time

import nuke

import labelmaker_config
import labelmaker_deoverlap
import labelmaker_prefs

# Nuke only asks for a node's label again when a real knob on it changes, or
# in a whole-script pass (after a viewer input change, or any knob change
# followed by a frame step) that requests every node in one burst. Handing
# Nuke a *changed* label string costs it a main-loop stall proportional to
# the script size (~70 ms at 3k nodes, ~200 ms at 10k), which is what makes
# slider drags and scrubbing sluggish. So: answer bursts from the cache, never
# return a changed string while the user is interacting, and once label
# traffic has gone quiet verify every label that was answered from the cache
# (rebuild it in the background, in slices) and release the ones that
# differ. Nothing is inferred from a burst's size or cause: a bulk edit by a
# tool and a whole-script pass are served the same way and both converge
# (measured with the harness on the profiling-harness branch).
LABEL_BURST_GAP_S = 0.005      # requests closer together than this are one pass
LABEL_BURST_MIN = 8            # requests before a pass counts as a burst
LABEL_REFRESH_MIN_S = 0.4      # quiet time before stale labels are released
LABEL_REFRESH_MAX_S = 1.5
LABEL_STALL_FACTOR = 5.0       # wait at least this many measured stalls
LABEL_VERIFY_SLICE_S = 0.015   # background verification runs in slices this long,
LABEL_VERIFY_GAP_MS = 0        # returning to the event loop between them

# nuke.runIn() evaluates one expression and returns nothing: the verified
# label comes back through this slot ([labeller] in, [labeller, text] out)
_verify_slot = []
_VERIFY_CODE = "__import__('labelmaker')._verify_run()"


def _verify_run():
    _verify_slot.append(_verify_slot[0]._compose_label())


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
    return mask_input is not None


class AutolabelReplacement(object):
    def __init__(self, config):
        super(AutolabelReplacement, self).__init__()
        self._config = config
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
        self._line_counts = {}       # {node_name: int} last known line count per node
        self._pending_deoverlap = set()  # node names whose height increased since last timer fire
        self._deoverlap_timer = None  # created lazily; PySide6 is not imported at module level
        self._content = {}    # {full_name: (frame or None, text)} from the last real build
        self._shown = {}      # {full_name: text} the string Nuke was last given
        self._forced = set()  # full names whose next request must build and show the result
        self._stale = set()   # full names shown with a string known to be out of date
        self._verify = set()  # full names answered from the cache, to be checked when idle
        self._verify_first = set()  # ... of which the frame-dependent ones, checked first
        self._frame_dep = set()  # full names whose last build read keys, expressions or [tcl]
        self._burst = {"t": 0.0, "n": 0, "names": []}
        self._verify_timer = None   # created lazily; PySide6 is not imported at module level
        self._stall_t = None      # when a changed string was last handed to Nuke
        self._stall_ema = 0.05    # running estimate of Nuke's stall after a change
        self._refresh_timer = None  # created lazily; PySide6 is not imported at module level

    @property
    def config(self):
        return self._config

    @config.setter
    def config(self, config):
        self._config = config
        self.invalidate_labels()

    def register_autolabel(self):
        nuke.addAutolabel(self.create_autolabel)
        # the cache is keyed by name, and a delete or a rename frees a name
        # for a new node; both callbacks fire once per node (knobChanged
        # would fire per pointer move while dragging a selection)
        nuke.addOnCreate(self._on_node_created)
        nuke.addOnDestroy(self._on_node_destroyed)

    def unregister_autolabel(self):
        nuke.removeAutolabel(self.create_autolabel)
        nuke.removeOnCreate(self._on_node_created)
        nuke.removeOnDestroy(self._on_node_destroyed)
        self.invalidate_labels()

    def set_enabled(self, enabled):
        if enabled:
            self.register_autolabel()
            self.refresh_all_labels()
        else:
            self.unregister_autolabel()
            # Nuke never re-requests a label on redraw, so without a poke
            # every node keeps showing Labelmaker's string
            self._poke_nodes(
                [node.fullName() for node in nuke.allNodes(recurseGroups=True)], force=False
            )

    def _get_deoverlap_timer(self):
        if self._deoverlap_timer is None:
            from PySide6 import QtCore
            self._deoverlap_timer = QtCore.QTimer()
            self._deoverlap_timer.setSingleShot(True)
            self._deoverlap_timer.setInterval(150)
            self._deoverlap_timer.timeout.connect(self._run_deoverlap)
        return self._deoverlap_timer

    def _run_deoverlap(self):
        pending = self._pending_deoverlap.copy()
        self._pending_deoverlap.clear()
        if pending:
            labelmaker_deoverlap.deoverlap_from_nodes(pending)

    def create_autolabel(self):
        now = time.perf_counter()
        self._note_stall(now)
        in_burst = self._track_burst(now)
        node = nuke.thisNode()
        full_name = node.fullName()
        cached = self._content.get(full_name) if self._pokeable(node) else None
        if in_burst and cached is not None and full_name not in self._forced:
            # answered from the cache whatever the burst is (a whole-script
            # pass or a bulk edit): the idle verification finds out whether
            # the string is still right
            self._burst["names"].append(full_name)
            return self._shown.get(full_name, cached)
        was_forced = full_name in self._forced
        self._forced.discard(full_name)
        self._verify.discard(full_name)
        text = self._build_label()
        self._content[full_name] = text
        self._note_frame_dependence(full_name)
        previous = self._shown.get(full_name)
        if previous is not None and text != previous and not was_forced and self._pokeable(node):
            # keep showing the old string; the idle refresh releases the new one
            self._mark_stale(full_name)
            return previous
        if text != previous:
            self._stall_t = now
        self._shown[full_name] = text
        return text

    def _build_label(self):
        autolabel = self._compose_label(write_indicators=True)
        new_line_count = autolabel.count('\n') + 1
        old_line_count = self._line_counts.get(self.node_name)
        self._line_counts[self.node_name] = new_line_count
        if (
            old_line_count is not None
            and new_line_count > old_line_count
            and labelmaker_prefs.prefs_singleton.get("deoverlap_enabled")
        ):
            self._pending_deoverlap.add(self.node_name)
            self._get_deoverlap_timer().start()  # restarts timer if already running
        return autolabel

    def _compose_label(self, write_indicators=False):
        """The label text for nuke.thisNode(); read-only unless asked to
        update the indicators knob as Nuke's own autolabel does."""
        self.update()
        if write_indicators:
            self.set_indicators()
        else:
            self.compute_indicators()
        self.name_line_creator()
        self.file_line_creator()
        self.channels_line_creator()
        self.knob_readout_creator()
        self.mix_line_creator()
        self.label_readout_creator()
        return "\n".join(self.lines)

    def _note_frame_dependence(self, full_name):
        # keys or an expression (indicator bits 1 and 2), or TCL in the label
        # knob: these are the labels a frame change alters, so they are
        # verified first after a pass (an ordering hint, not a gate)
        indicators = getattr(self, "indicators", 0)
        if bool(indicators & 3) or "[" in getattr(self, "node_label_raw", ""):
            self._frame_dep.add(full_name)
        else:
            self._frame_dep.discard(full_name)

    def _pokeable(self, node):
        # a held-back or cached string is only ever refreshed by a poke, so a
        # node that cannot be poked must be rebuilt and shown on every request
        return node.knob("dope_sheet") is not None

    def _note_stall(self, now):
        # the gap from handing Nuke a changed string to its next request is,
        # during a drag, one pointer interval plus Nuke's stall
        if self._stall_t is None:
            return
        gap = now - self._stall_t
        self._stall_t = None
        if gap < 1.0:
            self._stall_ema = 0.7 * self._stall_ema + 0.3 * gap

    def _refresh_window(self):
        window = LABEL_STALL_FACTOR * self._stall_ema
        return min(LABEL_REFRESH_MAX_S, max(LABEL_REFRESH_MIN_S, window))

    def _track_burst(self, now):
        burst = self._burst
        if now - burst["t"] > LABEL_BURST_GAP_S:
            self._close_burst()
        burst["t"] = now
        burst["n"] += 1
        if burst["n"] == LABEL_BURST_MIN + 1:
            self._arm_refresh()
        return burst["n"] > LABEL_BURST_MIN

    def _close_burst(self):
        burst = self._burst
        if burst["names"]:
            self._verify.update(burst["names"])
            self._verify_first.update(self._frame_dep.intersection(burst["names"]))
            self._arm_refresh()
        burst["n"] = 0
        burst["names"] = []

    def _mark_stale(self, full_name):
        self._stale.add(full_name)
        self._arm_refresh()

    def _get_refresh_timer(self):
        if self._refresh_timer is None:
            from PySide6 import QtCore
            self._refresh_timer = QtCore.QTimer()
            self._refresh_timer.setSingleShot(True)
            self._refresh_timer.timeout.connect(self._refresh_stale_labels)
        return self._refresh_timer

    def _arm_refresh(self):
        self._get_refresh_timer().start(int(self._refresh_window() * 1000))

    def _refresh_stale_labels(self):
        if self._busy():
            self._arm_refresh()  # still busy: wait for the traffic to end
            return
        self._close_burst()
        if self._verify:
            self._verify_slice()
            return
        self._release_stale()

    def _busy(self):
        return time.perf_counter() - self._burst["t"] < self._refresh_window() * 0.9

    def _release_stale(self):
        names = list(self._stale)
        self._stale.clear()
        self._poke_nodes(names)

    def _get_verify_timer(self):
        if self._verify_timer is None:
            from PySide6 import QtCore
            self._verify_timer = QtCore.QTimer()
            self._verify_timer.setSingleShot(True)
            self._verify_timer.timeout.connect(self._verify_slice)
        return self._verify_timer

    def _verify_slice(self):
        """Rebuild a slice of the cache-answered labels; queue the ones that
        differ from what Nuke is showing. Yields to the event loop between
        slices and backs off while label traffic resumes."""
        if self._busy():
            self._arm_refresh()
            return
        deadline = time.perf_counter() + LABEL_VERIFY_SLICE_S
        had_first = bool(self._verify_first)
        while self._verify and time.perf_counter() < deadline:
            full_name = self._pop_verify()
            text = self._compose_in_context(full_name)
            if text is None:
                continue
            self._content[full_name] = text
            self._note_frame_dependence(full_name)
            if text != self._shown.get(full_name):
                self._stale.add(full_name)
        if self._verify:
            if had_first and not self._verify_first:
                # the likely-changed labels are done: release them now rather
                # than after the whole script has been checked
                self._release_stale()
            self._get_verify_timer().start(LABEL_VERIFY_GAP_MS)
        else:
            self._release_stale()

    def _pop_verify(self):
        if self._verify_first:
            full_name = self._verify_first.pop()
            self._verify.discard(full_name)
        else:
            full_name = self._verify.pop()
            self._verify_first.discard(full_name)
        return full_name

    def _compose_in_context(self, full_name):
        """The label the build would produce for `full_name` right now, or
        None if the node is gone. The label code reads nuke.thisNode() and
        'this.*' paths, so it has to run with the node as Nuke's context."""
        if nuke.toNode(full_name) is None:
            return None
        _verify_slot[:] = [self]
        nuke.runIn(full_name, _VERIFY_CODE)
        return _verify_slot[1] if len(_verify_slot) > 1 else None

    def _poke_nodes(self, full_names, force=True):
        # Nothing in the API re-requests one node's label; a real knob change
        # does. Flipping dope_sheet and flipping it back in the same callback
        # yields exactly one relabel, no undo entry and no visible change.
        nuke.Undo.disable()
        try:
            for full_name in full_names:
                node = nuke.toNode(full_name)
                if node is None:
                    continue
                knob = node.knob("dope_sheet")
                if knob is None:
                    continue
                if force:
                    self._forced.add(full_name)
                value = knob.value()
                knob.setValue(not value)
                knob.setValue(value)
        finally:
            nuke.Undo.enable()

    def _on_node_created(self):
        self._forget(nuke.thisNode().fullName())

    def _on_node_destroyed(self):
        self._forget(nuke.thisNode().fullName())

    def _forget(self, full_name):
        self._content.pop(full_name, None)
        self._shown.pop(full_name, None)
        self._stale.discard(full_name)
        self._forced.discard(full_name)
        self._verify.discard(full_name)
        self._verify_first.discard(full_name)
        self._frame_dep.discard(full_name)

    def invalidate_labels(self):
        """Forget every cached label; nodes rebuild when Nuke next asks."""
        self._content.clear()
        self._shown.clear()
        self._stale.clear()
        self._forced.clear()
        self._verify.clear()
        self._verify_first.clear()
        self._frame_dep.clear()
        self._burst = {"t": 0.0, "n": 0, "names": []}

    def refresh_all_labels(self):
        """Rebuild and redraw every label now (after a config change)."""
        self.invalidate_labels()
        self._poke_nodes([node.fullName() for node in nuke.allNodes(recurseGroups=True)])

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
        self.compute_indicators()
        # a knob write: it dirties the node, so only a real label request
        # does it (background verification must not touch the DAG)
        nuke.knob("this.indicators", str(self.indicators))

    def compute_indicators(self):
        # this function is copied from Foundry's autolabel.py and
        # is copyright Foundry, all rights reserved
        # seemingly more or less need to use this TCL code, as there doesn't
        # seem to be python equivalents for these functions
        # nuke.expression returns a float
        ind = int(nuke.expression(
            "(keys?1:0)+(has_expression?2:0)+(clones?8:0)+(viewsplit?32:0)"
        ))
        if int(nuke.numvalue("maskChannelInput", 0)):
            ind += 4
        if int(nuke.numvalue("this.mix", 1)) < 1:
            ind += 16
        self.indicators = ind

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
        mask_string = "Minv" if mask_inverted == "true" else "M"
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

            if "tcl_string" in item:
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
                always_show = labelmaker_prefs.prefs_singleton.get(
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
                ) and not labelmaker_prefs.prefs_singleton.get("colorize_disable"):
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
                        # jam wrapper onto the first item to avoid an extra line
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
        # the raw knob is what tells TCL apart; once substituted, "[frame]"
        # is just a number
        self.node_label_raw = nuke.value("this.label", "") or ""
        node_label_value = self.node_label_raw
        with contextlib.suppress(RuntimeError):
            node_label_value = nuke.tcl("subst", node_label_value)
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
        text_color = "black" if background_luminance > 0.22 else "white"
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
if labelmaker_prefs.prefs_singleton.get("labelmaker_enabled"):
    autolabeller_singleton.register_autolabel()
