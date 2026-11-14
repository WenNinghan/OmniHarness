#!/bin/sh
# Source this from the project root (or set OMNI_ROOT first), then run commands.
# Usage: . scripts/g-drive-env.sh
# Keeps every cache and temp file on the G: checkout; changes only this shell.
OMNI_G_ROOT=${OMNI_ROOT:-$(pwd -W 2>/dev/null || pwd)}
OMNI_G_CACHE="$OMNI_G_ROOT/.omniharness/cache"
for _d in uv python pip npm tmp playwright xdg hf; do
    mkdir -p "$OMNI_G_CACHE/$_d"
done
UV_CACHE_DIR="$OMNI_G_CACHE/uv"; export UV_CACHE_DIR
UV_PYTHON_INSTALL_DIR="$OMNI_G_CACHE/python"; export UV_PYTHON_INSTALL_DIR
PIP_CACHE_DIR="$OMNI_G_CACHE/pip"; export PIP_CACHE_DIR
npm_config_cache="$OMNI_G_CACHE/npm"; export npm_config_cache
PLAYWRIGHT_BROWSERS_PATH="$OMNI_G_CACHE/playwright"; export PLAYWRIGHT_BROWSERS_PATH
XDG_CACHE_HOME="$OMNI_G_CACHE/xdg"; export XDG_CACHE_HOME
HF_HOME="$OMNI_G_CACHE/hf"; export HF_HOME
TEMP="$OMNI_G_CACHE/tmp"; export TEMP
TMP="$OMNI_G_CACHE/tmp"; export TMP
TMPDIR="$OMNI_G_CACHE/tmp"; export TMPDIR
printf '%s\n' "G-drive environment active. Root: $OMNI_G_ROOT"
