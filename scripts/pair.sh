#!/bin/sh
set -eu
OMNI_SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$OMNI_SCRIPT_DIR/common.sh"
omni_check_docker
printf '%s\n' 'Creating a one-time local pairing code. Enter it in the browser; do not share it or include it in diagnostics.'
omni_compose exec -T api omniharness auth-code
