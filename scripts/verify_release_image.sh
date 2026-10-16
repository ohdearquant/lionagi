#!/usr/bin/env bash
# Check the image produced by this run, rather than any older image with its tag.
set -euo pipefail

if [[ ! "${VERSION:-}" =~ ^[0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[0-9]+)?(\.post[0-9]+)?(\.dev[0-9]+)?$ ]]; then
  echo '::error::version must be a numeric x.y.z release, optionally with a/b/rc/post/dev suffix' >&2
  exit 1
fi
if [[ ! "${EXPECTED_DIGEST:-}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo '::error::the image build did not produce a valid digest' >&2
  exit 1
fi
if [[ "${PUBLISH_LATEST:-}" != true && "${PUBLISH_LATEST:-}" != false ]]; then
  echo '::error::latest publication decision must be true or false' >&2
  exit 1
fi

repo=ohdearquant/lion-studio
token="$(curl --fail --silent --show-error --connect-timeout 10 --max-time 30 \
  "https://ghcr.io/token?scope=repository:${repo}:pull" | jq -er '.token')"

digest_for() {
  local headers
  headers="$(curl --fail --silent --show-error --connect-timeout 10 --max-time 30 --head \
    -H "Authorization: Bearer ${token}" \
    -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json' \
    "https://ghcr.io/v2/${repo}/manifests/$1")" || return 1
  printf '%s\n' "${headers}" | tr -d '\r' | \
    awk 'tolower($1) == "docker-content-digest:" { print $2 }'
}

for attempt in 1 2 3 4 5; do
  version_digest="$(digest_for "${VERSION}")" || version_digest=""
  latest_digest="${EXPECTED_DIGEST}"
  if [ "${PUBLISH_LATEST}" = true ]; then
    latest_digest="$(digest_for latest)" || latest_digest=""
  fi
  if [ "${version_digest}" = "${EXPECTED_DIGEST}" ] && \
     [ "${latest_digest}" = "${EXPECTED_DIGEST}" ]; then
    echo "Verified ghcr.io/${repo}:${VERSION} at ${EXPECTED_DIGEST}"
    exit 0
  fi
  echo "Attempt ${attempt}: registry has not returned the digest published by this run."
  if [ "${attempt}" -lt 5 ]; then sleep 15; fi
done
echo "::error::the requested image tags do not resolve to this run's build digest"
exit 1
