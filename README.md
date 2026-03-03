# Labelmaker

Labelmaker is a wholesale replacement for Foundry Nuke's autolabel system, showing you far more information at a glance in the node graph. No more opening the properties panel to see what a node is doing — Labelmaker shows you right in the DAG.

![Example of what Labelmaker does.](https://github.com/charlesangus/Labelmaker/blob/assets/example.png?raw=true)

# Installation

## Single User

Drop the **entire `Labelmaker` folder** (not just individual files) into your `~/.nuke/` directory, then add the following line to your `menu.py`:

```python
nuke.pluginAddPath('Labelmaker')
```

Note: this goes in `menu.py`, not `init.py`. Labelmaker only affects the UI and does not need to load in headless sessions.

## Facility

Put the `Labelmaker` folder somewhere on a shared location and add it to the Nuke plugin path the same way as above.

For cascading facility configurations, use the environment variables described in the [Configuration](#configuration) section below. You can layer a facility-wide config on top of the base config, and let artists override further with personal configs.

# Features

## Node Classes

Labelmaker ensures you can always tell what class a node is.

![Never wonder what class a node is.](https://github.com/charlesangus/Labelmaker/blob/assets/node_class.png?raw=true)

If a node has been renamed to something that no longer starts with the class name — for example `Transform1` renamed to `guy` — Labelmaker displays `Transform | guy` so you always know what you're looking at. Nodes that haven't been renamed display normally.

## Colour Swatches

See the colour of your grades right in the node graph.

![Colourized labels for Color knobs!](https://github.com/charlesangus/Labelmaker/blob/assets/grade.png?raw=true)

Color and AColor knobs (e.g. in a Grade node) automatically get colour swatches. Labelmaker uses an approximation of the AlexaToRec curve to tonemap colours so even quite bright values stay legible. Swatch text colour adapts for readability. This can be disabled in the preferences.

## Channels

Any node with a `channels` knob shows what channels it is operating on.

![Display channels.](https://github.com/charlesangus/Labelmaker/blob/assets/channels.png?raw=true)

## Masks and Unpremults

The channel readout updates to reflect masking and unpremult state.

![Clearly display masks and un/premultiplication.](https://github.com/charlesangus/Labelmaker/blob/assets/mask_unpremult.png?raw=true)

- `M` — masked by a channel
- `Minv` — masked by the inverted channel
- `/*` — unpremultiplied/premultiplied by a channel

For example, `(rgb M red) /* alpha` means the node processes `rgb` channels masked by `red` and (un)premultiplied by `alpha`.

## File Readout

Read and Write nodes show the basename of their file path directly on the node, so you can identify sources and outputs without opening the properties panel.

## Mix

When a node's `mix` knob is set to anything other than `1.0`, the current mix value is shown on the node.

## Custom TCL in Autolabels

![Execute arbitrary TCL code defined in your Labelmaker config.](https://github.com/charlesangus/Labelmaker/blob/assets/tcl.png?raw=true)

Config entries can include arbitrary TCL strings that are evaluated as if written in the node's label knob. For example, the base config for Shuffle uses `"in [value in]-->out [value out]"`, which displays `in rgba --> out rgba` on the node. This keeps TCL out of the label knob and lets you update the display of every node in every script by editing the config.

## Regular Old Labels

The label knob works exactly as before, including TCL expressions.

![Regular old labels work exactly as before.](https://github.com/charlesangus/Labelmaker/blob/assets/regular_label.png?raw=true)

## Auto De-overlap

When a node's label grows taller (because more knob values come into view), Labelmaker automatically pushes downstream nodes down to prevent overlap. The push is debounced with a 150ms delay and does not pollute the undo stack. Nodes are not retracted when a label shrinks. This feature can be toggled in preferences.

## De-overlap All Nodes

**Edit > Node Layout > De-overlap All Nodes** runs a one-shot spatial sweep across the entire script. Nodes are processed in top-to-bottom order; any node whose bounding box overlaps the node above it is pushed down to clear it. This operation is fully undoable.

# Configuration

Labelmaker reads one or more JSON config files. Each top-level key is a node class name; its value is a list of line definitions displayed in order.

```json
{
    "NameOfNodeClass": [
        {
            "name": "knob_name",
            "label": "optional display label",
            "default": "optional — omit line when value equals this (use a list for multi-value knobs, e.g. [0.0, 0.0])",
            "always_show": true
        },
        {
            "tcl_string": "[value in] --> [value out]"
        }
    ]
}
```

Keys for knob entries:

| Key | Required | Description |
|---|---|---|
| `name` | yes | Internal knob name |
| `label` | no | Display label shown in the DAG |
| `default` | no | Skip this line when the knob is at this value |
| `always_show` | no | Show this line even when the value matches `default` |
| `tcl_string` | — | Alternative to `name`; raw TCL evaluated in node context |

## Config Cascade

Configs are layered in this order (later entries override earlier ones):

1. Base config shipped with Labelmaker (`base_config.json`), if enabled
2. Facility configs, in order, from `LABELMAKER_CONFIGS_NAMES` / `LABELMAKER_CONFIGS_PATHS`
3. Personal config at `~/.nuke/labelmaker_config.json` (or the path set in preferences)

## Environment Variables

| Variable | Description |
|---|---|
| `LABELMAKER_DEFAULT_CONFIG_PATH` | Override the default personal config path |
| `LABELMAKER_DISABLE_BASE_CONFIG` | Set to `1` to skip the base config entirely |
| `LABELMAKER_CONFIGS_NAMES` | Semicolon-separated (Windows) or colon-separated (Mac/Linux) list of config names |
| `LABELMAKER_CONFIGS_PATHS` | Matching list of paths for the configs named above |

# Preferences

Open the preferences dialog via **Edit > Labelmaker Preferences...**

| Preference | Default | Description |
|---|---|---|
| Enable Labelmaker | on | Master on/off switch |
| Always Show All Labels | off | Show all config lines regardless of whether values are at their defaults |
| Disable Colorization | off | Turn off colour swatches |
| Use Base Config | on | Include the shipped `base_config.json` |
| Enable Auto De-overlap | on | Automatically push downstream nodes down when a label grows taller |
| Personal Config Path | `~/.nuke/labelmaker_config.json` | Location of your personal config overrides |

Preferences are saved to `~/.nuke/labelmaker_prefs.json`.

# Caveats

## Node Heights Change

By default, knob lines are hidden until the value differs from the default. This makes it easy to see at a glance which knobs have been adjusted, but nodes change height as you edit them.

If this bothers you, enable **Always Show All Labels** in the preferences. Nodes will be a consistent (taller) height at the cost of being more verbose and less scannable.

## Performance

Labelmaker has been used on production scripts of substantial size without issue. The autolabel routine runs as a low-priority idle process. If you do encounter performance problems, please open a GitHub issue and include the approximate node count and any node class that seems to be the culprit.

# Contributing

Pull requests welcome. Please rebase and squash your commits before submitting.
