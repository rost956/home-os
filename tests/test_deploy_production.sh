#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_SCRIPT="$REPOSITORY_ROOT/scripts/deploy_production.sh"
WORK_DIR="$(mktemp -d)"

cleanup() {
    rm -rf -- "$WORK_DIR"
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
HEALTH_ATTEMPTS=3
HEALTH_RETRY_DELAY=0
TARGET="$TARGET_DIR"
printf 'CADDY_SITE_ADDRESS=home.example.test\n' > "$TARGET/.env"

sleep() {
    :
}

curl() {
    printf 'curl %s\n' "$*" >> "$MOCK_LOG"
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
                    esac
                    ;;
            esac
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
