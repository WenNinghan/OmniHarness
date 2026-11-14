#!/bin/sh
set -eu
OMNI_SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$OMNI_SCRIPT_DIR/common.sh"
case "${1:-}" in
    '') omni_build=--build ;;
    --no-build) omni_build=--no-build ;;
    *) printf '%s\n' 'Usage: sh scripts/start.sh [--no-build]' >&2; exit 2 ;;
esac
omni_check_docker
omni_initialize
omni_compose config --quiet
omni_compose_context
if [ "$omni_pinned_release" = true ]; then omni_build=--no-build; fi
omni_compose up -d --wait --wait-timeout 180 "$omni_build"
omni_endpoint=$(omni_compose port api 8787)
printf 'Open http://%s\n' "$omni_endpoint"
printf '%s\n' 'Pair your browser by running sh scripts/pair.sh. Existing paired sessions remain usable.'
printf '%s\n' 'Agent and tools require explicit enablement. A pinned release restarts without rebuilding local source.'
printf '%s\n' 'For the reviewed Windows M4 host-broker profile use scripts/start.ps1; see docs/m4-deployment.md.'
