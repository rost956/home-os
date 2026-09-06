#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_SCRIPT="$REPOSITORY_ROOT/scripts/deploy_production.sh"
CADDYFILE="$REPOSITORY_ROOT/Caddyfile"
DOCKERFILE="$REPOSITORY_ROOT/Dockerfile"
COMPOSE_FILE="$REPOSITORY_ROOT/docker-compose.yml"
WORK_DIR="$(mktemp -d)"

cleanup() {
    chmod -R u+rwX "$WORK_DIR" 2>/dev/null || true
    rm -rf -- "$WORK_DIR" 2>/dev/null || {
        if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
            sudo -n rm -rf -- "$WORK_DIR"
        fi
    }
}
trap cleanup EXIT

DEPLOY_PRODUCTION_LIBRARY_ONLY=1 source "$DEPLOY_SCRIPT"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

assert_regular_file_with_content() {
    local path="$1"
    local expected="$2"
    [[ -f "$path" && ! -L "$path" && ! -d "$path" ]] || fail "Expected regular file: $path"
    [[ "$(<"$path")" == "$expected" ]] || fail "Unexpected content in $path"
}

write_caddyfile() {
    printf '%s\n' "$2" > "$1/Caddyfile"
}

make_protected_python_cache() {
    local directory="$1"
    local filename="$2"

    mkdir -p "$directory/__pycache__"
    printf 'stale bytecode\n' > "$directory/__pycache__/$filename"
    if [[ "$(id -u)" -ne 0 ]] && command -v sudo >/dev/null 2>&1 \
        && sudo -n true >/dev/null 2>&1; then
        sudo -n chown -R root:root "$directory/__pycache__"
        sudo -n chmod 0555 "$directory/__pycache__"
        sudo -n chmod 0444 "$directory/__pycache__/$filename"
    else
        chmod 0555 "$directory/__pycache__" 2>/dev/null || true
        chmod 0444 "$directory/__pycache__/$filename" 2>/dev/null || true
    fi
}

mock_cleanup_docker() {
    local mount_spec="" source_path=""

    [[ "$1" == "run" ]] || fail "Unexpected cleanup Docker command: $*"
    while [[ $# -gt 0 ]]; do
        if [[ "$1" == "--mount" ]]; then
            mount_spec="$2"
            break
        fi
        shift
    done
    [[ -n "$mount_spec" ]] || fail "Cleanup Docker command has no scoped bind mount"
    source_path="${mount_spec#type=bind,source=}"
    source_path="${source_path%,target=/managed-code}"
    [[ "$source_path" == "$TARGET_DIR/app" || "$source_path" == "$TARGET_DIR/scripts" \
        || "$source_path" == "$TARGET_DIR/ops" ]] || fail "Cleanup escaped managed code: $source_path"

    if [[ "$(id -u)" -ne 0 ]] && command -v sudo >/dev/null 2>&1 \
        && sudo -n true >/dev/null 2>&1; then
        sudo -n find "$source_path" -type d -name __pycache__ -prune -exec rm -rf -- {} +
        sudo -n find "$source_path" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
    else
        chmod -R u+rwX "$source_path" 2>/dev/null || true
        find "$source_path" -type d -name __pycache__ -prune -exec rm -rf -- {} +
        find "$source_path" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
    fi
}

# Docker owns readiness for the single web container. Caddy must proxy it
# directly instead of maintaining a competing active-health state.
if grep -Eq '^[[:space:]]*health_(uri|interval|timeout)[[:space:]]' "$CADDYFILE"; then
    fail "Caddyfile still has active upstream health checks"
fi
grep -Fq 'reverse_proxy web:8000' "$CADDYFILE" || fail "Caddyfile does not proxy the web service"
grep -Fq 'HEALTHCHECK' "$DOCKERFILE" || fail "Docker HEALTHCHECK was removed"
grep -Fq 'condition: service_healthy' "$COMPOSE_FILE" || fail "Caddy no longer waits for Docker web health"
grep -Fxq '**/__pycache__/' "$REPOSITORY_ROOT/.dockerignore" \
    || fail ".dockerignore does not exclude nested Python cache directories"
grep -Fxq '*.py[cod]' "$REPOSITORY_ROOT/.dockerignore" \
    || fail ".dockerignore does not exclude Python bytecode"
grep -Fq '"--exclude=__pycache__"' "$REPOSITORY_ROOT/deploy.ps1" \
    || fail "Manual release archive does not exclude Python cache directories"
grep -Fq '"--exclude=*.py[cod]"' "$REPOSITORY_ROOT/deploy.ps1" \
    || fail "Manual release archive does not exclude Python bytecode"

SOURCE_DIR="$WORK_DIR/source"
TARGET_DIR="$WORK_DIR/target"
mkdir -p "$SOURCE_DIR/app" "$SOURCE_DIR/scripts" "$SOURCE_DIR/ops" "$TARGET_DIR/.deploy"
printf 'FROM scratch\n' > "$SOURCE_DIR/Dockerfile"
printf 'services: {}\n' > "$SOURCE_DIR/docker-compose.yml"
printf 'requirements\n' > "$SOURCE_DIR/requirements.txt"
write_caddyfile "$SOURCE_DIR" "new-caddyfile"

# replace_live_code handles an existing ordinary file, no file, and a directory.
for target_state in file absent directory; do
    rm -rf -- "$TARGET_DIR/Caddyfile"
    case "$target_state" in
        file) write_caddyfile "$TARGET_DIR" "old-caddyfile" ;;
        directory) mkdir "$TARGET_DIR/Caddyfile" ;;
    esac
    SOURCE="$SOURCE_DIR"
    TARGET="$TARGET_DIR"
    replace_live_code
    assert_regular_file_with_content "$TARGET_DIR/Caddyfile" "new-caddyfile"
done

# A stale root-owned cache must not block code replacement. The release payload
# also excludes source-tree bytecode, while .env and persistent data stay intact.
DOCKER=(mock_cleanup_docker)
IMAGE_NAME="recipe-budget-service:test"
printf 'new application code\n' > "$SOURCE_DIR/app/main.py"
printf 'new script code\n' > "$SOURCE_DIR/scripts/task.py"
mkdir -p "$SOURCE_DIR/scripts/__pycache__"
printf 'source cache\n' > "$SOURCE_DIR/scripts/__pycache__/task.cpython-313.pyc"
printf 'source optimized cache\n' > "$SOURCE_DIR/app/main.pyo"
printf 'old script code\n' > "$TARGET_DIR/scripts/old.py"
make_protected_python_cache "$TARGET_DIR/scripts" "home_ai_benchmark_contract.cpython-313.pyc"
printf 'production-secret\n' > "$TARGET_DIR/.env"
mkdir -p "$TARGET_DIR/data"
printf 'persistent-data\n' > "$TARGET_DIR/data/sentinel"
SOURCE="$SOURCE_DIR"
TARGET="$TARGET_DIR"
replace_live_code
[[ -f "$TARGET_DIR/scripts/task.py" && ! -e "$TARGET_DIR/scripts/old.py" ]] \
    || fail "Code replacement did not complete after protected bytecode cleanup"
python_artifacts_absent "$TARGET_DIR/app" || fail "Release copied app bytecode"
python_artifacts_absent "$TARGET_DIR/scripts" || fail "Release copied script bytecode"
[[ "$(<"$TARGET_DIR/.env")" == "production-secret" ]] || fail "Code replacement modified .env"
[[ "$(<"$TARGET_DIR/data/sentinel")" == "persistent-data" ]] \
    || fail "Code replacement modified persistent data"

# New snapshots omit Python artifacts. Rollback also cleans a stale protected
# cache and ignores bytecode from a legacy snapshot before restoring code.
printf 'snapshot application code\n' > "$TARGET_DIR/app/main.py"
make_protected_python_cache "$TARGET_DIR/app" "main.cpython-313.pyc"
printf 'standalone optimized cache\n' > "$TARGET_DIR/scripts/stale.pyo"
CODE_SNAPSHOT="$WORK_DIR/code-snapshot.tar.gz"
create_code_snapshot "$TARGET_DIR" "$CODE_SNAPSHOT" \
    app scripts ops Dockerfile docker-compose.yml requirements.txt Caddyfile
SNAPSHOT_LISTING="$(tar -tzf "$CODE_SNAPSHOT")"
if grep -Eq '(^|/)__pycache__(/|$)|\.py[cod]$' <<< "$SNAPSHOT_LISTING"; then
    fail "Deployment snapshot contains Python runtime artifacts"
fi

LEGACY_SNAPSHOT_SOURCE="$WORK_DIR/legacy-snapshot"
mkdir -p "$LEGACY_SNAPSHOT_SOURCE/app/__pycache__" "$LEGACY_SNAPSHOT_SOURCE/scripts"
printf 'rollback application code\n' > "$LEGACY_SNAPSHOT_SOURCE/app/main.py"
printf 'legacy cache\n' > "$LEGACY_SNAPSHOT_SOURCE/app/__pycache__/main.cpython-313.pyc"
printf 'legacy optimized cache\n' > "$LEGACY_SNAPSHOT_SOURCE/scripts/legacy.pyo"
printf 'FROM scratch\n' > "$LEGACY_SNAPSHOT_SOURCE/Dockerfile"
printf 'services: {}\n' > "$LEGACY_SNAPSHOT_SOURCE/docker-compose.yml"
printf 'requirements\n' > "$LEGACY_SNAPSHOT_SOURCE/requirements.txt"
write_caddyfile "$LEGACY_SNAPSHOT_SOURCE" "rollback-caddyfile"
LEGACY_SNAPSHOT="$WORK_DIR/legacy-code-snapshot.tar.gz"
tar -czf "$LEGACY_SNAPSHOT" -C "$LEGACY_SNAPSHOT_SOURCE" \
    app scripts Dockerfile docker-compose.yml requirements.txt Caddyfile

printf 'failed deployment code\n' > "$TARGET_DIR/app/main.py"
make_protected_python_cache "$TARGET_DIR/scripts" "rollback-stale.cpython-313.pyc"
restore_code_snapshot "$LEGACY_SNAPSHOT" "$TARGET_DIR"
[[ "$(<"$TARGET_DIR/app/main.py")" == "rollback application code" ]] \
    || fail "Rollback did not restore application code"
assert_regular_file_with_content "$TARGET_DIR/Caddyfile" "rollback-caddyfile"
python_artifacts_absent "$TARGET_DIR/app" || fail "Rollback restored legacy app bytecode"
python_artifacts_absent "$TARGET_DIR/scripts" || fail "Rollback restored legacy script bytecode"
[[ "$(<"$TARGET_DIR/.env")" == "production-secret" ]] || fail "Rollback modified .env"
[[ "$(<"$TARGET_DIR/data/sentinel")" == "persistent-data" ]] \
    || fail "Rollback modified persistent data"

# An invalid source must fail before the live Caddyfile is removed.
BAD_SOURCE="$WORK_DIR/bad-source"
mkdir -p "$BAD_SOURCE/Caddyfile"
write_caddyfile "$TARGET_DIR" "live-caddyfile"
SOURCE="$BAD_SOURCE"
TARGET="$TARGET_DIR"
if replace_live_code; then
    fail "Directory source Caddyfile was accepted"
fi
assert_regular_file_with_content "$TARGET_DIR/Caddyfile" "live-caddyfile"

MISSING_SOURCE="$WORK_DIR/missing-source"
mkdir -p "$MISSING_SOURCE"
SOURCE="$MISSING_SOURCE"
if replace_live_code; then
    fail "Missing source Caddyfile was accepted"
fi
assert_regular_file_with_content "$TARGET_DIR/Caddyfile" "live-caddyfile"

# Symlinks are removed and replaced with an ordinary source file when supported by the host.
rm -f -- "$TARGET_DIR/Caddyfile"
if ln -s "$SOURCE_DIR/Caddyfile" "$TARGET_DIR/Caddyfile" 2>/dev/null; then
    SOURCE="$SOURCE_DIR"
    replace_live_code
    assert_regular_file_with_content "$TARGET_DIR/Caddyfile" "new-caddyfile"
else
    echo "Skipping symlink regression case: symlinks are unavailable on this host"
fi

# A snapshot with Caddyfile restores a normal file even if the target became a directory.
SNAPSHOT_WITH_CADDY="$WORK_DIR/with-caddy.tar.gz"
write_caddyfile "$TARGET_DIR" "snapshot-caddyfile"
tar -czf "$SNAPSHOT_WITH_CADDY" -C "$TARGET_DIR" Caddyfile
snapshot_has_regular_caddyfile "$SNAPSHOT_WITH_CADDY"
rm -f -- "$TARGET_DIR/Caddyfile"
mkdir "$TARGET_DIR/Caddyfile"
restore_caddyfile_from_snapshot "$SNAPSHOT_WITH_CADDY" "$TARGET_DIR/Caddyfile"
assert_regular_file_with_content "$TARGET_DIR/Caddyfile" "snapshot-caddyfile"

# A directory archive entry is not a valid Caddyfile snapshot.
INVALID_SNAPSHOT_SOURCE="$WORK_DIR/invalid-snapshot"
mkdir -p "$INVALID_SNAPSHOT_SOURCE/Caddyfile"
INVALID_SNAPSHOT="$WORK_DIR/invalid-caddy.tar.gz"
tar -czf "$INVALID_SNAPSHOT" -C "$INVALID_SNAPSHOT_SOURCE" Caddyfile
if snapshot_has_regular_caddyfile "$INVALID_SNAPSHOT"; then
    fail "Directory Caddyfile was accepted in snapshot"
fi

# A snapshot without Caddyfile removes a malformed target instead of restoring it.
NO_CADDY_SOURCE="$WORK_DIR/no-caddy"
mkdir -p "$NO_CADDY_SOURCE"
printf 'payload\n' > "$NO_CADDY_SOURCE/file"
NO_CADDY_SNAPSHOT="$WORK_DIR/no-caddy.tar.gz"
tar -czf "$NO_CADDY_SNAPSHOT" -C "$NO_CADDY_SOURCE" file
if snapshot_has_regular_caddyfile "$NO_CADDY_SNAPSHOT"; then
    fail "Snapshot without Caddyfile was accepted"
fi
rm -rf -- "$TARGET_DIR/Caddyfile"
mkdir "$TARGET_DIR/Caddyfile"
restore_caddyfile_from_snapshot "$NO_CADDY_SNAPSHOT" "$TARGET_DIR/Caddyfile"
[[ ! -e "$TARGET_DIR/Caddyfile" && ! -L "$TARGET_DIR/Caddyfile" ]] || fail "Invalid target Caddyfile was not removed"

# This archive puts Caddyfile before many entries. Inspection must consume tar output fully,
# rather than using tar | grep -q under pipefail and risking SIGPIPE.
PIPE_SOURCE="$WORK_DIR/pipe-source"
mkdir -p "$PIPE_SOURCE"
write_caddyfile "$PIPE_SOURCE" "pipe-safe-caddyfile"
for index in $(seq 1 1000); do
    printf 'x\n' > "$PIPE_SOURCE/file-$index"
done
PIPE_SNAPSHOT="$WORK_DIR/pipe-safe.tar.gz"
(
    cd "$PIPE_SOURCE"
    tar -czf "$PIPE_SNAPSHOT" Caddyfile file-*
)
snapshot_has_regular_caddyfile "$PIPE_SNAPSHOT"

# Deployment readiness uses retries. These mocks exercise the shell flow without
# talking to Docker or a production target.
MOCK_LOG="$WORK_DIR/deploy-flow.log"
MOCK_CURRENT_CADDY=""
MOCK_CONFIG_FAILURES=0
MOCK_ADMIN_FAILURES=0
MOCK_HTTPS_STATUS=200
MOCK_DIAGNOSTICS_FAIL=0
HEALTH_ATTEMPTS=3
HEALTH_RETRY_DELAY=0
TARGET="$TARGET_DIR"
printf 'CADDY_SITE_ADDRESS=home.example.test\n' > "$TARGET/.env"

sleep() {
    :
}

curl() {
    printf 'curl %s\n' "$*" >> "$MOCK_LOG"
    printf '%s' "$MOCK_HTTPS_STATUS"
    [[ "$MOCK_HTTPS_STATUS" == "200" ]] || return 22
}

mock_compose() {
    printf 'compose %s\n' "$*" >> "$MOCK_LOG"
    case "$1" in
        ps)
            printf '%s\n' "$MOCK_CURRENT_CADDY"
            ;;
        exec)
            shift
            [[ "$1" == "-T" ]] && shift
            case "$1" in
                web)
                    ;;
                caddy)
                    shift
                    case "$*" in
                        *"caddy validate"*)
                            if [[ "$MOCK_CONFIG_FAILURES" -gt 0 ]]; then
                                MOCK_CONFIG_FAILURES=$((MOCK_CONFIG_FAILURES - 1))
                                echo "Caddy config is not ready yet" >&2
                                return 1
                            fi
                            ;;
                        *"wget -q -O /dev/null http://127.0.0.1:2019/config/"*)
                            if [[ "$MOCK_ADMIN_FAILURES" -gt 0 ]]; then
                                MOCK_ADMIN_FAILURES=$((MOCK_ADMIN_FAILURES - 1))
                                echo "Caddy admin endpoint is not ready yet" >&2
                                return 1
                            fi
                            ;;
                        *"caddy reload"*)
                            ;;
                        *"wget -S -O - http://web:8000/health"*)
                            if [[ "$MOCK_DIAGNOSTICS_FAIL" -gt 0 ]]; then
                                echo "Caddy-to-web diagnostic failed" >&2
                                return 1
                            fi
                            ;;
                    esac
                    ;;
            esac
            ;;
        logs)
            if [[ "$MOCK_DIAGNOSTICS_FAIL" -gt 0 ]]; then
                echo "Caddy log diagnostic failed" >&2
                return 1
            fi
            ;;
        *)
            fail "Unexpected compose command: $*"
            ;;
    esac
}

LIVE_COMPOSE=(mock_compose)

# A fresh Caddy container may not be ready on the first health attempt. The
# deploy flow waits for it and never reloads a newly created container.
: > "$MOCK_LOG"
MOCK_CURRENT_CADDY="caddy-new"
MOCK_CONFIG_FAILURES=1
MOCK_ADMIN_FAILURES=0
health_ok "[deploy]" "caddy-old"
[[ "$(grep -Fc 'caddy validate' "$MOCK_LOG")" -eq 2 ]] || fail "Caddy readiness was not retried"
if grep -Fq 'caddy reload' "$MOCK_LOG"; then
    fail "Fresh Caddy container was reloaded unnecessarily"
fi
grep -Fq '/health' "$MOCK_LOG" || fail "Deployment no longer checks HTTPS /health"

# A reused container must wait for the Caddy admin endpoint before reloading.
: > "$MOCK_LOG"
MOCK_CURRENT_CADDY="caddy-reused"
MOCK_CONFIG_FAILURES=0
MOCK_ADMIN_FAILURES=1
health_ok "[deploy]" "caddy-reused"
[[ "$(grep -Fc 'wget -q -O /dev/null http://127.0.0.1:2019/config/' "$MOCK_LOG")" -eq 2 ]] \
    || fail "Caddy admin readiness was not retried"
[[ "$(grep -Fc 'caddy reload' "$MOCK_LOG")" -eq 1 ]] || fail "Reused Caddy container was not reloaded once"

# Permanent readiness failure reports the failed stage and its last real error.
: > "$MOCK_LOG"
MOCK_CURRENT_CADDY="caddy-stuck"
MOCK_CONFIG_FAILURES=0
MOCK_ADMIN_FAILURES=3
READINESS_FAILURE_LOG="$WORK_DIR/readiness-failure.log"
if health_ok "[deploy]" "caddy-stuck" > "$READINESS_FAILURE_LOG" 2>&1; then
    fail "Permanent Caddy admin failure was accepted"
fi
grep -Fq '[deploy] caddy admin: FAILED after 3 attempts' "$READINESS_FAILURE_LOG" \
    || fail "Permanent readiness failure did not name the failed stage"
grep -Fq 'Caddy admin endpoint is not ready yet' "$READINESS_FAILURE_LOG" \
    || fail "Permanent readiness failure hid the last Caddy error"

# HTTPS 502/503 keeps the final end-to-end probe strict, records diagnostics
# before rollback, and preserves the original deployment exit status.
: > "$MOCK_LOG"
MOCK_CURRENT_CADDY="caddy-https-failure"
MOCK_CONFIG_FAILURES=0
MOCK_ADMIN_FAILURES=0
MOCK_HTTPS_STATUS=503
MOCK_DIAGNOSTICS_FAIL=1
HTTPS_FAILURE_LOG="$WORK_DIR/https-failure.log"
if health_ok "[deploy]" "caddy-old" > "$HTTPS_FAILURE_LOG" 2>&1; then
    fail "HTTPS 503 was accepted"
fi
[[ "$LAST_FAILED_CHECK" == "HTTPS health" ]] || fail "HTTPS failure did not keep its stage"
[[ "$HTTPS_HEALTH_HTTP_STATUS" == "503" ]] || fail "HTTPS failure did not keep its status"
rollback() {
    echo '[rollback] simulated failure' >&2
    return 1
}
HTTPS_ROLLBACK_LOG="$WORK_DIR/https-rollback.log"
if (finish_failed_deploy 22) > "$HTTPS_ROLLBACK_LOG" 2>&1; then
    fail "HTTPS deployment failure unexpectedly returned success"
else
    https_rollback_exit_status=$?
fi
[[ "$https_rollback_exit_status" -eq 22 ]] || fail "HTTPS failure exit status was not preserved"
grep -Fq '[deploy] HTTPS health diagnostics for HTTP 503' "$HTTPS_ROLLBACK_LOG" \
    || fail "HTTPS 503 diagnostics were not logged"
grep -Fq '[deploy] caddy logs (last 100 lines):' "$HTTPS_ROLLBACK_LOG" \
    || fail "Caddy logs were not requested before rollback"
grep -Fq '[deploy] Caddy -> web /health:' "$HTTPS_ROLLBACK_LOG" \
    || fail "Caddy-to-web health diagnostics were not requested"
grep -Fq 'Caddy log diagnostic failed' "$HTTPS_ROLLBACK_LOG" \
    || fail "Failed Caddy log diagnostics were not visible"
grep -Fq 'Caddy-to-web diagnostic failed' "$HTTPS_ROLLBACK_LOG" \
    || fail "Failed Caddy-to-web diagnostics were not visible"
grep -Fq '[rollback] simulated failure' "$HTTPS_ROLLBACK_LOG" \
    || fail "HTTPS failure did not invoke rollback"
grep -Fq 'Rollback also failed; original deployment status was 22' "$HTTPS_ROLLBACK_LOG" \
    || fail "HTTPS rollback failure masked the original status"

# A rollback failure is explicitly logged but preserves the original deploy status.
rollback() {
    echo '[rollback] simulated failure' >&2
    return 1
}
ROLLBACK_FAILURE_LOG="$WORK_DIR/rollback-failure.log"
if (finish_failed_deploy 73) > "$ROLLBACK_FAILURE_LOG" 2>&1; then
    fail "Failed deployment unexpectedly returned success"
else
    rollback_exit_status=$?
fi
[[ "$rollback_exit_status" -eq 73 ]] || fail "Original deployment status was not preserved"
grep -Fq 'Deployment failed with status 73; starting rollback' "$ROLLBACK_FAILURE_LOG" \
    || fail "Original deployment failure was not logged"
grep -Fq 'Rollback also failed; original deployment status was 73' "$ROLLBACK_FAILURE_LOG" \
    || fail "Rollback failure masked the original deployment status"

echo "deploy_production.sh regression tests passed"
