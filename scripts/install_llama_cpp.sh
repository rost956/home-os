#!/usr/bin/env bash
set -Eeuo pipefail

# Reproducible default. Override explicitly only after reading the upstream
# release notes and rerunning the manual smoke/benchmark suite.
LLAMA_CPP_REVISION="${LLAMA_CPP_REVISION:-v0.4.0}"
HOME_AI_PREFIX="${HOME_AI_PREFIX:-/opt/home-ai}"
LLAMA_CPP_DIR="${LLAMA_CPP_DIR:-$HOME_AI_PREFIX/llama.cpp}"
MODEL_DIR="${MODEL_DIR:-$HOME_AI_PREFIX/models}"
SERVICE_USER="${SERVICE_USER:-home-ai}"
SERVICE_GROUP="${SERVICE_GROUP:-home-ai}"
BUILD_JOBS="${BUILD_JOBS:-$(nproc)}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"

die() {
    echo "install_llama_cpp: $*" >&2
    exit 1
}

[[ "$(id -u)" -eq 0 ]] || die "run this installer as root (for example: sudo bash $0)"

case "$(uname -m)" in
    aarch64|arm64) ;;
    *) die "Raspberry Pi ARM64 is required; detected $(uname -m)" ;;
esac

[[ "$HOME_AI_PREFIX" == /* && "$HOME_AI_PREFIX" != "/" ]] || die "unsafe HOME_AI_PREFIX: $HOME_AI_PREFIX"
[[ "$LLAMA_CPP_DIR" == "$HOME_AI_PREFIX"/* ]] || die "LLAMA_CPP_DIR must be below HOME_AI_PREFIX"
[[ "$MODEL_DIR" == "$HOME_AI_PREFIX"/* ]] || die "MODEL_DIR must be below HOME_AI_PREFIX"
[[ "$BUILD_JOBS" =~ ^[1-9][0-9]*$ ]] || die "BUILD_JOBS must be a positive integer"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    cmake \
    curl \
    git \
    libcurl4-openssl-dev \
    pkg-config

if ! getent group "$SERVICE_GROUP" >/dev/null; then
    groupadd --system "$SERVICE_GROUP"
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd \
        --system \
        --gid "$SERVICE_GROUP" \
        --home-dir "$HOME_AI_PREFIX" \
        --no-create-home \
        --shell /usr/sbin/nologin \
        "$SERVICE_USER"
fi
[[ "$(id -gn "$SERVICE_USER")" == "$SERVICE_GROUP" ]] \
    || die "existing user $SERVICE_USER does not use primary group $SERVICE_GROUP"

install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$HOME_AI_PREFIX"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$MODEL_DIR"
install -d -m 0755 -o root -g root "$HOME_AI_PREFIX/bin"
install -d -m 0750 -o root -g "$SERVICE_GROUP" /etc/home-ai

run_as_service_user() {
    runuser -u "$SERVICE_USER" -- "$@"
}

if [[ -e "$LLAMA_CPP_DIR" && ! -d "$LLAMA_CPP_DIR/.git" ]]; then
    die "$LLAMA_CPP_DIR exists but is not a llama.cpp Git checkout"
fi

if [[ ! -d "$LLAMA_CPP_DIR/.git" ]]; then
    run_as_service_user git clone --filter=blob:none https://github.com/ggml-org/llama.cpp.git "$LLAMA_CPP_DIR"
fi

origin_url="$(run_as_service_user git -C "$LLAMA_CPP_DIR" remote get-url origin)"
case "$origin_url" in
    https://github.com/ggml-org/llama.cpp.git|https://github.com/ggml-org/llama.cpp) ;;
    *) die "unexpected llama.cpp origin: $origin_url" ;;
esac

if [[ -n "$(run_as_service_user git -C "$LLAMA_CPP_DIR" status --porcelain --untracked-files=no)" ]]; then
    die "tracked changes exist in $LLAMA_CPP_DIR; preserve or revert them before updating"
fi

run_as_service_user git -C "$LLAMA_CPP_DIR" fetch --tags --prune origin
run_as_service_user git -C "$LLAMA_CPP_DIR" rev-parse --verify "${LLAMA_CPP_REVISION}^{commit}" >/dev/null \
    || die "revision does not resolve to a commit: $LLAMA_CPP_REVISION"
run_as_service_user git -C "$LLAMA_CPP_DIR" checkout --detach "$LLAMA_CPP_REVISION"

run_as_service_user cmake \
    -S "$LLAMA_CPP_DIR" \
    -B "$LLAMA_CPP_DIR/build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DGGML_NATIVE=ON \
    -DLLAMA_BUILD_TESTS=OFF \
    -DLLAMA_BUILD_SERVER=ON \
    -DLLAMA_CURL=ON
run_as_service_user cmake \
    --build "$LLAMA_CPP_DIR/build" \
    --config Release \
    --target llama-server llama-cli \
    --parallel "$BUILD_JOBS"

install -m 0755 -o root -g root "$REPO_ROOT/scripts/run_llama_server.sh" "$HOME_AI_PREFIX/bin/run_llama_server.sh"
install -m 0755 -o root -g root "$REPO_ROOT/scripts/wait_llama_server.sh" "$HOME_AI_PREFIX/bin/wait_llama_server.sh"
install -m 0644 "$REPO_ROOT/ops/home-ai/llama-server.service" /etc/systemd/system/home-ai-llama.service
if [[ ! -e /etc/home-ai/llama-server.env ]]; then
    install -m 0640 -o root -g "$SERVICE_GROUP" \
        "$REPO_ROOT/ops/home-ai/llama-server.env.example" \
        /etc/home-ai/llama-server.env
    echo "Created /etc/home-ai/llama-server.env; review LLAMA_HOST and LLAMA_MODEL_PATH before starting."
else
    echo "Keeping existing /etc/home-ai/llama-server.env unchanged."
fi
systemctl daemon-reload

resolved_revision="$(run_as_service_user git -C "$LLAMA_CPP_DIR" rev-parse HEAD)"
"$LLAMA_CPP_DIR/build/bin/llama-server" --version
echo "llama.cpp $resolved_revision is built in $LLAMA_CPP_DIR."
echo "Models in $MODEL_DIR were not modified. The service was installed but not enabled or started."
