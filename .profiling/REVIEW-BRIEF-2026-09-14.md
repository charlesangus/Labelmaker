# Labelmaker autolabel performance — brief for an outside review

Written 2026-09-14 for another agent to analyse, challenge and reproduce.
Everything referenced is either in git or in this directory.

## 0. The question

Labelmaker replaces Nuke's autolabel (the multi-line text on every node in
the Node Graph) with richer labels. On large scripts (3k–10k nodes) the
original implementation made slider drags, timeline scrubs and frame
steps sluggish. Four implementations have now been measured against the
same real-Nuke harness:

| tag | what | where |
|---|---|---|
| **stock** | Nuke's own `plugins/autolabel.py` (also Python) | Nuke 17.0v3 install; harness mode `off` / `stock` |
| **debounce** | per-node cache + 100 ms debounce timer | commit `94e313b` (`labelmaker.py`, `AUTOLABEL_DEBOUNCE_MS`) |
| **burst** | burst-served cache, size-classified bursts, idle poke refresh | commits `6d76f27`…`1527d28` = branch `label-cache` HEAD; §10 of RESULTS adds a frame/viewer-change heuristic (superseded, not committed) |
| **verify-at-idle** | burst-served cache, **no classification**, background re-verification, poke only on difference | uncommitted working tree on `label-cache`; `verify-at-idle.patch` (apply on `1527d28`) |

We want an independent view on: (a) whether the measurements support the
conclusions, (b) whether verify-at-idle is the right shape or has a hole,
(c) what else should be tested. Section 8 lists the specific doubts.

## 1. Ground truth about Nuke (measured, Nuke 17.0v3)

All four designs stand on these facts (RESULTS §1, §6). If any is wrong the
designs are wrong.

1. Nuke caches each node's label and re-requests it only on invalidation:
   zoom / pan / hover / select / click / drag / move → **0** requests.
   A real knob change on a node → **1** request for that node (deferred to
   the next draw, not synchronous inside `setValue`). Delete and any wiring
   change → 0.
2. A **whole-script pass** (every top-level node in one synchronous burst,
   back to back) is *scheduled* by any real knob change, a viewer input
   change, registering a Nuke callback, or a poke, and *runs* at the next
   frame change; a viewer input change runs one immediately. Script open
   and first display of a Group also request every (shown) node. A plain
   frame change with nothing scheduled relabels only frame-dependent nodes
   (~10 %: keys, expressions, `[tcl]` in the label knob).
3. Handing Nuke a **changed** label string costs it a main-loop stall
   proportional to the script (~70 ms at 3k, ~200 ms at 10k), whatever
   produced the string; an unchanged string costs ~3 ms. This, not label
   build time (0.14–0.43 ms per label), is what makes drags sluggish.
4. Nothing in the API re-requests one node's label. Flipping `dope_sheet`
   (or `bookmark`) and flipping it back in the same callback yields exactly
   one relabel, no undo entry (with `nuke.Undo.disable()`), no visible
   change — the "poke". `tile_color`, `note_font*`, `xpos` etc. yield none.
5. `knobChanged` fires per node per pointer move when dragging a selection
   (123 032 calls for one drag of 3 002 nodes) — unusable as an invalidation
   hook. `onCreate`/`onDestroy` fire once per node and cost nothing
   measurable (§10, G1/G2/G3).
6. Nuke does its own idle work after a frame change — re-rendering Read
   postage stamps — in ~65 ms (3k) / ~215 ms (10k) chunks under llvmpipe.
   It competes with anything else on the main loop (§11).

## 2. The four implementations

**stock** — builds every requested label synchronously; every changed string
stalls. Baseline for every table.

**debounce (`94e313b`)** — `create_autolabel` returns the cached string if
the node was rebuilt within the last 100 ms (`_label_fresh` cleared by a
single-shot timer), else rebuilds. Rationale at the time: "Nuke calls the
autolabel on every redraw" — which fact 1 shows is false. Effects: the
per-redraw hit path never fires in normal use; during a slider drag every
pointer move still rebuilds and changes the text (the stall, ~200 ms at
10k, exceeds the 100 ms window, so 35 of 39 moves rebuilt); a second edit
inside the window is served the *first* value (RESULTS §4, correctness).

**burst (`label-cache` HEAD `1527d28`)** — `create_autolabel`:
- tracks bursts (requests < 5 ms apart); past 8 requests every cached node
  is answered from the cache (`_content`) without building;
- on a lone request it rebuilds but, if the text changed, keeps returning
  the previously shown string and marks the node stale;
- an idle timer (0.4–1.5 s of quiet, scaled by the measured stall) pokes
  stale nodes; the poked request is forced to build and show;
- when a burst closes, a burst of 9–200 requests is treated as a genuine
  multi-node edit and its names are marked stale; > 200 is assumed to be a
  whole-script pass and left as served (`LABEL_BURST_GENUINE_MAX`);
- frame-dependent cached entries are per frame; served at a new frame they
  are marked stale;
- `onCreate`/`onDestroy` forget cache entries (name reuse after delete /
  rename); nodes without `dope_sheet` (Viewer, Dot, Backdrop…) cannot be
  poked and are always built and shown.
- §10 found the > 200 rule leaves a 1000-node bulk edit stale forever
  (216/1000 updated). The interim fix — classify a big burst as a pass only
  if `nuke.frame()` or the viewer inputs changed since the last burst —
  fixed the measured case but is another guess about triggers (a bulk edit
  that also changes frame or viewer in the same callback is misclassified).
  That is what prompted the spike.

**verify-at-idle (working tree; `verify-at-idle.patch`)** — same burst
serving and hold-back, but:
- every name answered from the cache during a burst goes into `_verify`
  (a set) when the burst closes, whatever its size or cause;
- once quiet, `_verify_slice` re-composes those labels in the background
  in 15 ms slices on a 0 ms `QTimer` (`nuke.runIn(fullName, expr)` gives the
  label code its node context; the compose is read-only — the indicators
  knob write in Foundry's `set_indicators` now happens only on a real
  request), backing off while label traffic resumes;
- only labels whose text differs from what Nuke shows are poked;
- names that were frame-dependent at their last build (`_frame_dep`) are
  verified first and released as soon as that phase ends, then the rest
  (an ordering hint only);
- `LABEL_BURST_GENUINE_MAX`, per-frame cache entries and the frame/viewer
  heuristic are gone. Constants: `LABEL_BURST_GAP_S=0.005`,
  `LABEL_BURST_MIN=8`, `LABEL_REFRESH_MIN/MAX_S=0.4/1.5`,
  `LABEL_STALL_FACTOR=5`, `LABEL_VERIFY_SLICE_S=0.015`, `LABEL_VERIFY_GAP_MS=0`.
- Unit tests: `tests/test_label_cache.py` (94 pass; stubs in `tests/conftest.py`).

## 3. Consolidated results

All numbers from an Intel N100 (4 cores) in Docker, Nuke 17.0v3 under
`xvfb-run` + Mesa llvmpipe (no GPU). Absolute timings are 2–3× a
workstation and the box throttles (same step varies ~2.5× between runs), so
**compare across a row, not between rows**, and prefer the same-session
A/B/C rows. "ms/tick" = latency of the harness's pointer-event chain during
a real XTest drag (lower is better; stock is the floor). "settle" = time
from the action to the last label request Nuke made for it.

### 3.1 Interaction (RESULTS §7, §8; §11)

| gesture | nodes | stock | debounce | burst | verify-at-idle |
|---|---|---|---|---|---|
| slider drag, ms/tick | 3 002 | 16–23 | 120–128 | **20–25** | 22–30 (contaminated run) / 18 ¹ |
| slider drag, ms/tick | 10 002 | 14–22 | 154–188 | **13–18** | **16–20** (also right after a pass) |
| timeslider scrub, ms/tick | 3 002 | 47 | 174 | **5.5** | 90–107 ² |
| timeslider scrub, ms/tick | 10 002 | 185 | 270 | **27** | 169 ² |
| whole-script pass, foreground label time | 3 002 | 213 ms | 405 ms | **60 ms** | 80–160 ms |
| whole-script pass, foreground label time | 10 002 | 703 ms | 1 529 ms | **251 ms** | ~300 ms |
| drag 1 / all nodes, ms/tick | 10 002 | 23 / 509 | 21 / 515 | 21–29 / 496–556 | not re-measured (no label work) |

¹ `spike_verify_d3000_idle.txt` overlapped with a 10k script build on the same
box; `spike_cadence_d3000.txt` "slider drag (quiet)" = 18 ms.
² The §11 scrub steps include a *pending* whole-script pass on their first
frame (an edit preceded them), so they are not the pure scrub of §8; the
verifier itself did not run during the gesture (`busy_exits`).

### 3.2 Whole-script pass, end to end (RESULTS §11)

| | stock | burst | verify-at-idle |
|---|---|---|---|
| 3k, postage stamps off: settle | 0.47 s | ~0.5 s | 0.9–1.3 s (2 608 verifications = 0.40 s compute, 28 slices, no gaps) |
| 3k, stamps on: settle | 0.47 s | ~0.5 s | 2.5 s (same compute, ~65 ms Nuke gaps between slices) |
| 10k, stamps on: frame-dependent readouts visible / all verified | inside a 0.7–2 s UI freeze | inside ~0.25 s + poke pass | 2.4 s / ~13 s, UI live throughout |
| background CPU per pass | 0 (it is all foreground) | 0 | ≈ 0.4 s at 3k, 1.6 s at 10k (N100) |

### 3.3 One vs many nodes (RESULTS §10; E, M, G groups)

burst = `1527d28`; verify-at-idle re-measured only where noted.

| gesture | stock | burst | notes |
|---|---|---|---|
| create 1 / insert in chain | 1 req, 3–9 ms | 1 req (1 build) | |
| create 100 / 200 | 100 req, 0.1 s / 0.2–1.2 s | 100 req, 0.1–0.3 s | Nuke's cost |
| copy 100 / 1000 + paste, originals kept | 0.3 s / 21–37 s | 0.6 s / 24–30 s | Nuke renames + relinks; labels are 170–260 ms of it |
| cut 1 / 100 / 1000 | 6 / 39 / 245 ms | 2 / 33 / 256 ms | delete → 0 requests |
| paste back after cut | 8 / 59 / 517 ms | 3 / 59 / 502 ms | |
| rewire 1 / 200, delete 200 mid-chain | 0 req / 0 req / 6.8 s | 0 / 0 / 8.9 s | Nuke's auto-rewire |
| disable 1000 selected | 884 req, 65 ms | 884 req, 6 ms (8 builds) | text unchanged → cache |
| label knob on 100 | settle 0.11 s | 0.5–0.9 s, 100/100 | one poke pass |
| label knob on 1000 | settle 0.07 s | **216/1000, never refreshed** → §10 fix 1.9 s, 1000/1000 → verify-at-idle 1.2 s (3k) / 0.9 s (10k), 1000/1000 | |
| label knob on all 2 992 | settle 0.31 s | verify-at-idle 2.6 s, 2992/2992 | |
| rename 200 / tile_color 1000 / autoplace 1000 / save / playback | 199 / 0 / 0 / 0 / 0 req | same | |
| 100 Dots, 20 Backdrops | 100 / 20 req | same, built and shown (un-pokeable) | |
| `onCreate`/`onDestroy` off vs on vs empty (paste/delete/undo/redo 1000, create 200) | — | all within noise | G1/G2/G3 |

### 3.4 Correctness matrix (verify-at-idle, `spike_verify_d3000*.txt`, `spike_verify_d10000.txt`)

`_check_bulk` compares the string Nuke was last given (`_shown`) with the
edit for every affected node, after settle.

| case | 3k | 10k |
|---|---|---|
| bulk label on 100 / 1000 / all nodes | OK / OK / OK | OK (1000) |
| bulk + `nuke.frame(+1)` in the same callback | OK | (filter artifact, not paired) |
| bulk + `connectViewer` in the same callback | OK | " |
| bulk then delete 100 of them before idle (undo group) | OK | " |
| bulk then rename 100 of them | OK | " |
| bulk in an undo group, then undo | OK, then 0/1000 OK | " |
| bulk on nodes inside a Group, Group closed / opened | 0/8 (Nuke asks nothing) / 8/8 | " |
| bulk then `set_enabled(False)` → `True` → frame step | OK | " |
| bulk then `scriptClear`; `scriptOpen` | queue 0, content 0; cold pass | " |
| slider drag: final value shown | OK | OK |

burst (`1527d28`) on the same matrix: everything OK **except** label knob on
1000 (216/1000) and clear on 1000 (89 stuck) — the bug that started this.

## 4. Harness

Branch `profiling-harness` (commit `178319f`) archives the harness as of
2026-09-13 20:24; the working copy here has today's additions (groups
M/G/G3/Z/E/S, probes, counters). To reproduce, use this directory or apply
the same edits to the branch; nothing in `.profiling/` is on `label-cache`
(`.gitignore`).

- `run_drawpath.sh <script.nk> <off|on|both>` — clean `HOME`, clean
  `NUKE_TEMP_DIR`, private copy of the script (Nuke's `.autosave` prompt
  blocks forever otherwise), `xvfb-run` with GLX + XTEST, loads
  `drawpath/menu.py` via `NUKE_PATH`.
- `drawpath/menu.py` — every step is its own `QTimer.singleShot` so Nuke's
  real `exec()` loop draws the DAG between steps (pumping `processEvents()`
  inside one callback never lets the label pass run). Real input via XTest
  (`x_drag`, `slider_drag`, `timeslider_drag`); Qt test events are ignored
  by the GL DAG. `use_impl("stock"|"on")` switches implementations in one
  session for fair A/B. `measure()` runs a step, waits for the label
  request count to stop changing for 2.5 s (and for `_verify` to empty),
  logs `calls builds hits action settled_after label_py` plus
  `builds pokes verified(ms) first_release releases slices span gap busy_exits`
  and `ticks lat mean/p95/max` for gestures.
- `LM_PROFILE_ONLY="a,b,c"` — substring filter on step labels (mind
  accidental matches: `"G "` matches `"DAG ("`).
- `LM_STAMPS=off` — the S group turns off `postage_stamp` on every node.
- Scripts: `scripts/lm_d3000.nk`, `scripts/lm_d10000.nk` (regenerate with
  `LM_OUT=… LM_NODES=… nuke -t build_diverse_script.py`: mixed chains,
  Keylight OFX, Groups, expression links, clones, ~10 % Reads with stamps).
- Step groups: base census (untagged), A/B/C/R (same-session A/B/C by
  gesture), H (opHashes), N/K/W (knobChanged cost), Q/T (which knob writes
  relabel), P/D (setValue bursts, debounce), X (scrubs), V (viewer
  connects), M/G/G1/G2/G3 (mass create/paste/delete/undo/redo, hooks
  on/off/empty), Z (undo-history growth), E (cut/paste/rewire/bulk edits/
  nameless nodes/save), S (verify-at-idle spike + break-it cases + probes).

Harness traps that cost time (all confirmed):

- Python-driven `setInput`/`setValue` get an undo entry only inside an
  explicit `nuke.Undo()` begin/end; a bare `nuke.undo()` then undoes the
  previous big operation.
- Undo of a delete makes new C++ nodes; old wrappers raise "PythonObject is
  not attached to a node" — re-resolve by name (`_reresolve`, `pool`).
- `nuke.delete()` per node + one `undo()` + one `redo()` leaves the nodes in
  the script: Z shows 3 003 → 6 003 nodes, and every later step gets slower
  (stock identically) — the apparent "growth" was the harness.
- `nuke.collapseToGroup()` blocks on a modal (expression links leaving the
  group) under Xvfb; the 5-minute `faulthandler` dump is periodic and not a
  hang by itself.
- `set_enabled(True)` re-registers Labelmaker's autolabel *in front of* the
  harness timing wrapper → `calls=0` afterwards unless re-wrapped.
- `nuke.runIn` takes an **expression** (`"__import__('labelmaker')._verify_run()"`),
  not statements.
- A killed run's shell still appends `EXIT` to the results path; the
  harness's low-memory guard killed two runs (paste-1000 cycles); the 10k
  script needs a fresh session (~2 GB).
- `_check_bulk` needles must be the exact tag (`"s"` matched everything).

## 5. Raw files (this directory)

`RESULTS-2026-09-13.md` §1–11 is the narrative; the tables above are
distilled from it. Raw census output per step: `results/*.txt`.

- §1–§8 (census, per-label cost, debounce vs proto): `census_*.txt`,
  `xcensus_*.txt`, `abc_final*_d3000/d10000.txt`, `kc_d3000.txt`, `r_d10000.txt`,
  `port_*.txt`, cProfiles `*.prof`.
- §10 (edge cases on `1527d28`): `edge_d3000_MG.txt`, `edge_d3000_Z.txt`,
  `edge_d3000_E_stock_partial.txt`, `edge_d3000_E_on_partial.txt`,
  `edge_d3000_E_tail.txt` (frame/viewer heuristic), `*_aborted*.txt`.
- §11 (spike): `spike_verify_d3000.txt` (stock + on, first build),
  `spike_verify_d3000_idle.txt`, `spike_verify_d3000_ro.txt`,
  `spike_cadence_d3000.txt`, `spike_slices_d3000.txt`, `spike_probe*_d3000.txt`,
  `spike_nostamps_d3000.txt`, `spike_verify_d10000.txt`, `spike_priority_d10000.txt`.
- `verify-at-idle.patch` — the working-tree diff vs `1527d28`
  (`labelmaker.py`, `tests/`), 550 lines.

## 6. How to reproduce the key claims

```sh
# environment: Nuke 17 on PATH as `nuke`, xvfb-run, Mesa; ~10 min per run
LM_OUT=.profiling/scripts/lm_d3000.nk LM_NODES=3000 nuke -t .profiling/build_diverse_script.py

# burst implementation leaves a 1000-node bulk edit stale (216/1000):
git checkout 1527d28
LM_PROFILE_ONLY="E impl on,E on label knob,E on  -> shown" .profiling/run_drawpath.sh .profiling/scripts/lm_d3000.nk on

# verify-at-idle: same case plus the break-it matrix, both implementations:
git apply .profiling/verify-at-idle.patch
LM_PROFILE_ONLY="S ,scriptClear" .profiling/run_drawpath.sh .profiling/scripts/lm_d3000.nk on
LM_STAMPS=off LM_PROFILE_ONLY="S impl on,S on warm,S on edit,S on pass: frame,scriptClear" .profiling/run_drawpath.sh .profiling/scripts/lm_d3000.nk on

# unit tests
python3 -m pytest tests/ -q && ruff check .
```

Look for `-> shown` lines (`OK`/`MISMATCH`), `first_release=`, `verified=N (ms)`,
`slices= span= gap median/p95`, `ticks=… lat mean=`.

## 7. What we believe the data says

1. The debounce made things worse: it never hits in normal use and drops
   the trailing edit inside its window.
2. The stall on a changed string is the cost that matters; serving bursts
   from cache and holding back changed strings until idle removes it from
   the interaction path (burst and verify-at-idle both: drags at stock
   latency, scrubs far below stock when no pass is pending).
3. Any design that decides *correctness* by classifying a burst has a
   stale-forever failure mode; two such classifiers have now failed
   (size; size + frame/viewer change).
4. verify-at-idle keeps the interaction wins, is correct on every case we
   could construct, and pays for it with background compute after each
   pass (≈ stock's synchronous pass cost, moved off the interaction path)
   and a delay before frame-dependent readouts refresh (~1–2.5 s on this
   box after an edit + frame step; stock shows them inside its freeze).
5. The remaining wall-clock cost of verification on real scripts is Nuke's
   postage-stamp re-rendering sharing the main loop; it is not our CPU.

## 8. Doubts and untested areas for the reviewer

- **Is the post-pass background cost acceptable at 10k+?** Every edit + frame
  step triggers ~1.6 s (N100) / ~0.7 s (workstation) of compute in the
  gaps. A user who edits and steps continuously keeps it busy. The queue is
  bounded (a set ≤ node count) and it yields during traffic, but it is
  constant work stock does not do *after* the pass.
- **Latency to correct readouts.** After a frame step, keyed/expression
  readouts on ~10 % of nodes update ~1–2.5 s later (stamps on) than under
  stock (which freezes instead). Is that the right trade for users?
- **`nuke.runIn` side effects.** Probes show no hidden main-loop cost, but
  we have not checked: behaviour while a Viewer is rendering, nodes inside
  LiveGroups/Precomps, Gizmos with `onCreate` TCL, nodes whose label knob
  contains `[python …]` with side effects (now composed once more per
  pass than stock — same count as a stock pass, but at a different time).
- **The poke.** `dope_sheet` toggle-and-revert with the Dope Sheet panel
  open is untested (does the panel rebuild twice per poke?). `bookmark`
  is the alternative (also measured as one relabel, no undo).
- **Un-pokeable nodes** (no `dope_sheet`: Viewer, Dot, Backdrop, StickyNote,
  PostageStamp…) are always built and shown; a Viewer's label changes
  its text on every input switch → those still stall. Not measured.
- **Frame-dependent hint accuracy.** `_frame_dep` comes from indicator
  bits 1/2 and `[` in the label knob at the last build; a label that
  depends on frame some other way is still verified, just later.
- **Labels Nuke never re-requests** (expression *sources* of a knob shown
  elsewhere, mask wiring): unchanged from stock behaviour? — no: stock
  rebuilds them in every whole-script pass, and so does verification (they
  are re-composed then), so they update after a pass; between passes
  neither implementation updates them. Worth confirming with a test.
- **Refresh window at 10k** hits the 1.5 s cap (`LABEL_STALL_FACTOR ×`
  measured stall); a slider release therefore shows its final value ~1.5 s
  later at 10k, ~0.4 s at 3k. Tunable; not user-tested.
- **Interplay with de-overlap** (`_pending_deoverlap` from `_build_label`
  only, not from verification): a poke → real build → line-count change →
  de-overlap timer. Not exercised with `LM_PROFILE_DEOVERLAP=1` on the
  spike.
- **Other Nuke versions** (only 17.0v3 measured) and a **GPU workstation**
  (stamp renders faster or slower depending on the media; llvmpipe here).
- **Memory**: not measured for the sets (trivial) nor for `runIn` churn.
- **Not covered by the harness**: `collapseToGroup` (modal), LiveGroup /
  Precomp publish, Nuke's own Edit ▸ Cut (assumed = copy + delete),
  undo of a rename, DAG font/preference changes (do they start a pass?
  irrelevant to correctness now, relevant to cost).
