#!/usr/bin/env bash
set -Eeuo pipefail

MODEL_URL="${MODEL_URL:-}"
MODEL_PATH="${MODEL_PATH:-}"
MODEL_SHA256="${MODEL_SHA256:-}"
MIN_FREE_MIB="${MIN_FREE_MIB:-512}"

usage() {
    cat <<'EOF'
Usage:
  MODEL_URL=https://... MODEL_PATH=/opt/home-ai/models/model.gguf \
    MODEL_SHA256=<optional-sha256> bash scripts/download_home_ai_model.sh

The download is written to a sibling .partial file, verified, then atomically
renamed. Existing models are never overwritten or removed.
EOF
}

die() {
    echo "download_home_ai_model: $*" >&2
    exit 1
}

if [[ "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi
[[ $# -eq 0 ]] || die "unknown argument: $1"
[[ "$MODEL_URL" == https://* ]] || die "MODEL_URL must use https://"
[[ "$MODEL_PATH" == /* && "$MODEL_PATH" == *.gguf ]] || die "MODEL_PATH must be an absolute .gguf path"
[[ "$MIN_FREE_MIB" =~ ^[1-9][0-9]*$ ]] || die "MIN_FREE_MIB must be a positive integer"
if [[ -n "$MODEL_SHA256" ]]; then
    [[ "$MODEL_SHA256" =~ ^[[:xdigit:]]{64}$ ]] || die "MODEL_SHA256 must contain 64 hexadecimal characters"
    MODEL_SHA256="${MODEL_SHA256,,}"
fi

model_dir="$(dirname -- "$MODEL_PATH")"
partial_path="$MODEL_PATH.partial"
[[ -d "$model_dir" && -w "$model_dir" ]] || die "target directory is missing or not writable: $model_dir"
[[ ! -e "$MODEL_PATH" ]] || die "target already exists; refusing to overwrite: $MODEL_PATH"
[[ ! -L "$partial_path" ]] || die "refusing a symlink partial path: $partial_path"

headers="$(curl --fail --silent --show-error --location --head --max-redirs 5 \
    --proto '=https' --proto-redir '=https' "$MODEL_URL")" \
    || die "could not inspect model URL"
expected_bytes="$(printf '%s\n' "$headers" | awk '
    BEGIN { IGNORECASE=1 }
    /^content-length:/ { gsub("\r", "", $2); if ($2 ~ /^[0-9]+$/) n=$2 }
    END { if (n) print n }
')"
[[ -n "$expected_bytes" ]] || die "server did not provide a final Content-Length; set a direct downloadable URL"

free_kib="$(df -Pk -- "$model_dir" | awk 'NR==2 {print $4}')"
required_kib="$(( (expected_bytes + 1023) / 1024 + MIN_FREE_MIB * 1024 ))"
if (( free_kib < required_kib )); then
    die "not enough free space: need model size plus ${MIN_FREE_MIB} MiB safety margin"
fi

cleanup_partial() {
    rm -f -- "$partial_path"
}
trap cleanup_partial ERR INT TERM

echo "Downloading $MODEL_URL"
curl \
    --fail \
    --location \
    --proto '=https' \
    --proto-redir '=https' \
    --max-filesize "$expected_bytes" \
    --retry 5 \
    --retry-delay 2 \
    --retry-connrefused \
    --show-error \
    --output "$partial_path" \
    "$MODEL_URL"

actual_bytes="$(stat -c %s -- "$partial_path")"
[[ "$actual_bytes" -eq "$expected_bytes" ]] \
    || die "download size mismatch: expected $expected_bytes bytes, got $actual_bytes"

if [[ -n "$MODEL_SHA256" ]]; then
    actual_sha256="$(sha256sum -- "$partial_path" | awk '{print $1}')"
    [[ "$actual_sha256" == "$MODEL_SHA256" ]] \
        || die "SHA-256 mismatch: expected $MODEL_SHA256, got $actual_sha256"
fi

chmod 0640 "$partial_path"
mv --no-clobber --no-target-directory -- "$partial_path" "$MODEL_PATH"
[[ -e "$MODEL_PATH" && ! -e "$partial_path" ]] || die "target appeared during download; refusing to overwrite it"
trap - ERR INT TERM
echo "Model is ready at $MODEL_PATH ($actual_bytes bytes)."
