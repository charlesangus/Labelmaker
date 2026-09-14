# Labelmaker profiling harness (local only, git-ignored)

Profiles Labelmaker against a large script in a real — but headless — Nuke GUI
session (`xvfb-run` + Mesa llvmpipe GL). Nothing here ships; it pairs with the
TEMP INSTRUMENTATION counters currently in `labelmaker.py`/`menu.py`.

Every launch starts clean: `run*.sh` wipe and recreate the throwaway `HOME`
(`.profiling/home`, so `~/.nuke` is empty), `NUKE_TEMP_DIR` (`.profiling/tmp`)
and `/var/tmp/nuke-u$UID`, and open a private copy of the `.nk` from the temp
dir — Nuke writes `<script>.autosave` beside an open script, and a later
`scriptOpen` of that script blocks forever on the "recover autosave?" prompt.

## Use

Results: `RESULTS-2026-09-13.md` (summary), `results/*.txt` (raw census
output), `xvfb-draw-path-findings.md` (the runIn/single-shot issue, for
nuke-screenshotter).

```sh
# 1. build a synthetic comp (LM_NODES is the top-level node target)
LM_OUT=/tmp/lm_d3000.nk LM_NODES=3000 nuke -t .profiling/build_diverse_script.py   # mixed chains, Keylight OFX, Groups, expressions, clones
LM_OUT=/tmp/lm_800.nk LM_NODES=800 nuke -t .profiling/build_big_script.py          # older, uniform 9-node chains

# 2. draw-path census — Nuke's own DAG drives the labels (preferred)
.profiling/run_drawpath.sh /tmp/lm_800.nk both      # on | off | both
LM_PROFILE_ONLY="frame,viewer" .profiling/run_drawpath.sh /tmp/lm_800.nk on
LM_PROFILE_DEOVERLAP=1 ...                          # leave auto-deoverlap on

# 3. per-build cost via nuke.runIn (older harness; cProfile per phase)
LM_PROFILE_SCENARIOS=build,cache,repeat .profiling/run.sh /tmp/lm_800.nk
```

`run_drawpath.sh` opens the script, then fires ~50 UI events (zoom, pan, GL
mouse events, selection, knob edits, wiring, frame changes, viewer connects,
paste, undo …) and for each reports how many times Nuke asked for a label and
the time spent in the Python label function — Labelmaker's, or in `off` mode
Nuke's stock `plugins/autolabel.py` (also Python), so the two are comparable.
The script-open label pass is cProfiled to `.profiling/drawpath-on.prof`.

## Getting Nuke to draw labels under Xvfb

Nuke computes node labels from its exec() loop. Anything that stays inside one
Python callback and pumps `QApplication.processEvents()` — the old `diag`
scenario, `dag.repaint()`, screen grabs — never lets that run, so it sees zero
autolabel calls and concludes the DAG "never renders". It does: schedule each
step as its own `QTimer.singleShot` and return to the event loop between steps
(`drawpath/menu.py`), and every node is labelled via Nuke's real draw path.

`bootstrap/menu.py` (used by `run.sh`) predates that finding and calls the
autolabel by hand with `nuke.runIn`; it is still fine for per-build CPU cost
and phase breakdowns, but its README-era claim about the DAG was wrong.

## What the census established (Nuke 17.0v3)

Nuke caches each node's label and only re-requests it on invalidation:

| event | label requests |
|---|---|
| zoom / pan / hover / select / click / drag / move node | 0 |
| knob edit, rename, disable, label knob, create/paste node | 1 per affected node |
| delete node, any wiring change (setInput, mask input, drag-wire) | 0 |
| script open, first frame change, viewer connect | every node in the script |
| subsequent frame changes | frame-dependent nodes only (57 of 801 here) |

So the per-redraw "cache hit" path in Labelmaker never fires in normal use, and
serving a cached label for a repeat request inside the debounce window drops
the trailing edit (two knob edits 50 ms apart leave the first value on screen).

## Temporary instrumentation

`temp-instrumentation.patch` is the TEMP INSTRUMENTATION that lived in
`labelmaker.py`/`menu.py` during the first profiling pass (per-phase timers,
`dump_stats`, and an *Edit > Labelmaker Profile* menu). It applies to the
pre-cache code (`master` at 94e313b, `git apply .profiling/temp-instrumentation.patch`);
the label cache that replaced the debounce rewrote `create_autolabel`, so it
does not apply on top of `label-cache`. The draw-path harness no longer needs
it: `_timed_autolabel` in `drawpath/menu.py` times whichever label function
is live, and per-class/per-phase breakdowns come from cProfile.
