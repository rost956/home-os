#!/usr/bin/env bash
set -Eeuo pipefail

TARGET="/opt/recipe_budget_service"
ARCHIVE=""
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target) TARGET="${2:?}"; shift 2 ;;
        --archive) ARCHIVE="${2:?}"; shift 2 ;;
        --yes) ASSUME_YES=1; shift ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

TARGET="$(realpath -m "$TARGET")"
ARCHIVE="$(realpath -e "$ARCHIVE")"
[[ "$TARGET" == /* && "$TARGET" != "/" && -f "$TARGET/docker-compose.yml" ]] || {
    echo "Unsafe or invalid target: $TARGET" >&2
    exit 2
}
[[ -f "$TARGET/.env" ]] || { echo "Missing $TARGET/.env" >&2; exit 2; }

if [[ "$ASSUME_YES" != "1" ]]; then
    read -r -p "Restore $ARCHIVE into $TARGET/data/app.db? Type RESTORE: " CONFIRMATION
    [[ "$CONFIRMATION" == "RESTORE" ]] || { echo "Cancelled"; exit 1; }
fi

if [[ "${USE_SUDO_DOCKER:-0}" == "1" ]]; then
    DOCKER=(sudo docker)
else
    DOCKER=(docker)
fi
COMPOSE=("${DOCKER[@]}" compose -p recipe_budget_service --env-file "$TARGET/.env" -f "$TARGET/docker-compose.yml" --project-directory "$TARGET")

START_REQUIRED=0
start_web() {
    if [[ "$START_REQUIRED" == "1" ]]; then
        "${COMPOSE[@]}" up -d --no-build
    fi
}
trap start_web EXIT

"${COMPOSE[@]}" stop web
START_REQUIRED=1
python3 "$TARGET/scripts/sqlite_backup.py" restore \
    --archive "$ARCHIVE" \
    --database "$TARGET/data/app.db" \
    --backup-dir "$TARGET/data/backups"
"${COMPOSE[@]}" up -d --no-build
START_REQUIRED=0

for _ in {1..30}; do
    if "${COMPOSE[@]}" exec -T web python -c \
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()" \
        >/dev/null 2>&1; then
        echo "Restore completed; healthcheck passed"
        exit 0
    fi
    sleep 2
done
echo "Restore completed, but healthcheck failed" >&2
exit 1
