#!/usr/bin/env bash
# Profile Labelmaker in a real (but headless) Nuke GUI session.
#
#   .profiling/run.sh [script.nk] [steps] [mode]
#
# mode: on (default, Labelmaker registered) | off (baseline) | both
set -uo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"

NK=${1:-/tmp/lm_big.nk}
STEPS=${2:-60}
MODE=${3:-both}
NUKE=${NUKE:-nuke}
PROF_HOME="$REPO/.profiling/home"
PROF_TMP="$REPO/.profiling/tmp"

# Every launch starts from a clean slate: fresh HOME (so ~/.nuke holds no
# accumulated prefs/recent files/plugins) and a fresh NUKE_TEMP_DIR (disk
# caches, autosaves), plus Nuke's default /var/tmp/nuke-u$UID cache.
fresh_env() {
  rm -rf "$PROF_HOME" "$PROF_TMP" "/var/tmp/nuke-u$(id -u)"
  mkdir -p "$PROF_HOME/.nuke" "$PROF_TMP"
}

run_one() {
  local mode=$1
  fresh_env
  # Open a private copy: Nuke writes <script>.autosave beside the script it has
  # open, and on the next scriptOpen blocks on a "recover autosave?" prompt.
  cp "$NK" "$PROF_TMP/$(basename "$NK")"
  HOME="$PROF_HOME" \
  NUKE_TEMP_DIR="$PROF_TMP" \
  NUKE_PATH="$REPO/.profiling/bootstrap" \
  LM_PROFILE_SCRIPT="$PROF_TMP/$(basename "$NK")" \
  LM_PROFILE_MODE="$mode" \
  LM_PROFILE_STEPS="$STEPS" \
  LM_PROFILE_ZOOM="${ZOOM:-1.0}" \
  LM_PROFILE_OUT="$REPO/.profiling/labelmaker-$mode.prof" \
  LIBGL_ALWAYS_SOFTWARE=${LIBGL_ALWAYS_SOFTWARE:-1} \
  GALLIUM_DRIVER=${GALLIUM_DRIVER:-llvmpipe} \
  xvfb-run -a --server-args="-screen 0 1920x1080x24 +extension GLX +render" \
    "$NUKE" -q 2>&1
}

if [[ "$MODE" == "both" ]]; then
  run_one off
  run_one on
else
  run_one "$MODE"
fi
