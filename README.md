# Labelmaker

Labelmaker is a wholesale replacement for Foundry Nuke's autolabel.py, offering much more information at a glance in the node graph. Try it - after using it for a while, you'll feel blind without it. No more opening the properties panel to see what a node is doing - Labelmaker shows you this info right in the DAG.

![Example of what Labelmaker does.](https://github.com/charlesangus/Labelmaker/blob/assets/example.png?raw=true)

# Features

Labelmaker ships with a base config which should be suitable for most people, and which covers the most-used nodes and knobs.

## Node Classes

Labelmaker ensures you can always tell what class a node is.

If the name of a node is changed to no longer start with the node class by some naughty comper, for example `Transform1` becomes `guy`, ruining your ability to tell at a glance whether it's a Transform, Reformat, Crop, etc., Labelmaker helpfully displays `Transform | guy` as the node name. For nodes which haven't been renamed, it will still just display `Transform1`.

## Colour Swatches

I'm pretty pleased with this one - see the colour of your grades *right in the node graph*.

![Colourized labels for Color knobs!](https://github.com/charlesangus/Labelmaker/blob/assets/grade.png?raw=true)

Color and AColor knobs (e.g. in a Grade node) will automatically get colour swatches to let you see at a glance what the node is doing. Labelmaker uses an approximation of the AlexaToRec curve to tonemap colours, so even quite bright values should be legible. This can be disabled in the preferences if you find it distracting.

## Channels

Any node with a "channels" selector will show you what channels are being operated on.

## Masks and Unpremults

Nodes which are using the NodeWrapper functionality to mask or unpremult by a channel will now display that fact in the channel readout using the indicators `M` (or `Minv`) for mask/inverted mask and `/*` for unpremult by (since this knob first divides by the matte and then multiplies by the matte, `/*` seemed an appropriate symbol).

For example, a Grade displaying `(rgb M red) /* alpha` would mean the node is processing the `rgb` channels masked by the `red` channel and (un)premultiplying by the `alpha` channel.

## Custom TCL In Autolabels

Perhaps the most powerful feature of Labelmaker is the use of arbitrary TCL code on nodes.

Some of us are used TCL in the labels of our nodes to display important information in the DAG. The downside of this is it clutters up the label knob and is easy to accidentally delete or mess up if you want to add another label.

Labelmaker supports using TCL in the autolabeling, and indeed the default config uses this functionality for a number of nodes. For instance, the base config for the Shuffle node is `"in [value in]-->out [value out]"`, which will print e.g. `in rgba --> out rgba`. Moving this code to the autolabel routine keeps it out of the label knob, and allows you to change the label of every node in every script you ever open by altering the Labelmaker config.

## Regular Old Labels

The regular old label knob works exactly as you'd expect, including the use of TCL code in the label knob.

## Config Files

Labelmaker is totally configurable using simple JSON files. At some point, I would like to make a simple GUI to manage the config files. Until then, it should be fairly straightforward to modify the JSON file by hand.

## Config Structure

Each object in the JSON file is a node class to label. The object's value is a list, the members of which are objects representing lines in the label. They will be displayed in the order they appear in the JSON list. Knob labels can either be "dumb", as represented by an object which must have a "name" key (and optionally "label", "default", and "always_show" keys), or "smart", as represented by an object with one key, "tcl_string", the value of which is text to run through the TCL parser as if it was written in the node's label knob.


A (non-working) example of a simple config file to make it more clear:

```
{
    "NameOfNodeClass": [
        {
            "name": "internal_name_of_knob",
            "label": "optional - label to displat in DAG",
            "default": "optional - don't display the value if it's at this value - should be a list for knobs with multiple values, like translate - e.g. [0.0, 0.0]",
            "always_show": "optional - 'true' if the label should always be shown no matter what"
        },
        {
            "tcl_string": "tcl code to execute and display, e.g. [value frame]"
        }
    ]
}
```

## Preferences

Labelmaker exposes a few preferences in the Nuke preferences node, including disabling colour swatches, always showing all label lines, and disabling the base config. You can also move your personal config from its default location.

## Caveats

### Node Heights Change

By default, most knob values are hidden from the nodes until they're changed. I like this, as it lets you more easily see at a glance which knobs have been adjusted. For example, it's immediately clear a Grade node has only had its `lift` knob adjusted, since it's the only knob readout being displayed.

This annoys some people who have tested this, however, as the nodes change height as you play with the knobs. For myself, it doesn't bother me, as I generally know in advance which knobs I will be using and place my nodes appropriately to allow the space required. If you give yourself a chance, I think you'll find you can adjust to this fairly easily.

If it really bothers you, though, there is a preference to always show all knobs, which will make the nodes always the same height (at the cost of being quite tall and not being able to see which knobs have been changed as easily).

Down the road, I would like to make an auto-de-overlapper to get the best of both worlds, but I haven't yet.

### Performance

I have used Labelmaker on production scripts of fairly large size without issue. Performance should be fine, as the autolabel routine runs as a low-priority idle process. However, if you encounter performance problems, please do let me know, and include the script (if possible) or at least the node count and if any particular node seemed especially problematic.

### Work in Progress

Labelmaker remains very much a work in progress. I've been using various iterations of it for a while now, and the time has come to send it out into the wild. I do hope you find it useful. Please use the Github Issue Tracker to report any bugs or issues you find, and please feel free to submit a pull request if you've done any useful work on it (see "Contributing" below).

# Installation

## Single User

Drop the Labelmaker folder in your `~/.nuke` folder, and add the following line to your menu.py:

`nuke.pluginAddPath('Labelmaker')`

(Yes, pluginAddPath is usually called from init.py, but since Labelmaker only affects the UI, it does not need to be loaded in a headless session, and it makes more sense to add it to the menu.py.)

## Facility

Put the Labelmaker folder somewhere sensible for your facility and add it to the Nuke plugin path as above.

For use in a facility, Labelmaker can be configured with cascading configurations by using environment variables. You can add configurations by adjusting the environment variables `LABELMAKER_CONFIGS_NAMES` and `LABELMAKER_CONFIGS_PATHS`. Labelmaker expects the same number of entries in both variables. Values are separated by ';' on Windows and ':' on Mac or Linux.

Labelmaker will load the base config which ships with Labelmaker (if enabled in preferences), and then override it successively by each config in the env vars above, and then finally override by the user config. Nodes which are not overridden are passed through from the higher-level configs unchanged.

If you have your own facility base config and would not like to use the Labelmaker base config, set the env var `LABELMAKER_DISABLE_BASE_CONFIG` to '1'.

# Contributing

Pull requests welcome. Ideally, rebase and squash your commits before merging back to master to keep things clean before submitting the pull request.
