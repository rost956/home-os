#!/usr/bin/env bash
set -Eeuo pipefail

required_variables=(
    LLAMA_SERVER_BIN
    LLAMA_WORKING_DIRECTORY
    LLAMA_MODEL_PATH
    LLAMA_MODEL_ALIAS
    LLAMA_HOST
    LLAMA_PORT
    LLAMA_THREADS
    LLAMA_THREADS_BATCH
    LLAMA_CTX_SIZE
    LLAMA_BATCH_SIZE
    LLAMA_UBATCH_SIZE
    LLAMA_PARALLEL
    LLAMA_N_PREDICT
    LLAMA_API_KEY_FILE
)

die() {
    echo "run_llama_server: $*" >&2
    exit 1
}

for variable_name in "${required_variables[@]}"; do
    [[ -n "${!variable_name:-}" ]] || die "missing $variable_name in EnvironmentFile"
done

[[ "$LLAMA_SERVER_BIN" == /* && -x "$LLAMA_SERVER_BIN" ]] || die "LLAMA_SERVER_BIN is not an executable absolute path"
[[ "$LLAMA_WORKING_DIRECTORY" == /* && -d "$LLAMA_WORKING_DIRECTORY" ]] || die "invalid LLAMA_WORKING_DIRECTORY"
[[ "$LLAMA_MODEL_PATH" == /* && -r "$LLAMA_MODEL_PATH" && "$LLAMA_MODEL_PATH" == *.gguf ]] \
    || die "LLAMA_MODEL_PATH is not a readable absolute .gguf path"
[[ "$LLAMA_API_KEY_FILE" == /* && -r "$LLAMA_API_KEY_FILE" ]] || die "LLAMA_API_KEY_FILE is not readable"
[[ "$LLAMA_MODEL_ALIAS" =~ ^[A-Za-z0-9._-]{1,200}$ ]] || die "invalid LLAMA_MODEL_ALIAS"
[[ "$LLAMA_HOST" != "0.0.0.0" && "$LLAMA_HOST" != "::" ]] || die "refusing a wildcard LLAMA_HOST"

for variable_name in \
    LLAMA_PORT LLAMA_THREADS LLAMA_THREADS_BATCH LLAMA_CTX_SIZE \
    LLAMA_BATCH_SIZE LLAMA_UBATCH_SIZE LLAMA_PARALLEL LLAMA_N_PREDICT; do
    [[ "${!variable_name}" =~ ^[1-9][0-9]*$ ]] || die "$variable_name must be a positive integer"
done
(( LLAMA_PORT <= 65535 )) || die "LLAMA_PORT must be at most 65535"
(( LLAMA_PARALLEL == 1 )) || die "PHASE 6.5 permits exactly one inference slot"
(( LLAMA_CTX_SIZE >= 1024 && LLAMA_CTX_SIZE <= 8192 )) || die "LLAMA_CTX_SIZE must be between 1024 and 8192"
(( LLAMA_BATCH_SIZE <= 512 )) || die "LLAMA_BATCH_SIZE must be at most 512 on the Pi profile"
(( LLAMA_UBATCH_SIZE <= LLAMA_BATCH_SIZE )) || die "LLAMA_UBATCH_SIZE must not exceed LLAMA_BATCH_SIZE"

if [[ "${1:-}" == "--check" ]]; then
    [[ $# -eq 1 ]] || die "--check accepts no additional arguments"
    exit 0
fi
[[ $# -eq 0 ]] || die "unknown argument: $1"

cd -- "$LLAMA_WORKING_DIRECTORY"
exec "$LLAMA_SERVER_BIN" \
    --model "$LLAMA_MODEL_PATH" \
    --alias "$LLAMA_MODEL_ALIAS" \
    --host "$LLAMA_HOST" \
    --port "$LLAMA_PORT" \
    --threads "$LLAMA_THREADS" \
    --threads-batch "$LLAMA_THREADS_BATCH" \
    --ctx-size "$LLAMA_CTX_SIZE" \
    --batch-size "$LLAMA_BATCH_SIZE" \
    --ubatch-size "$LLAMA_UBATCH_SIZE" \
    --parallel "$LLAMA_PARALLEL" \
    --n-predict "$LLAMA_N_PREDICT" \
    --no-webui \
    --no-slots \
    --metrics \
    --api-key-file "$LLAMA_API_KEY_FILE"
