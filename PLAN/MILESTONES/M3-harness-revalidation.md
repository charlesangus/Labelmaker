# Milestone 3: Harness expansion and re-validation (finding 6)

The review called the evidence "good qualitatively, incomplete for release
confidence", and the review brief's §8 (`.profiling/REVIEW-BRIEF-2026-09-14.md`)
lists what was never exercised. This milestone adds a step group to the
real-Nuke draw-path harness covering the M1/M2 fixes and those untested areas,
re-runs the 3k and 10k censuses against the fixed `label-cache` code, records
the results, and commits the harness on `profiling-harness`. Depends on M1 and
M2 being on `label-cache`.

Harness mechanics a fresh agent needs: `.profiling/drawpath/menu.py` is put on
`NUKE_PATH` by `run_drawpath.sh`; it wraps whichever label function is live in
`_timed_autolabel` (counts `counter["calls"]`), switches implementations with
`use_impl("stock"|"on")`, runs steps as `(label, callable)` tuples each scheduled
on its own `QTimer.singleShot`, and filters them with `LM_PROFILE_ONLY="V"` (a
substring match on the step label). `_check_bulk(nodes, needle, expect=None)`
and `_check_label(node, knob)` verify what the DAG shows; `_undoable(name, fn)`
wraps an undo group; `log()` writes to the census output. `autolabeller` is the
live `AutolabelReplacement`. Runs must go through `run_drawpath.sh` (fresh HOME,
private script copy — see `.profiling/README.md`), in the background with a long
timeout; 10k runs take many minutes on this box.

## Decisions

- 2026-09-15 — Finding 4 reversed by the harness: `nuke.Undo.disable()/enable()` nest as a depth counter (group `V` case (d): the M1.P2.T4 guard leaked one `disable()` per poke batch and needed 4 extra `enable()` calls to recover). `e7c6bf3` restores the unconditional balanced pair, which is what preserves the caller's state; the unit stub models the counter. Codex's premise (a boolean toggle) was wrong.
- 2026-09-15 — Group `V` case (a) shows `on` runs label-knob Tcl `LABEL_BURST_MIN` (8) times more than stock per bulk edit (208 vs 200): the first 8 requests of a burst are built for real, held, then force-rebuilt at release. Verification itself runs no Tcl (frame/viewer passes: 0). A consultant confirmed serving the held `_content` at the forced re-request is safe (since 566db23 `_content` has one writer); `bcdc0a3` adds a `_fresh` set for it with 9 tests, so Tcl runs once per request for held labels too. Case (a) should now read 200/100/41 under `on`, to be confirmed by T4's re-run.
- 2026-09-15 — Harness-only fixes landed with T1: `_counting_poke_nodes` forwards `on_attempted` (broke every release after `70f719f`); the load path uses `register_autolabel()/unregister_autolabel()` instead of `set_enabled(False/True)`, because two unmatched load-time `disable()`s (before Undo is initialised) kill Undo for the whole session. Fixtures exclude clones and expression-driven `size`.

- 2026-09-15 — T2 findings (run `/tmp/v2_d3000.txt`): `activeViewer().play(1)` advances one frame in 3 s under Xvfb, so case (g) is environmental; (g2) steps `nuke.frame()` on a 40 ms timer instead. The headless Dope Sheet is floated as a top-level window (`nuke.menu("Pane")` is empty headless). Residual accepted: a slider drag on a Tcl-labelled node runs its Tcl +1/+2 vs stock (42 vs 40–41), the release poke's rebuild after a drag whose last requests were burst-served; not a doubling.

## Phase 3.1: New harness cases

- [x] M3.P1.T1 — Step group `V`: regression cases for the review findings
  - files: `.profiling/drawpath/menu.py`
  - approach: Add a `V` group (modelled on `S`, run for both `stock` and `on`) with: (a) **Tcl side effects** — set the label knob of 200 pool nodes to `[python {__import__('menu_probe').tick()}]`-style code (put a tiny counter module beside `menu.py` on `NUKE_PATH`, or use a `nuke.root()` custom knob as the counter) and log the execution count after each of: a frame-step pass, a viewer-connect pass, a bulk edit of those nodes' `label`, and a slider drag; the expected `on` count per pass is ≤ the `stock` count and never 2× it. (b) **Tcl label + other change** — on 100 Tcl-labelled nodes edit a knob that appears in their knob readout during one callback; `_check_bulk` afterwards must show the new readout with exactly one Tcl execution each. (c) **Compose failure** — wrap `autolabeller._compose_label` so it raises for one named node, trigger a pass, confirm via `len(autolabeller._verify) == 0` after idle and that the other changed labels were released; restore the wrapper. (d) **Undo state** — call `nuke.Undo.disable()`, bulk-edit 100 nodes, wait for the release, log `nuke.Undo.disabled()` (must still be True), then `enable()`. (e) **Transactional release** — make one stale node's `dope_sheet` knob raise (e.g. a node with the knob replaced by a `nuke.PyCustom_Knob`, or by deleting the node between the edit and the release), confirm the others are still released.
  - verify: `LM_PROFILE_ONLY="V" .profiling/run_drawpath.sh .profiling/scripts/lm_d3000.nk both` completes without a Python traceback in the census output and every `->` check line reports the expected count; ruff is not run on `.profiling/` (ignored), but the file must import cleanly (`python3 -c "import ast,sys; ast.parse(open('.profiling/drawpath/menu.py').read())"`).
  - size: L

- [x] M3.P1.T2 — Step group `V`: panel, playback and Viewer cases from the brief's §8
  - files: `.profiling/drawpath/menu.py`
  - approach: Extend `V` with: (a) **Dope Sheet open** — show the Dope Sheet pane (`nukescripts.panels` / `nuke.menu("Pane")`), bulk-edit 100 nodes and log label calls and wall time for the release vs. the same edit with it closed (does the `dope_sheet` poke rebuild the panel?). (b) **Viewer rendering** — connect the Viewer to the deepest chain and start playback (`nuke.activeViewer().play(1)`, or `nuke.frame()` on a 40 ms timer) while a bulk edit's verification runs; log settle time and that `_check_bulk` passes; stop playback. (c) **Un-pokeable Viewer stall** — switch the Viewer input 20 times 40 ms apart; log per-tick label time for the Viewer node under `stock` and `on`. Leave the script as the group found it (close the pane, stop playback, restore the Viewer input).
  - verify: `LM_PROFILE_ONLY="V" .profiling/run_drawpath.sh .profiling/scripts/lm_d3000.nk both` runs these cases without tracebacks; each logs a comparable `stock` and `on` line.
  - size: M

- [ ] M3.P1.T3 — Step group `V`: node-type, expression-source and de-overlap cases from the brief's §8
  - files: `.profiling/drawpath/menu.py`, `.profiling/build_diverse_script.py` (only if the script needs new node types baked in)
  - approach: Extend `V` with: (a) **LiveGroup / Precomp / Gizmo with `onCreate` Tcl** — create at runtime a `LiveGroup` holding 20 inner nodes, a `Precomp`, and a Group whose `onCreate` knob runs Tcl; bulk-edit inside them, `nuke.showDag` each, confirm with `_check_bulk` that labels update and that no `runIn` warning was logged; delete them at the end of the case. (b) **Expression sources** — make one Grade's `white` the expression source for 50 other nodes' knobs that appear in their readouts; change it once, then a frame-step pass; confirm the 50 dependents show the new value after the pass under both implementations, and log whether they updated *before* the pass (neither should). (c) **De-overlap interplay** — with `LM_PROFILE_DEOVERLAP=1`, bulk-edit 300 nodes so their line count grows; confirm no overlap afterwards and log `len(autolabeller._pending_deoverlap)` before the deoverlap timer fires (verification's pokes must feed it). Any nodes added must be removed within the case so later groups see the same script.
  - verify: `LM_PROFILE_ONLY="V" .profiling/run_drawpath.sh .profiling/scripts/lm_d3000.nk both` (and once with `LM_PROFILE_DEOVERLAP=1`) runs these cases without tracebacks; each logs a comparable `stock` and `on` line.
  - size: M

## Phase 3.2: Re-run and record

- [ ] M3.P2.T4 — Full census re-run at 3k and 10k against the fixed code
  - files: `.profiling/results/` (new `v_d3000.txt`, `v_d10000.txt`, `s_d3000_fixed.txt`, `s_d10000_fixed.txt`), no source changes
  - approach: From `label-cache` at the M2 gate commit, run `.profiling/run_drawpath.sh .profiling/scripts/lm_d3000.nk both` and the 10k script with `LM_PROFILE_ONLY="S"` (the existing verify-at-idle cases, to confirm nothing regressed) and `LM_PROFILE_ONLY="V"`, capturing each run's output into `.profiling/results/<name>.txt`. Run each in the background with a ≥ 30-minute timeout, one at a time (one Nuke per box); before each run `pkill -x Nuke` any stray instance. If a run hangs past the timeout, kill it, note it in the results file name (`_aborted`) and re-run once.
  - verify: the four result files exist, end with the harness's finish summary, and every `S` `->` check reports the same pass/fail as §11 of `RESULTS-2026-09-13.md` (all OK) and every `V` check the expected count.
  - size: M

- [ ] M3.P2.T5 — Write up §12 of the results and refresh the harness README
  - files: `.profiling/RESULTS-2026-09-13.md` (append `## 12. Review fixes and expanded edge cases (2026-09-xx)`), `.profiling/README.md` (mention group `V` and the new counter module if any)
  - approach: Summarise per case: what was exercised, stock vs. on numbers (label calls, Tcl executions, settle times), pass/fail, and any new residual (e.g. Tcl-only changes staying stale by design — cite M2's decision). Keep the same tabular style as §11. Note the code SHA tested and the harness SHA.
  - verify: the section reads standalone; every claim cites a `results/*.txt` file.
  - size: S

- [ ] M3.P2.T6 — Commit the harness changes and results on `profiling-harness`
  - files: `.profiling/**` (via a separate worktree of `profiling-harness`)
  - approach: `git worktree add ../Labelmaker-harness profiling-harness`; `rsync -a --exclude home --exclude tmp .profiling/ ../Labelmaker-harness/.profiling/`; in that worktree `git add .profiling && git commit` with a message listing the new group, results and §12; `git push origin profiling-harness`; then `git worktree remove ../Labelmaker-harness`. Do **not** check out `profiling-harness` in the main working tree (the ignored `.profiling/` would be clobbered).
  - verify: `git log --oneline -1 profiling-harness` shows the commit; `git diff --stat label-cache profiling-harness -- .profiling` lists only `.profiling/` files; `git status` on `label-cache` is unchanged (only the untracked `CODEX-REVIEW-FINDINGS.md`).
  - size: S

**Verification gate:** all `S` cases still pass at 3k and 10k on the fixed code; every `V` case has a logged stock-vs-on comparison and no traceback; §12 written; harness + results pushed on `profiling-harness`; `label-cache` untouched by this milestone except that its HEAD is the SHA the results cite.
