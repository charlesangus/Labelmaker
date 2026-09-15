---
title: Label cache — address the Codex review of verify-at-idle
status: running
current: M3.P2.T4
ship: none
pm_heartbeat: 2026-09-15T13:37:32-04:00
publish_decisions: none
---

# Goal

The `label-cache` branch (verify-at-idle label cache, HEAD `e661f42`) is ready to
merge to `master`: the five findings in `CODEX-REVIEW-FINDINGS.md` are fixed
with pytest coverage, background verification never executes Tcl/Python from a
node's label knob, the README/User Guide describe the resulting behaviour, and
the real-Nuke profiling harness has been extended with the review's untested
edge cases and re-run against the fixed code with the results recorded.

# Context and constraints

- All code work lands as ordinary commits on the existing `label-cache` branch
  (`ship: none`). No milestone branches, no PRs; the branch is merged to
  `master` as a whole afterwards. Never commit to `master` or `profiling-harness`
  from the main working tree.
- The cache lives in `labelmaker.py` (`AutolabelReplacement`, ~lines 105–420:
  `create_autolabel`, `_build_label`, `_compose_label`, `_verify_slice`,
  `_pop_verify`, `_compose_in_context`, `_poke_nodes`, `_release_stale`,
  `_forget`, `invalidate_labels`) with unit tests in `tests/test_label_cache.py`
  (fixtures `labeller`, `request`, `whole_script_pass`, `go_idle`, `pokes`;
  `_FakeTimer` stands in for QTimer; `nuke` is a stub from `tests/conftest.py`,
  `_StubUndoManager` there is a no-op).
- Conventions (CLAUDE.md): run `ruff check .` and `pytest tests/` before every
  commit (invoke pytest as `python3 -m pytest tests/` — there is no `python` on
  PATH). PySide6 imports stay deferred inside methods. User-facing behaviour
  changes require a README update **and** `make pdf` (pandoc + xelatex are
  installed) in the same change; the PDF is committed.
- The review being addressed is `CODEX-REVIEW-FINDINGS.md` in the repo root
  (untracked; do not commit it). Findings: (1) verification re-executes label-knob
  Tcl/Python; (2) one compose exception abandons the verify queue; (3)
  `_release_stale` clears `_stale` before poking; (4) `_poke_nodes` clobbers the
  caller's Undo state; (5) a real request leaves an orphan in `_verify_first`;
  (6) evidence incomplete for release confidence.
- The profiling harness is `.profiling/` — git-ignored on `label-cache`, tracked
  on the `profiling-harness` branch (currently identical to what is on disk).
  `.profiling/README.md` explains it; `.profiling/run_drawpath.sh <script.nk>
  on|off|both` drives a real Nuke under `xvfb-run` (Nuke is at
  `~/.local/bin/nuke`; the box is an Intel N100 with llvmpipe only, so runs
  are slow — 10k-node runs take many minutes; run them in the background with
  a generous timeout). Step groups live in `.profiling/drawpath/menu.py`
  (group `S` ≈ lines 840–900 is the verify-at-idle set; `_check_bulk`,
  `_check_label`, `_undoable`, `_walk`, `log` are the helpers). Prebuilt test
  scripts: `.profiling/scripts/lm_d3000.nk`, `lm_d10000.nk`.
  **Never `git checkout profiling-harness` in the main working tree** — the
  ignored `.profiling/` files would be silently overwritten. Commit harness
  changes through a separate worktree: `git worktree add ../Labelmaker-harness
  profiling-harness`, copy the changed `.profiling/` files there, commit, push.
- Nuke under Xvfb gotchas (memory): labels are only computed from Nuke's exec()
  loop — steps must be `QTimer.singleShot`-scheduled, never pumped inside one
  callback; never `scriptOpen` a file with a stale `.autosave` beside it (the
  harness copies scripts to a fresh temp dir for this reason); kill stray Nukes
  with `pkill -x`, not `-f`.

# Board

| ID | Milestone | Status | File |
|----|-----------|--------|------|
| M1 | Verification robustness (findings 2–5) | done | [M1-verification-robustness.md](PLAN/MILESTONES/M1-verification-robustness.md) |
| M2 | Side-effect-free verification (finding 1) + docs | done | [M2-side-effect-free-verification.md](PLAN/MILESTONES/M2-side-effect-free-verification.md) |
| M3 | Harness expansion and re-validation (finding 6) | doing | [M3-harness-revalidation.md](PLAN/MILESTONES/M3-harness-revalidation.md) |

# Open questions

_None._
