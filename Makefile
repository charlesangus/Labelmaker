# Makefile — regenerate Labelmaker's documentation.
#
#   make screenshots   regenerate all PNGs under docs/images/ (requires Nuke +
#                      nuke-screenshotter + xvfb-run on headless Linux)
#   make pdf           build docs/user-guide.pdf from the README (requires
#                      pandoc + xelatex only — no Nuke, so this runs in CI)
#   make docs          screenshots + pdf (full local regen)
#   make clean         remove generated PDFs
#
# The screenshots come from nuke-screenshotter:
#   pip install "git+https://github.com/charlesangus/nuke-screenshotter"
# Override the Nuke path or screenshotter command if they are not on PATH:
#   make screenshots NUKE=/path/to/nuke SHOTTER=/path/to/nuke-screenshotter

NUKE    ?= nuke
SHOTTER ?= nuke-screenshotter
ZOOM    ?= 2.0

DOCS      := docs
IMAGES    := $(DOCS)/images
BOOTSTRAP := $(DOCS)/screenshots/bootstrap
FEATURES  := $(DOCS)/screenshots/features.nk
SCENARIOS := $(DOCS)/screenshots/panels.scenarios.json
PDF_YAML  := $(DOCS)/pandoc/pdf.yaml
FLOAT_FILTER := $(DOCS)/pandoc/float-images.lua

# The User Guide PDF is derived from the README (single source of truth) by
# build_user_guide_md.py, which drops the install section and adds a title block.
BUILD_DIR   := $(DOCS)/.build
GUIDE_MD    := $(BUILD_DIR)/user-guide.md
GUIDE_SCRIPT := $(DOCS)/pandoc/build_user_guide_md.py

PDFS := $(DOCS)/user-guide.pdf

# Run the capture session in a CLEAN, reproducible Nuke environment:
#  - NUKE_PATH is ONLY our bootstrap (no inherited NUKE_PATH), so the capture
#    loads Labelmaker and nothing else.
#  - HOME points at a throwaway dir so the user's personal ~/.nuke plugins (and
#    their callbacks, which would otherwise fire during the capture) are not
#    sourced. Nuke writes a fresh, empty ~/.nuke there.
# This keeps the docs identical no matter whose machine regenerates them.
export NUKE_PATH := $(CURDIR)/$(BOOTSTRAP)
CAPTURE_HOME := $(CURDIR)/.capture-home

.PHONY: docs screenshots screenshots-dag screenshots-panels pdf clean help

docs: screenshots pdf ## Regenerate screenshots and PDFs (needs Nuke)

screenshots: screenshots-dag screenshots-panels ## Regenerate every PNG (needs Nuke)

screenshots-dag: ## Capture the DAG/autolabel screenshots from features.nk
	mkdir -p "$(CAPTURE_HOME)/.nuke"
	HOME="$(CAPTURE_HOME)" $(SHOTTER) $(FEATURES) $(IMAGES) --zoom $(ZOOM) --nuke-exec $(NUKE)

screenshots-panels: ## Capture the Preferences / Config Editor windows
	mkdir -p "$(CAPTURE_HOME)/.nuke"
	HOME="$(CAPTURE_HOME)" $(SHOTTER) --scenarios $(SCENARIOS) --output-dir $(IMAGES) --nuke-exec $(NUKE)

pdf: $(PDFS) ## Build the User Guide PDF from the README (needs pandoc + xelatex)

# Build docs/user-guide.pdf from README.md. The README's image paths are
# repo-root-relative (docs/images/...), so the resource path starts at $(CURDIR).
$(DOCS)/user-guide.pdf: README.md $(GUIDE_SCRIPT) $(PDF_YAML) $(FLOAT_FILTER) $(DOCS)/latex/training_doc.cls
	mkdir -p $(BUILD_DIR)
	python3 $(GUIDE_SCRIPT) README.md $(GUIDE_MD)
	TEXINPUTS="$(CURDIR)/$(DOCS)/latex:$$TEXINPUTS" \
	pandoc --defaults $(PDF_YAML) --lua-filter $(FLOAT_FILTER) --resource-path "$(CURDIR):$(DOCS):$(DOCS)/latex" -o $@ $(GUIDE_MD)

clean: ## Remove generated PDFs and the intermediate build dir
	rm -f $(PDFS)
	rm -rf $(BUILD_DIR)

help: ## List targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  %-20s %s\n", $$1, $$2}'
