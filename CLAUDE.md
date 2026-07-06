# Labelmaker — project instructions

Labelmaker is a Nuke plugin that replaces Nuke's autolabel system with rich,
multi-line node labels, plus an Edit-menu Preferences dialog, a Config Editor, and
a De-overlap command.

## Documentation must stay in sync with the code

**Whenever a change affects user-facing behaviour, update the documentation and
regenerate its screenshots in the same change.** User-facing means any of:

- a feature or the per-node-class labels (`labelmaker.py`, `base_config.json`)
- the Edit-menu commands (`menu.py`)
- the Preferences fields (`labelmaker_prefs.py`, `labelmaker_prefs_dialog.py`)
- the Config Editor UI (`labelmaker_config_editor.py`)
- the config JSON format or the config cascade (`labelmaker_config.py`)

The documentation has a single source of truth, `README.md`:

- `README.md` — the complete reference (install, features, configuration,
  preferences); references `docs/images/*.png`
- `docs/user-guide.pdf` — the User Guide, built from `README.md` by `make pdf`
  (pandoc + xelatex). `docs/pandoc/build_user_guide_md.py` derives the Markdown by
  dropping the install section and adding a title block; it is styled with the
  project LaTeX class `docs/latex/training_doc.cls` (+ `docs/latex/logo.pdf`) and
  `docs/pandoc/pdf.yaml`. The PDF is **built locally and committed** — the
  GitHub runner has neither pandoc/TeX Live nor Nuke, so nothing in `docs/` is
  regenerated in CI. Rebuild it with `make pdf` and commit the result whenever the
  README changes.
- `docs/images/*.png` — screenshots, generated (committed to the repo)

Keep the README free of pandoc-specific image attributes (`{ width=… }`): it
renders on GitHub as GFM, which shows those as literal text. PDF image layout and
sizing is handled by `docs/pandoc/float-images.lua` (a pandoc filter that anchors
each screenshot beside its paragraph in a two-column minipage row — text left,
image pinned right at half width — and centres the hero image) plus
`docs/pandoc/pdf.yaml`, not per-image.

### Source → documentation map

| If you change … | Update … | Regenerate … |
|---|---|---|
| `base_config.json`, `labelmaker.py` (label content) | feature sections of `README.md` | DAG screenshots — `docs/screenshots/features.nk` (run `make screenshots-dag`) |
| `menu.py` (menu commands) | menu references in `README.md` | panel screenshots (`make screenshots-panels`) |
| `labelmaker_prefs*.py` (preferences) | Preferences table/section in `README.md` | `prefs_dialog.png` (`make screenshots-panels`) |
| `labelmaker_config_editor.py` | Config Editor section in `README.md` | `config_editor.png` (`make screenshots-panels`); keep `setObjectName` targets in sync with `docs/screenshots/panels.scenarios.json` |
| config format/cascade | Configuration section in `README.md` | — |

### How to regenerate

Screenshots come from [nuke-screenshotter](https://github.com/charlesangus/nuke-screenshotter)
(`pip install` it once), driving a real Nuke under `xvfb-run`:

```sh
make screenshots   # regenerate docs/images/*.png     (needs Nuke; commit the PNGs)
make pdf           # rebuild docs/user-guide.pdf       (needs pandoc + xelatex; commit the PDF)
make docs          # both
```

- DAG/autolabel shots come from `docs/screenshots/features.nk` (regenerate that
  scene with `nuke -t docs/screenshots/build_features_nk.py`).
- Prefs/Config-Editor window shots come from `docs/screenshots/panels.scenarios.json`.
- Both runs load Labelmaker into the capture session via
  `docs/screenshots/bootstrap/menu.py` (put on `NUKE_PATH` by the Makefile).
  Getting the autolabels (not the bare class names) to render used to require a
  cut/paste redraw hack in that bootstrap; nuke-screenshotter v1.2 (#6) does
  the required label warm-up in its capture path, so the hack has been removed.
  Screenshot regeneration therefore needs the **v1.2-or-later** screenshotter
  (`pip install --upgrade "git+https://github.com/charlesangus/nuke-screenshotter"`).

CI (`.github/workflows/release.yml`) does **not** build any docs — the GitHub
runner has neither Nuke (for screenshots) nor pandoc/TeX Live (for the PDF), and
installing the TeX Live toolchain on every release is slow and heavy. It only
bundles the committed `docs/user-guide.pdf` and `docs/images/*.png` into the
release zip, so **both the PDF and the PNGs must be committed**. Regenerate them
locally with `make docs` and commit before tagging a release.

## Conventions

- Run `ruff check .` and `pytest tests/` before committing (see `pyproject.toml`).
- PySide6 imports in `labelmaker*.py` are deferred (not at module level) to avoid
  import-order issues in Nuke; keep them that way.
