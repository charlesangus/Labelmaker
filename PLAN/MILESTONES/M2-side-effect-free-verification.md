# Milestone 2: Side-effect-free verification (finding 1) + docs

Background verification (`_verify_slice` → `_compose_in_context` → `_compose_label`)
currently re-runs `label_readout_creator`, which substitutes the node's label
knob with `nuke.tcl("subst", …)` — so `[python …]`/Tcl in a label knob executes
speculatively, and twice for a changed label (verify, then the forced relabel).
After this milestone verification never substitutes the label knob: it compares
a **verify key** — the composed lines with the label knob's *raw* text appended —
against the key recorded at the node's last real build. A difference pokes the
node (the real build then executes the Tcl exactly once, as stock does); no
difference means the cached label stands, even if the Tcl's output would have
changed. Depends on M1 (same functions; land M1 first to avoid conflicts).

## Decisions

- 2026-09-15 — Tcl/Python in a label knob is never executed by verification, and a `[`-label is poked only if the rest of its label changed: the user accepts that a Tcl-only change (e.g. `[frame]` after a frame step, `[value size]` after a bulk edit that touches nothing else in the label) can stay stale until Nuke next asks for that node — Nuke itself already often misses Tcl-label rebuilds and users know that limitation. Chosen over "always poke `[`-nodes after a burst" (an extra relabel of every Tcl-labelled node per pass) and over a per-node frame stamp (more state for a hole that is accepted anyway).

## Phase 2.1: Verify without substitution

- [x] M2.P1.T1 — Compose a substitution-free verify key and compare keys instead of texts
  - files: `labelmaker.py` (`_compose_label`, `label_readout_creator`, `_build_label`/`create_autolabel`, `_verify_run`, `_verify_slice`, `_note_frame_dependence`, `_forget`, `invalidate_labels`, `__init__`), `tests/test_label_cache.py` (fixtures only)
  - approach: (1) Give `_compose_label` a `substitute_label=True` parameter and thread it into `label_readout_creator`: read `self.node_label_raw = nuke.value("this.label", "") or ""` as now; when substituting, append the `nuke.tcl("subst", …)` result as today; in both modes record `self.verify_key = "\n".join(lines_before_label + ([raw] if raw else []))` — the lines composed so far plus the raw knob text — so the key is derived from the same compose and costs no second pass. (2) At a real build store `self._verify_key[full_name] = self.verify_key` alongside `_content` (a new dict, cleared in `invalidate_labels`, popped in `_forget`; update the `__init__` comments, and fix the stale `_content` comment that still says "(frame or None, text)"). (3) `_verify_run` calls `_compose_label(substitute_label=False)` and returns `self.verify_key`; `_verify_slice` compares the returned key with `self._verify_key.get(full_name)` and adds the node to `_stale` on difference — it must **no longer write `_content`** (the served label stays the last real build's substituted text). (4) Drop the `"[" in node_label_raw` clause from `_note_frame_dependence` (Tcl output is no longer verified, so it is not a "verify first" hint any more); keep the indicator-bits clause. Update the `labeller` fixture's `build`/`compose` stubs and `_verify_run` interplay so the existing suite keeps passing (e.g. `build` records `labeller._verify_key[name]`, `compose` returns the key) — do not weaken existing assertions; `test_tcl_in_the_label_knob_is_composed_in_node_context` and `test_frame_dependence_is_noted_from_the_build` will need their expectations adjusted for (4).
  - verify: `python3 -m pytest tests/ -q` green; `ruff check .` clean; `grep -n "_content\[" labelmaker.py` shows no write inside `_verify_slice`.
  - size: M

- [x] M2.P1.T2 — Tests pinning the no-execution contract
  - files: `tests/test_label_cache.py`
  - approach: Using the real `_compose_label` (no fixture stubbing of compose; monkeypatch `nuke.value` to return `"[frame]"` for `this.label` and `nuke.tcl` to count calls and return e.g. `"1001"`), cover: (a) a real build calls `nuke.tcl("subst", …)` exactly once and shows the substituted text; (b) a whole-script pass served from the cache followed by `go_idle` calls `nuke.tcl` **zero** times and, with nothing else changed, pokes nothing even if `nuke.tcl` would now return `"1002"`; (c) when a knob readout line changes for that node (e.g. monkeypatch the node's configured knob value or `nuke.expression` so `_note_frame_dependence`/indicators or a readout line differs) verification pokes the node and the resulting real build calls `nuke.tcl` exactly once more — one execution per pass, never two. Reuse `request`/`whole_script_pass`/`go_idle` where the fixture's stubs are compatible, or build a small dedicated fixture that only stubs the timers and `nuke.runIn`.
  - verify: the new tests pass; temporarily reverting M2.P1.T1's step (3) makes test (b)'s `nuke.tcl` count non-zero (confirm once, then restore).
  - size: M

## Phase 2.2: Documentation

- [x] M2.P2.T3 — Document the Tcl-label behaviour in the README and rebuild the User Guide
  - files: `README.md` (performance/caching paragraph, currently around line 177–178), `docs/user-guide.pdf`
  - approach: Extend the existing bullet about background re-checking with one or two sentences: label-knob text containing Tcl or `[python …]` is never executed by the background check — such a label refreshes when Nuke next asks for the node or when anything else in its label changes, so a purely Tcl-driven change (like `[frame]`) may lag until then; the label code never runs more often than Nuke's own autolabel would. Keep GFM-clean (no pandoc attributes). Run `make pdf` and commit README + PDF together (CLAUDE.md rule). No screenshots change.
  - verify: `make pdf` succeeds; `git status` shows only `README.md` and `docs/user-guide.pdf` changed; the README renders the new sentence in the caching bullet.
  - size: S

**Verification gate:** `ruff check .` clean, `python3 -m pytest tests/ -q` green; test M2.P1.T2(b) proves zero `nuke.tcl` calls during verification; README and `docs/user-guide.pdf` committed together; the milestone's diff on `label-cache` (`git diff <M1 gate commit>..HEAD`) reviewed per the `ship: none` review round with findings recorded below.
