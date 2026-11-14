#!/bin/sh
# Source from a script that sets OMNI_SCRIPT_DIR. Never print compose.env.
OMNI_ROOT=$(CDPATH= cd -- "$OMNI_SCRIPT_DIR/.." && pwd)
OMNI_RUNTIME="$OMNI_ROOT/.omniharness"
OMNI_SECRET_DIR="$OMNI_RUNTIME/secrets"
OMNI_COMPOSE_ENV="$OMNI_SECRET_DIR/compose.env"

omni_check_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        printf '%s\n' 'Docker CLI was not found. Install Docker and Compose v2, then retry.' >&2
        return 1
    fi
    docker compose version --short >/dev/null
    omni_engine=$(docker info --format '{{.OSType}}' 2>/dev/null) || {
        printf '%s\n' 'Docker engine is not available. Start it and retry.' >&2
        return 1
    }
    if [ "$omni_engine" != linux ]; then
        printf '%s\n' 'OmniHarness requires a Linux container engine.' >&2
        return 1
    fi
}

omni_initialize() {
    umask 077
    mkdir -p "$OMNI_SECRET_DIR" "$OMNI_RUNTIME/data"
    chmod 700 "$OMNI_SECRET_DIR" "$OMNI_RUNTIME/data"
    if [ ! -f "$OMNI_COMPOSE_ENV" ]; then
        omni_password=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
        [ "${#omni_password}" -eq 64 ] || { printf '%s\n' 'Could not generate a database password.' >&2; return 1; }
        printf 'OMNI_POSTGRES_PASSWORD=%s\nOMNI_UID=%s\nOMNI_GID=%s\n' "$omni_password" "$(id -u)" "$(id -g)" > "$OMNI_COMPOSE_ENV"
        unset omni_password
    fi
    chmod 600 "$OMNI_COMPOSE_ENV"
}

omni_compose_raw() {
    if [ ! -f "$OMNI_COMPOSE_ENV" ]; then
        printf '%s\n' 'Local deployment has not been initialized. Run sh scripts/start.sh first.' >&2
        return 1
    fi
    if [ -f "$OMNI_RUNTIME/deployment.env" ]; then
        docker compose --project-directory "$OMNI_ROOT" -f "$OMNI_ROOT/compose.yaml" --env-file "$OMNI_ROOT/.env" --env-file "$OMNI_COMPOSE_ENV" --env-file "$OMNI_RUNTIME/deployment.env" "$@"
    elif [ -f "$OMNI_ROOT/.env" ]; then
        docker compose --project-directory "$OMNI_ROOT" -f "$OMNI_ROOT/compose.yaml" --env-file "$OMNI_ROOT/.env" --env-file "$OMNI_COMPOSE_ENV" "$@"
    else
        docker compose --project-directory "$OMNI_ROOT" -f "$OMNI_ROOT/compose.yaml" --env-file "$OMNI_COMPOSE_ENV" "$@"
    fi
}

omni_compose_context() {
    command -v realpath >/dev/null 2>&1 || { printf '%s\n' 'realpath is required for safe Compose ownership checks.' >&2; return 1; }
    command -v sha256sum >/dev/null 2>&1 || { printf '%s\n' 'sha256sum is required for safe Compose image isolation.' >&2; return 1; }
    # Configuration may contain credentials. Keep it in memory and output only
    # the project name; never print or save the expanded configuration.
    omni_config=$(omni_compose_raw config) || { printf '%s\n' 'Unable to resolve Compose configuration safely.' >&2; return 1; }
    omni_first_line=$(printf '%s\n' "$omni_config" | sed -n '1p')
    unset omni_config
    case "$omni_first_line" in 'name: '*) omni_project=${omni_first_line#name: } ;; *) printf '%s\n' 'Unable to resolve the Compose project name safely.' >&2; return 1 ;; esac
    case "$omni_project" in ''|*[!a-z0-9_-]*|[-_]* ) printf '%s\n' 'Invalid Compose project name.' >&2; return 1 ;; esac
    omni_physical_root=$(realpath "$OMNI_ROOT") || return 1
    omni_physical_config=$(realpath "$OMNI_ROOT/compose.yaml") || return 1
    omni_containers=$(docker ps -aq --filter "label=com.docker.compose.project=$omni_project") || return 1
    omni_named=$(docker ps -aq --filter "name=^/${omni_project}-(api|worker|postgres|init)-[0-9]+$") || return 1
    omni_containers=$(printf '%s\n%s\n' "$omni_containers" "$omni_named" | sed '/^$/d' | sort -u)
    omni_owned_image=
    omni_image_conflict=false
    for omni_id in $omni_containers; do
        omni_dir=$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$omni_id") || return 1
        omni_file=$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$omni_id") || return 1
        omni_label_project=$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "$omni_id") || return 1
        omni_service=$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.service" }}' "$omni_id") || return 1
        [ -n "$omni_dir" ] && [ -n "$omni_file" ] && [ -n "$omni_service" ] && [ "$omni_dir" != '<no value>' ] && [ "$omni_file" != '<no value>' ] && [ "$omni_service" != '<no value>' ] && [ "$omni_label_project" = "$omni_project" ] || {
            printf '%s\n' 'A same-name Compose container lacks ownership labels; refusing to continue.' >&2; return 1;
        }
        omni_existing_root=$(realpath "$omni_dir") || { printf '%s\n' 'Cannot resolve existing Compose project directory.' >&2; return 1; }
        omni_existing_config=$(realpath "$omni_file") || { printf '%s\n' 'Cannot resolve existing Compose configuration.' >&2; return 1; }
        [ "$omni_existing_root" -ef "$omni_physical_root" ] && [ "$omni_existing_config" -ef "$omni_physical_config" ] || {
            printf '%s\n' 'Compose project name belongs to another directory or configuration. Choose a different COMPOSE_PROJECT_NAME.' >&2; return 1;
        }
        case "$omni_service" in api|worker|init)
            omni_id_image=$(docker inspect --format '{{.Image}}' "$omni_id") || return 1
            case "$omni_id_image" in sha256:*) ;; *) printf '%s\n' 'Cannot verify existing application image.' >&2; return 1 ;; esac
            if [ -n "$omni_owned_image" ] && [ "$omni_owned_image" != "$omni_id_image" ]; then omni_image_conflict=true; fi
            omni_owned_image=$omni_id_image ;;
        esac
    done
    omni_volumes=$(docker volume ls -q --filter "label=com.docker.compose.project=$omni_project") || return 1
    omni_expected="${omni_project}_postgres-data"
    omni_names=$(docker volume ls -q --filter "name=^${omni_expected}$") || return 1
    for omni_volume in $omni_names; do
        if [ "$omni_volume" = "$omni_expected" ]; then
            case "
$omni_volumes
" in *"
$omni_volume
"*) ;; *) printf '%s\n' 'Database volume lacks matching Compose ownership labels; refusing to continue.' >&2; return 1 ;; esac
        fi
    done
    if [ -z "$omni_containers" ] && [ -n "$omni_volumes" ]; then
        printf '%s\n' 'A same-name Compose volume exists without containers proving ownership; refusing to continue.' >&2; return 1
    fi
    omni_hash=$(printf '%s\n%s' "$omni_physical_root" "$omni_project" | sha256sum)
    omni_hash=${omni_hash%% *}
    omni_image="omniharness:project-$(printf '%.24s' "$omni_hash")"
    omni_pinned_release=false
    if [ -f "$OMNI_RUNTIME/deployment.env" ]; then
        omni_release=$(sed -n 's/^OMNI_RELEASE_IMAGE=//p' "$OMNI_RUNTIME/deployment.env")
        if ! printf '%s\n' "$omni_release" | LC_ALL=C grep -Eq '^sha256:[0-9a-f]{64}$'; then
            printf '%s\n' 'The deployment profile must pin an immutable local image ID.' >&2; return 1
        fi
        omni_image=$omni_release
        omni_pinned_release=true
    fi
}

omni_compose() {
    omni_compose_context || return 1
    if [ "${1:-}" = up ]; then
        omni_no_build=false
        for omni_argument in "$@"; do [ "$omni_argument" != --no-build ] || omni_no_build=true; done
        if [ "$omni_no_build" = true ]; then
            if omni_tagged_image=$(docker image inspect "$omni_image" --format '{{.Id}}' 2>/dev/null); then
                if [ -n "$omni_owned_image" ] && { [ "$omni_image_conflict" = true ] || [ "$omni_tagged_image" != "$omni_owned_image" ]; }; then
                    printf '%s\n' 'Project image tag differs from its existing containers. Start without --no-build.' >&2; return 1
                fi
            else
                if [ -z "$omni_owned_image" ] || [ "$omni_image_conflict" = true ]; then
                    printf '%s\n' 'No safely attributable project image exists. Start without --no-build.' >&2; return 1
                fi
                docker tag "$omni_owned_image" "$omni_image" || return 1
            fi
        fi
    fi
    OMNI_IMAGE="$omni_image" omni_compose_raw "$@"
}
