#!/usr/bin/env bash
set -Eeuo pipefail

validate_source_caddyfile() {
    local caddyfile="$1"
    if [[ -d "$caddyfile" ]]; then
        echo "Source Caddyfile must be an ordinary file, not a directory: $caddyfile" >&2
        return 1
    fi
    if [[ ! -e "$caddyfile" ]]; then
        echo "Missing source Caddyfile: $caddyfile" >&2
        return 1
    fi
    if [[ -L "$caddyfile" || ! -f "$caddyfile" ]]; then
        echo "Source Caddyfile must be an ordinary file: $caddyfile" >&2
        return 1
    fi
}

remove_caddyfile() {
    rm -rf -- "$1"
}

install_source_caddyfile() {
    local source_caddyfile="$1"
    local target_caddyfile="$2"

    validate_source_caddyfile "$source_caddyfile" || return 1
    remove_caddyfile "$target_caddyfile" || return 1
    install -m 0644 "$source_caddyfile" "$target_caddyfile" || return 1
    [[ -f "$target_caddyfile" && ! -L "$target_caddyfile" && ! -d "$target_caddyfile" ]] || {
        echo "Failed to create an ordinary target Caddyfile: $target_caddyfile" >&2
        return 1
    }
}

snapshot_has_regular_caddyfile() {
    local snapshot="$1"
    local archive_entries listing entry

    if ! archive_entries="$(tar -tzf "$snapshot")"; then
        echo "Cannot inspect deployment snapshot: $snapshot" >&2
        return 2
    fi
    while IFS= read -r entry; do
        if [[ "$entry" == "Caddyfile" ]]; then
            if ! listing="$(tar -tvzf "$snapshot" -- Caddyfile)"; then
                echo "Cannot inspect Caddyfile in deployment snapshot: $snapshot" >&2
                return 2
            fi
            [[ "${listing:0:1}" == "-" ]] || return 1
            return 0
        fi
    done <<< "$archive_entries"
    return 1
}

restore_caddyfile_from_snapshot() {
    local snapshot="$1"
    local target_caddyfile="$2"
    local temp_caddyfile status

    if snapshot_has_regular_caddyfile "$snapshot"; then
        if ! temp_caddyfile="$(mktemp "$(dirname "$target_caddyfile")/.caddyfile.restore.XXXXXX")"; then
            echo "Cannot create temporary Caddyfile for rollback" >&2
            return 1
        fi
        if ! tar -xOzf "$snapshot" -- Caddyfile > "$temp_caddyfile"; then
            rm -f -- "$temp_caddyfile"
            echo "Cannot extract Caddyfile from deployment snapshot: $snapshot" >&2
            return 1
        fi
        if ! remove_caddyfile "$target_caddyfile" || ! install -m 0644 "$temp_caddyfile" "$target_caddyfile"; then
            rm -f -- "$temp_caddyfile"
            echo "Cannot restore Caddyfile from deployment snapshot" >&2
            return 1
        fi
        rm -f -- "$temp_caddyfile"
        [[ -f "$target_caddyfile" && ! -L "$target_caddyfile" && ! -d "$target_caddyfile" ]] || return 1
        return 0
    else
        status=$?
    fi

    if [[ "$status" -eq 1 ]]; then
        echo "Deployment snapshot has no valid ordinary Caddyfile; leaving it absent" >&2
        remove_caddyfile "$target_caddyfile"
        return $?
    fi
    return "$status"
}

replace_live_code() {
    validate_source_caddyfile "$SOURCE/Caddyfile" || return 1
    rm -rf -- "$TARGET/app" "$TARGET/scripts" "$TARGET/ops" || return 1
    rm -f -- "$TARGET/Dockerfile" "$TARGET/docker-compose.yml" "$TARGET/requirements.txt" || return 1
    tar -C "$SOURCE" -cf - app scripts ops Dockerfile docker-compose.yml requirements.txt | tar -C "$TARGET" -xf - || return 1
    install_source_caddyfile "$SOURCE/Caddyfile" "$TARGET/Caddyfile"
}

wait_for_check() {
    local prefix="$1"
    local label="$2"
    local check_function="$3"
    local attempt status output_file

    output_file="$(mktemp "${TMPDIR:-/tmp}/home-os-deploy-check.XXXXXX")" || return 1
    for ((attempt = 1; attempt <= HEALTH_ATTEMPTS; attempt++)); do
        if "$check_function" >"$output_file" 2>&1; then
            rm -f -- "$output_file"
            echo "$prefix $label: OK"
            return 0
        else
            status=$?
        fi

        if [[ "$attempt" -lt "$HEALTH_ATTEMPTS" ]]; then
            echo "$prefix $label: waiting (attempt $attempt/$HEALTH_ATTEMPTS)" >&2
            sleep "${HEALTH_RETRY_DELAY:-2}"
        fi
    done

    echo "$prefix $label: FAILED after $HEALTH_ATTEMPTS attempts" >&2
    if [[ -s "$output_file" ]]; then
        cat "$output_file" >&2
    fi
    rm -f -- "$output_file"
    return "$status"
}

web_health_check() {
    "${LIVE_COMPOSE[@]}" exec -T web python -c \
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"
}

caddy_running_check() {
    local caddy_container
    caddy_container="$("${LIVE_COMPOSE[@]}" ps --status running -q caddy)"
    [[ -n "$caddy_container" ]] || {
        echo "Caddy container is not running" >&2
        return 1
    }
}

caddy_config_check() {
    "${LIVE_COMPOSE[@]}" exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
}

caddy_admin_check() {
    "${LIVE_COMPOSE[@]}" exec -T caddy wget -q -O /dev/null http://127.0.0.1:2019/config/
}

caddy_reload_check() {
    "${LIVE_COMPOSE[@]}" exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
}

https_health_check() {
    local site_address site_scheme site_host site_port

    site_address="$(grep -E '^CADDY_SITE_ADDRESS=' "$TARGET/.env" | tail -n 1 | cut -d= -f2- || true)"
    site_address="${site_address%%,*}"
    site_address="${site_address//[[:space:]]/}"
    [[ -n "$site_address" ]] || site_address="localhost"
    if [[ "$site_address" == http://* ]]; then
        site_scheme="http"
        site_host="${site_address#http://}"
        site_port=80
    else
        site_scheme="https"
        site_host="${site_address#https://}"
        site_port=443
    fi
    site_host="${site_host%%/*}"
    site_host="${site_host%%:*}"
    curl --fail --silent --show-error --insecure --max-time 5 \
        --resolve "$site_host:$site_port:127.0.0.1" \
        "$site_scheme://$site_host:$site_port/health"
}

health_ok() {
    local prefix="${1:-[deploy]}"
    local previous_caddy_container="${2:-}"
    local current_caddy_container

    wait_for_check "$prefix" "web health" web_health_check || return 1
    wait_for_check "$prefix" "caddy running" caddy_running_check || return 1
    wait_for_check "$prefix" "caddy config" caddy_config_check || return 1

    if ! current_caddy_container="$("${LIVE_COMPOSE[@]}" ps -q caddy)"; then
        echo "$prefix caddy running: FAILED while identifying the container" >&2
        return 1
    fi
    if [[ -n "$previous_caddy_container" && "$current_caddy_container" == "$previous_caddy_container" ]]; then
        echo "$prefix caddy container reused; reloading its updated bind-mounted config"
        wait_for_check "$prefix" "caddy admin" caddy_admin_check || return 1
        wait_for_check "$prefix" "caddy reload" caddy_reload_check || return 1
    else
        echo "$prefix caddy container recreated; startup config is active"
    fi

    wait_for_check "$prefix" "HTTPS health" https_health_check
}

finish_failed_deploy() {
    local deploy_status="$1"

    echo "Deployment failed with status $deploy_status; starting rollback" >&2
    if ! rollback; then
        echo "Rollback also failed; original deployment status was $deploy_status" >&2
    fi
    return "$deploy_status"
}

if [[ "${DEPLOY_PRODUCTION_LIBRARY_ONLY:-0}" == "1" ]]; then
    return 0 2>/dev/null || exit 0
fi

SOURCE=""
TARGET="/opt/recipe_budget_service"
REVISION="unknown"
COMPOSE_PROJECT="recipe_budget_service"
HEALTH_ATTEMPTS=30

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source) SOURCE="${2:?}"; shift 2 ;;
        --target) TARGET="${2:?}"; shift 2 ;;
        --revision) REVISION="${2:?}"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

[[ -n "$SOURCE" ]] || { echo "--source is required" >&2; exit 2; }
SOURCE="$(realpath "$SOURCE")"
TARGET="$(realpath -m "$TARGET")"
[[ "$TARGET" == /* && "$TARGET" != "/" ]] || { echo "Unsafe target: $TARGET" >&2; exit 2; }
[[ "$SOURCE" != "$TARGET" ]] || { echo "Source and target must be different" >&2; exit 2; }
[[ -f "$SOURCE/docker-compose.yml" && -f "$SOURCE/Dockerfile" && -d "$SOURCE/app" ]] || {
    echo "Source does not look like an application checkout" >&2
    exit 2
}
[[ ! -e "$SOURCE/.env" ]] || { echo "Refusing a source tree containing .env" >&2; exit 2; }
if [[ -d "$SOURCE/data" ]] && find "$SOURCE/data" -type f -print -quit | grep -q .; then
    echo "Refusing a source tree containing production data" >&2
    exit 2
fi
validate_source_caddyfile "$SOURCE/Caddyfile" || exit 2
[[ -d "$TARGET" && -w "$TARGET" ]] || {
    echo "Target must already exist and be writable: $TARGET" >&2
    echo "Create it once with: sudo install -d -o \"$USER\" -g \"$USER\" '$TARGET'" >&2
    exit 2
}
[[ -f "$TARGET/.env" ]] || { echo "Missing production environment: $TARGET/.env" >&2; exit 2; }
if find "$TARGET/.env" -perm /077 -print -quit | grep -q .; then
    echo "Production environment is readable by group or others: $TARGET/.env" >&2
    echo "Fix it with: chmod 600 '$TARGET/.env'" >&2
    exit 2
fi
if [[ -d "$TARGET/data" ]] && { [[ ! -w "$TARGET/data" ]] || [[ -e "$TARGET/data/app.db" && ! -w "$TARGET/data/app.db" ]]; }; then
    echo "Production data is not writable by the deployment user" >&2
    echo "One-time fix: sudo chown -R \"$(id -u):$(id -g)\" '$TARGET/data'" >&2
    exit 2
fi

mkdir -p "$TARGET/data/backups" "$TARGET/data/deployments" "$TARGET/.deploy"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_FILE="$TARGET/data/deployments/${STAMP}_${REVISION:0:12}.log"
exec > >(tee -a "$LOG_FILE") 2>&1
exec 9>"$TARGET/data/deploy.lock"
flock -n 9 || { echo "Another deployment is already running" >&2; exit 1; }

if [[ "${USE_SUDO_DOCKER:-0}" == "1" ]]; then
    DOCKER=(sudo docker)
else
    DOCKER=(docker)
fi
COMPOSE=("${DOCKER[@]}" compose -p "$COMPOSE_PROJECT" --env-file "$TARGET/.env")
NEW_COMPOSE=("${COMPOSE[@]}" -f "$SOURCE/docker-compose.yml" --project-directory "$SOURCE")
LIVE_COMPOSE=("${COMPOSE[@]}" -f "$TARGET/docker-compose.yml" --project-directory "$TARGET")
export APP_IMAGE_TAG=local
IMAGE_NAME="recipe-budget-service:local"
ROLLBACK_IMAGE="recipe-budget-service:rollback-$STAMP"
SNAPSHOT="$TARGET/.deploy/code-$STAMP.tar.gz"
HAD_OLD_IMAGE=0

echo "Deploying revision $REVISION at $STAMP"
"${DOCKER[@]}" version

if [[ -f "$TARGET/data/app.db" ]]; then
    python3 "$SOURCE/scripts/sqlite_backup.py" backup \
        --database "$TARGET/data/app.db" \
        --backup-dir "$TARGET/data/backups" \
        --retention 14 \
        --prefix deploy
else
    echo "No existing database yet; skipping pre-deploy backup"
fi

OLD_CONTAINER=""
OLD_CADDY_CONTAINER=""
if [[ -f "$TARGET/docker-compose.yml" ]]; then
    OLD_CONTAINER="$("${LIVE_COMPOSE[@]}" ps -q web 2>/dev/null || true)"
    OLD_CADDY_CONTAINER="$("${LIVE_COMPOSE[@]}" ps -q caddy 2>/dev/null || true)"
fi
if [[ -n "$OLD_CONTAINER" ]]; then
    OLD_IMAGE_ID="$("${DOCKER[@]}" inspect --format '{{.Image}}' "$OLD_CONTAINER")"
    "${DOCKER[@]}" image tag "$OLD_IMAGE_ID" "$ROLLBACK_IMAGE"
    HAD_OLD_IMAGE=1
elif "${DOCKER[@]}" image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    "${DOCKER[@]}" image tag "$IMAGE_NAME" "$ROLLBACK_IMAGE"
    HAD_OLD_IMAGE=1
fi

echo "Building while the current containers remain online"
"${NEW_COMPOSE[@]}" build --pull web
"${DOCKER[@]}" run --rm -v "$TARGET/data:/app/data" "$IMAGE_NAME" sh -c \
    'touch /app/data/.container-write-check && rm /app/data/.container-write-check'

if [[ -f "$TARGET/docker-compose.yml" ]]; then
    SNAPSHOT_PATHS=()
    for item in app scripts ops Dockerfile docker-compose.yml requirements.txt; do
        [[ -e "$TARGET/$item" || -L "$TARGET/$item" ]] && SNAPSHOT_PATHS+=("$item")
    done
    if [[ -f "$TARGET/Caddyfile" && ! -L "$TARGET/Caddyfile" ]]; then
        SNAPSHOT_PATHS+=("Caddyfile")
    elif [[ -e "$TARGET/Caddyfile" || -L "$TARGET/Caddyfile" ]]; then
        echo "Skipping invalid live Caddyfile in snapshot: $TARGET/Caddyfile" >&2
    fi
    if [[ "${#SNAPSHOT_PATHS[@]}" -gt 0 ]]; then
        tar -czf "$SNAPSHOT" -C "$TARGET" "${SNAPSHOT_PATHS[@]}"
    fi
fi

rollback() {
    local rollback_status=0 rollback_caddy_container=""
    echo "[rollback] restoring previous code and image"
    if [[ -f "$SNAPSHOT" ]]; then
        if ! rm -rf -- "$TARGET/app" "$TARGET/scripts" "$TARGET/ops" \
            || ! rm -f -- "$TARGET/Dockerfile" "$TARGET/docker-compose.yml" "$TARGET/requirements.txt" \
            || ! tar -xzf "$SNAPSHOT" -C "$TARGET" --exclude=Caddyfile \
            || ! restore_caddyfile_from_snapshot "$SNAPSHOT" "$TARGET/Caddyfile"; then
            echo "Rollback code restore failed" >&2
            rollback_status=1
        fi
    else
        echo "No deployment snapshot is available for code rollback" >&2
    fi
    if [[ "$HAD_OLD_IMAGE" == "1" ]]; then
        if ! "${DOCKER[@]}" image tag "$ROLLBACK_IMAGE" "$IMAGE_NAME"; then
            echo "[rollback] image restore: FAILED" >&2
            rollback_status=1
        else
            echo "[rollback] image restore: OK"
        fi
        if ! rollback_caddy_container="$("${LIVE_COMPOSE[@]}" ps -q caddy)"; then
            echo "[rollback] could not identify the current Caddy container; checking startup after compose up" >&2
            rollback_caddy_container=""
        fi
        if ! "${LIVE_COMPOSE[@]}" up -d --no-build --remove-orphans; then
            echo "[rollback] compose up: FAILED" >&2
            rollback_status=1
        else
            echo "[rollback] compose up: OK"
            if ! health_ok "[rollback]" "$rollback_caddy_container"; then
                echo "[rollback] health verification: FAILED (the rollback image may predate /health)" >&2
            fi
        fi
    fi
    "${LIVE_COMPOSE[@]}" ps || true
    "${LIVE_COMPOSE[@]}" logs --tail=100 web caddy || true
    return "$rollback_status"
}

handle_deploy_error() {
    local deploy_status="$1"
    trap - ERR
    if [[ "$ROLLBACK_NEEDED" == "1" ]]; then
        finish_failed_deploy "$deploy_status" || true
    fi
    exit "$deploy_status"
}

ROLLBACK_NEEDED=1
trap 'handle_deploy_error "$?"' ERR
replace_live_code
set +e
echo "[deploy] compose up: starting"
"${LIVE_COMPOSE[@]}" up -d --no-build --remove-orphans
DEPLOY_STATUS=$?
if [[ "$DEPLOY_STATUS" -eq 0 ]]; then
    echo "[deploy] compose up: OK"
    health_ok "[deploy]" "$OLD_CADDY_CONTAINER"
    DEPLOY_STATUS=$?
else
    echo "[deploy] compose up: FAILED" >&2
fi
set -e

if [[ "$DEPLOY_STATUS" -ne 0 ]]; then
    finish_failed_deploy "$DEPLOY_STATUS" || true
    ROLLBACK_NEEDED=0
    trap - ERR
    exit "$DEPLOY_STATUS"
fi

ROLLBACK_NEEDED=0
trap - ERR
"${LIVE_COMPOSE[@]}" ps
"${DOCKER[@]}" image rm "$ROLLBACK_IMAGE" >/dev/null 2>&1 || true
find "$TARGET/.deploy" -type f -name 'code-*.tar.gz' -mtime +14 -delete
find "$TARGET/data/deployments" -type f -name '*.log' -mtime +30 -delete
echo "Deployment completed; healthcheck passed"
