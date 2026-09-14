# Nuke under Xvfb: when the DAG label pass actually runs

Findings from profiling Labelmaker (Nuke 17.0v3, Debian 12, `xvfb-run` +
Mesa 22.3 llvmpipe, `LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe`,
`-screen 0 1920x1080x24 +extension GLX +render`), 2026-09-13. Written up
separately because it bears directly on nuke-screenshotter's warm-up logic
(issue #6) and its `_pump_events` settle mechanism.

## TL;DR

1. The DAG **does** render under Xvfb with software GL — node tiles, arrows,
   the minimap, all of it. Root-window grabs (`screen.grabWindow(0, …)`) read
   it back fine; `widget.grab()`/`render()` on the DAG widget return a flat
   fill, as the screenshotter already notes.
2. Nuke computes node labels (name + label knob + autolabel) **only from its
   own `exec()` loop**. Anything that stays inside one Python callback and
   pumps `QApplication.processEvents()` — no matter how long, with or without
   `dag.update()`/`dag.repaint()`, screen grabs, synthetic wheel/drag events —
   never lets that pass run. The tiles then draw the **class name only**
   (`Grade`, not `Grade12`), and every registered autolabel sees zero calls.
3. Once control returns to the event loop (schedule the next step with
   `QTimer.singleShot`, return from the callback), Nuke labels **every
   top-level node in the script within ~100 ms**, regardless of zoom or
   visibility, and then caches the result. The "centre each node at ≥2.0
   zoom and settle 0.6 s" warm-up is working around the symptom, not the
   cause.
4. `nuke.zoom(z, (x, y))` has one side effect that explains why the warm-up
   works at all: it labels the single node under the new view centre
   synchronously (exactly 1 autolabel call), even inside a callback. That is
   why centring each node one at a time produces labelled tiles.

## Evidence

Probe: register a counting autolabel (`nuke.addAutolabel(f)` where `f`
increments a counter and returns `None`), create 40 Grade nodes, then:

| what the probe did (inside ONE `QTimer.singleShot` callback) | autolabel calls |
|---|---|
| 1 s of `processEvents()` + `dag.update()` + `dag.repaint()` loops | 0 |
| `nuke.zoom(1.0 … 3.0)` at arbitrary points, each followed by 0.8 s pump | 0 |
| 5 synthetic `QWheelEvent`s sent to the `Foundry::UI::GLWindow` | 0 |
| synthetic middle-drag pan / hover sweep / left click via `QtTest` | 0 |
| `node.setSelected(True)`, `knob.setValue(...)`, `label` knob change | 0 |
| `nuke.zoom(2.5, centre_of_G10)` | **1** (G10 only; grab shows `G10`, everything else `Grade`) |

Same 40 nodes, steps chained as **separate** `QTimer.singleShot` callbacks:

| step (control returned to `exec()` between rows) | cumulative calls |
|---|---|
| create 40 Grades, return | 1 |
| 1.5 s later | **41** (40 Grades + Viewer; grab shows `G19 … G31`) |
| `nuke.zoom(2.5, …)`, 1.5 s later | 41 |
| one knob edit, 1 s later | 42 |
| zoom back to 1.0, elsewhere at 2.0 | 42 |

So zoom/pan never re-request labels; they are computed once per node and
invalidated by node changes. (Full event census on an 801-node script, with
the same pattern for the stock `plugins/autolabel.py` and for Labelmaker:
zoom/pan/hover/select/drag/move/wiring → 0; knob edit → 1; script open,
first frame change, viewer connect → every top-level node; later frame
changes → frame-dependent nodes only; opening a Group → its innards.)

## Why the screenshotter's warm-up still "works"

`_pump_events` runs `dag.update(); dag.repaint(); QApplication.processEvents()`
in a loop **inside** the capture callback, so the global label pass never
runs. Each `nuke.zoom(render_zoom, node_centre)` in the warm-up loop labels
the one node at the centre (finding 4), so after visiting every node in the
backdrop, every node has a cached label and the final grab shows them. That
is O(nodes × WARMUP_SETTLE_SECONDS) — 0.6 s per node — when a single return
to the event loop would label the whole script in one go.

## Recommendation for nuke-screenshotter

Restructure the in-Nuke runner so the capture is a chain of event-loop
steps rather than one blocking callback:

```python
def step_open():
    nuke.scriptOpen(path)
    QtCore.QTimer.singleShot(0, step_wait_labels)    # return to exec()

def step_wait_labels():
    # Nuke labels every top-level node from its exec() loop; poll a counting
    # autolabel until the count stops changing (≈100–500 ms for 800 nodes),
    # then proceed. Or simply singleShot(500, step_capture).
    ...

def step_capture():
    nuke.zoom(render_zoom, centre)      # no per-node warm-up needed
    QtCore.QTimer.singleShot(200, step_grab)  # let the async GL repaint land

def step_grab():
    pixmap = screen.grabWindow(0, x, y, w, h)
    ...
```

The existing freshness check (baseline-then-changed-and-stable) is still
worth keeping for the GL repaint lag; it is only the label warm-up that can
go. Group innards are labelled when the group is shown (`nuke.showDag`), so
captures inside groups need one extra event-loop return after `showDag`.

Two practical traps hit while doing this:

- **Autosave prompt hang.** Nuke writes `<script>.nk.autosave` beside any
  script it has open. A later `nuke.scriptOpen` of that script blocks
  forever, in C++, with no Qt timers firing, on the recover-autosave prompt.
  Killed/timed-out capture runs leave these behind. Copy the script into a
  fresh temp dir per run (and wipe `HOME/.nuke`, `NUKE_TEMP_DIR`,
  `/var/tmp/nuke-u$UID`).
- **`processEvents()` inside a modal is not the same as the exec loop.** Any
  poll/wait implemented as an in-callback pump inherits the problem above;
  waits must be timer callbacks.

## Repro scripts

- `.profiling/drawpath/menu.py` — the event-loop-driven harness (steps as
  `QTimer.singleShot` callbacks, counting autolabel, settle-by-count).
- `/tmp/lmprobe/boot/menu.py` (throwaway) — the two probes above; the
  in-callback variant and the chained-timer variant differ only in whether
  each step returns to the event loop.
