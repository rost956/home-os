#!/usr/bin/env bash
set -Eeuo pipefail

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
"${DOCKER[@]}" version >/dev/null

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
if [[ -f "$TARGET/docker-compose.yml" ]]; then
    OLD_CONTAINER="$("${LIVE_COMPOSE[@]}" ps -q web 2>/dev/null || true)"
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
    for item in app scripts ops Dockerfile docker-compose.yml Caddyfile requirements.txt; do
        [[ -e "$TARGET/$item" ]] && SNAPSHOT_PATHS+=("$item")
    done
    if [[ "${#SNAPSHOT_PATHS[@]}" -gt 0 ]]; then
        tar -czf "$SNAPSHOT" -C "$TARGET" "${SNAPSHOT_PATHS[@]}"
    fi
fi

replace_live_code() {
    rm -rf -- "$TARGET/app" "$TARGET/scripts" "$TARGET/ops"
    rm -f -- "$TARGET/Dockerfile" "$TARGET/docker-compose.yml" "$TARGET/requirements.txt"
    tar -C "$SOURCE" -cf - app scripts ops Dockerfile docker-compose.yml requirements.txt | tar -C "$TARGET" -xf -
    cat "$SOURCE/Caddyfile" > "$TARGET/Caddyfile"
}

health_ok() {
    local attempt
    for ((attempt = 1; attempt <= HEALTH_ATTEMPTS; attempt++)); do
        if "${LIVE_COMPOSE[@]}" exec -T web python -c \
            "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()" \
            >/dev/null 2>&1 \
            && [[ -n "$("${LIVE_COMPOSE[@]}" ps --status running -q caddy 2>/dev/null)" ]] \
            && "${LIVE_COMPOSE[@]}" exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile \
                >/dev/null 2>&1; then
            SITE_ADDRESS="$(grep -E '^CADDY_SITE_ADDRESS=' "$TARGET/.env" | tail -n 1 | cut -d= -f2- || true)"
            SITE_ADDRESS="${SITE_ADDRESS%%,*}"
            SITE_ADDRESS="${SITE_ADDRESS//[[:space:]]/}"
            [[ -n "$SITE_ADDRESS" ]] || SITE_ADDRESS="localhost"
            if [[ "$SITE_ADDRESS" == http://* ]]; then
                SITE_SCHEME="http"
                SITE_HOST="${SITE_ADDRESS#http://}"
                SITE_PORT=80
            else
                SITE_SCHEME="https"
                SITE_HOST="${SITE_ADDRESS#https://}"
                SITE_PORT=443
            fi
            SITE_HOST="${SITE_HOST%%/*}"
            SITE_HOST="${SITE_HOST%%:*}"
            if curl --fail --silent --show-error --insecure --max-time 5 \
                --resolve "$SITE_HOST:$SITE_PORT:127.0.0.1" \
                "$SITE_SCHEME://$SITE_HOST:$SITE_PORT/health" >/dev/null 2>&1; then
                return 0
            fi
        fi
        sleep 2
    done
    return 1
}

rollback() {
    echo "Deployment failed; restoring previous code and image"
    if [[ -f "$SNAPSHOT" ]]; then
        rm -rf -- "$TARGET/app" "$TARGET/scripts" "$TARGET/ops"
        rm -f -- "$TARGET/Dockerfile" "$TARGET/docker-compose.yml" "$TARGET/requirements.txt"
        tar -xzf "$SNAPSHOT" -C "$TARGET" --exclude=Caddyfile
        if tar -tzf "$SNAPSHOT" | grep -qx 'Caddyfile'; then
            tar -xOzf "$SNAPSHOT" Caddyfile > "$TARGET/Caddyfile"
        else
            rm -f -- "$TARGET/Caddyfile"
        fi
    fi
    if [[ "$HAD_OLD_IMAGE" == "1" ]]; then
        "${DOCKER[@]}" image tag "$ROLLBACK_IMAGE" "$IMAGE_NAME"
        "${LIVE_COMPOSE[@]}" up -d --no-build --remove-orphans || true
        "${LIVE_COMPOSE[@]}" exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile \
            >/dev/null 2>&1 || true
        health_ok || true
    fi
    "${LIVE_COMPOSE[@]}" ps || true
    "${LIVE_COMPOSE[@]}" logs --tail=100 web caddy || true
}

ROLLBACK_NEEDED=1
trap 'if [[ "$ROLLBACK_NEEDED" == "1" ]]; then rollback; fi' ERR
replace_live_code
set +e
"${LIVE_COMPOSE[@]}" up -d --no-build --remove-orphans
DEPLOY_STATUS=$?
if [[ "$DEPLOY_STATUS" -eq 0 ]]; then
    "${LIVE_COMPOSE[@]}" exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile \
        >/dev/null 2>&1
    DEPLOY_STATUS=$?
fi
if [[ "$DEPLOY_STATUS" -eq 0 ]]; then
    health_ok
    DEPLOY_STATUS=$?
fi
set -e

if [[ "$DEPLOY_STATUS" -ne 0 ]]; then
    rollback
    ROLLBACK_NEEDED=0
    exit 1
fi

ROLLBACK_NEEDED=0
trap - ERR
"${LIVE_COMPOSE[@]}" ps
"${DOCKER[@]}" image rm "$ROLLBACK_IMAGE" >/dev/null 2>&1 || true
find "$TARGET/.deploy" -type f -name 'code-*.tar.gz' -mtime +14 -delete
find "$TARGET/data/deployments" -type f -name '*.log' -mtime +30 -delete
echo "Deployment completed; healthcheck passed"
