# Milestone 1: Verification robustness (findings 2–5)

The four mechanical findings from `CODEX-REVIEW-FINDINGS.md`: the background
verifier and the poke path must survive a failing node, forget nothing they
have not attempted, and leave Nuke's Undo state as they found it. Each task is
one fix plus its pytest coverage in `tests/test_label_cache.py`, committed on
`label-cache`.

## Phase 1.1: Queue integrity

- [ ] M1.P1.T1 — A real request drops the node from `_verify_first` as well as `_verify` (finding 5)
  - files: `labelmaker.py` (`create_autolabel`, ~line 199), `tests/test_label_cache.py`
  - approach: `create_autolabel` currently does `self._verify.discard(full_name)` before a real build; also discard from `self._verify_first` so the "first ⊆ verify" invariant holds. Add a test modelled on `test_real_request_during_verification_drops_the_node_from_the_queue`: make the node frame-dependent (e.g. `labeller._frame_dep.add(name)` before the pass, or monkeypatch `nuke.expression` to return 1.0 so the keys bit is set), run a whole-script pass, then a lone real request for that node, and assert it is in neither `_verify` nor `_verify_first`; then `go_idle` and assert it is not in `labeller.verified`.
  - verify: `python3 -m pytest tests/test_label_cache.py -q` passes, the new test fails on the pre-change code (confirm by stashing the fix once), `ruff check .` clean.
  - size: S

- [ ] M1.P1.T2 — `_verify_slice` survives a failing compose and always reschedules (finding 2)
  - files: `labelmaker.py` (`_verify_slice`, `_pop_verify`, `_compose_in_context`), `tests/test_label_cache.py`
  - approach: Wrap the per-node work inside the slice loop so an exception from `_compose_in_context` (`nuke.runIn` propagates label-code exceptions) is caught per node: log it once via `nuke.warning("Labelmaker: could not verify <full_name>: <exc>")`, leave that node out of the queue (quarantined — it is rebuilt on Nuke's next real request, do not retry it in a loop) and continue with the next node. Put the post-loop scheduling (`_release_stale` / `_get_verify_timer().start(...)`) in a `finally` so the remaining queue is always continued even if something outside the per-node guard raises. Tests: with the `labeller` fixture, make `compose` raise for one name in a 30-node pass and assert (a) every other name ends up in `labeller.verified`, (b) `_verify` is empty after `go_idle`, (c) the failing node is not in `_stale`, (d) the changed labels of the others are still poked. Monkeypatch `nuke.warning` to capture the message.
  - verify: `python3 -m pytest tests/test_label_cache.py -q` passes with the new tests; `ruff check .` clean.
  - size: M

## Phase 1.2: Poke path

- [ ] M1.P2.T3 — `_release_stale` and `_poke_nodes` are per-node transactional (finding 3)
  - files: `labelmaker.py` (`_release_stale`, `_poke_nodes`), `tests/test_label_cache.py`
  - approach: Do not clear `_stale` up front. Restructure so each name is removed from `_stale` only once its poke has been attempted: e.g. `_poke_nodes` takes an iterable and, inside the Undo-disabled block, loops with a per-node `try/except Exception` that logs via `nuke.warning` and moves on (a persistently failing node must not block every later release, so a failed poke is dropped, not re-queued), and `_release_stale` pops names one at a time (`while self._stale: name = self._stale.pop(); ...`) or passes a callback that discards each name after its attempt — pick the simplest shape that keeps `_poke_nodes` usable by `refresh_all_labels` and `set_enabled(False)`. Keep exactly one `Undo.disable()/enable()` pair per batch. Test: 3 stale nodes whose middle node's `dope_sheet` knob `setValue` raises (subclass `_RecordingKnob`); after the refresh fires assert the other two were poked and `_stale` is empty.
  - verify: `python3 -m pytest tests/test_label_cache.py -q` passes including the new test; `ruff check .` clean.
  - size: M

- [ ] M1.P2.T4 — The poke restores the caller's Undo state instead of unconditionally enabling it (finding 4)
  - files: `labelmaker.py` (`_poke_nodes`), `tests/conftest.py` (`_StubUndoManager`), `tests/test_label_cache.py`
  - approach: Before `nuke.Undo.disable()`, read the prior state with `nuke.Undo.disabled()` where it exists (Nuke 17; guard with `getattr(nuke.Undo, "disabled", None)` so older Nukes fall back to the current always-enable behaviour) and in the `finally` call `enable()` only if Undo was enabled on entry. Extend `_StubUndoManager` in `tests/conftest.py` to track a `_disabled` flag with `disable()/enable()/disabled()` and record the call sequence. Tests: (a) with Undo enabled on entry the poke leaves it enabled; (b) with Undo already disabled on entry the poke leaves it disabled and never calls `enable()`; (c) with `disabled` absent from the stub (monkeypatch `delattr`) the old behaviour holds.
  - verify: `python3 -m pytest tests/ -q` passes (the conftest change must not break other test modules); `ruff check .` clean.
  - size: S

**Verification gate:** `ruff check .` clean and `python3 -m pytest tests/ -q` green on `label-cache`; each of findings 2–5 has at least one test that fails without its fix; `git log` shows one commit per task (or one per phase) on `label-cache` with no plan files in it.
