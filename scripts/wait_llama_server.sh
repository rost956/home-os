#!/usr/bin/env bash
set -Eeuo pipefail

LLAMA_STARTUP_TIMEOUT_SECONDS="${LLAMA_STARTUP_TIMEOUT_SECONDS:-540}"

die() {
    echo "wait_llama_server: $*" >&2
    exit 1
}

[[ -n "${LLAMA_HOST:-}" ]] || die "missing LLAMA_HOST"
[[ "${LLAMA_PORT:-}" =~ ^[1-9][0-9]*$ && "$LLAMA_PORT" -le 65535 ]] || die "invalid LLAMA_PORT"
[[ "$LLAMA_STARTUP_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || die "invalid startup timeout"
(( LLAMA_STARTUP_TIMEOUT_SECONDS <= 570 )) || die "startup timeout must leave margin below systemd TimeoutStartSec"

if [[ "$LLAMA_HOST" == *:* ]]; then
    health_url="http://[$LLAMA_HOST]:$LLAMA_PORT/health"
else
    health_url="http://$LLAMA_HOST:$LLAMA_PORT/health"
fi
deadline="$((SECONDS + LLAMA_STARTUP_TIMEOUT_SECONDS))"

while (( SECONDS < deadline )); do
    if curl --fail --silent --show-error --max-time 3 "$health_url" 2>/dev/null \
        | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"'; then
        echo "llama-server is ready at $health_url"
        exit 0
    fi
    sleep 2
done

die "llama-server did not become ready within ${LLAMA_STARTUP_TIMEOUT_SECONDS}s"
