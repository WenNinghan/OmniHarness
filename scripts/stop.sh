#!/bin/sh
set -eu
OMNI_SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$OMNI_SCRIPT_DIR/common.sh"
omni_check_docker
omni_compose stop
printf '%s\n' 'Services stopped. Database volumes, local data and credentials were preserved.'
