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

echo "deploy_production.sh regression tests passed"
