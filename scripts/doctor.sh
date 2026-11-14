#!/bin/sh
set -eu
OMNI_SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$OMNI_SCRIPT_DIR/common.sh"
printf '%s\n' 'OmniHarness local deployment diagnostic (no environment values or credential contents)'
if [ -f "$OMNI_ROOT/compose.yaml" ]; then printf '%s\n' 'Compose file: present'; else printf '%s\n' 'Compose file: missing'; exit 1; fi
if [ -f "$OMNI_COMPOSE_ENV" ]; then printf '%s\n' 'Private deployment configuration: present'; else printf '%s\n' 'Private deployment configuration: missing; run start.sh first'; fi
omni_check_docker
printf '%s\n' 'Docker Linux engine: ready'
docker compose version --short
omni_compose config --quiet
printf '%s\n' 'Compose configuration: valid'
omni_compose ps --all
omni_compose exec -T api python -c "import urllib.request; response = urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=5); assert response.status == 200; print('API health: HTTP 200 (inside container)')"
printf '%s\n' 'Provider connectivity is not checked here. Use each model configuration connection test in the UI.'
