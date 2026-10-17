#!/usr/bin/env bash
# Build and optionally push the Lion Studio Docker image.
#
# Usage:
#   ./scripts/build-studio-docker.sh              # build only
#   ./scripts/build-studio-docker.sh --push        # build + push to ghcr.io
#   ./scripts/build-studio-docker.sh --run         # build + run locally

set -euo pipefail

IMAGE="ghcr.io/ohdearquant/lion-studio"
TAG="${STUDIO_TAG:-latest}"

echo "Building ${IMAGE}:${TAG}..."
docker build -f apps/studio/Dockerfile -t "${IMAGE}:${TAG}" .

if [[ "${1:-}" == "--push" ]]; then
    echo "Pushing ${IMAGE}:${TAG}..."
    docker push "${IMAGE}:${TAG}"
    echo "Done: ${IMAGE}:${TAG}"
elif [[ "${1:-}" == "--run" ]]; then
    echo "Running ${IMAGE}:${TAG}..."
    echo "  UI:  http://127.0.0.1:3000"
    echo "  API: http://127.0.0.1:8765"
    # Loopback host halves, as `li studio --docker` publishes: the image binds
    # 0.0.0.0 inside the container, and a bare PORT:PORT mapping would put that
    # on every host interface, where anyone on the network reaches an
    # unauthenticated daemon. Publish on another address only with
    # LIONAGI_STUDIO_AUTH_TOKEN set.
    docker run --rm \
        -p 127.0.0.1:8765:8765 \
        -p 127.0.0.1:3000:3000 \
        -v "${HOME}/.lionagi:/root/.lionagi" \
        --name lion-studio \
        "${IMAGE}:${TAG}"
fi
