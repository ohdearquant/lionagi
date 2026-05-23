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
    echo "  UI:  http://localhost:3000"
    echo "  API: http://localhost:8765"
    docker run --rm \
        -p 8765:8765 \
        -p 3000:3000 \
        -v "${HOME}/.lionagi:/root/.lionagi" \
        --name lion-studio \
        "${IMAGE}:${TAG}"
fi
