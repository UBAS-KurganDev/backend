#!/usr/bin/env bash

set -Eeuo pipefail

readonly DEPLOY_ROOT="${UBAS_DEPLOY_ROOT:-/opt/ubas}"
readonly REGISTRY="registry.yandexcloud.net"
readonly TRACKED_IMAGE="${REGISTRY}/cn12qrbnsnck5l6a3en0/ubas-backend:master"
readonly METADATA_TOKEN_URL="http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"

cd "${DEPLOY_ROOT}"
exec 9>"${DEPLOY_ROOT}/poll.lock"
if ! flock --nonblock 9; then
  echo "Another registry poll is already running"
  exit 0
fi

docker_config="$(mktemp -d "${DEPLOY_ROOT}/.docker-auth.XXXXXX")"
container_id=""
release_dir=""
cleanup() {
  if [[ -n "${container_id}" ]]; then
    docker rm --force "${container_id}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${release_dir}" ]]; then
    rm -rf -- "${release_dir}"
  fi
  docker logout "${REGISTRY}" >/dev/null 2>&1 || true
  rm -rf -- "${docker_config}"
}
trap cleanup EXIT
export DOCKER_CONFIG="${docker_config}"

iam_token="$({
  curl --fail --silent --show-error \
    --header 'Metadata-Flavor: Google' \
    "${METADATA_TOKEN_URL}"
} | jq --exit-status --raw-output '.access_token')"
printf '%s' "${iam_token}" | docker login \
  --username iam \
  --password-stdin \
  "${REGISTRY}"
unset iam_token

docker pull "${TRACKED_IMAGE}"
image_id="$(docker image inspect --format '{{.Id}}' "${TRACKED_IMAGE}")"
if [[ -f "${DEPLOY_ROOT}/.deployed-image-id" ]] \
  && [[ "$(<"${DEPLOY_ROOT}/.deployed-image-id")" == "${image_id}" ]]; then
  echo "Production image has not changed"
  exit 0
fi

release_id="$({
  docker image inspect \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "${TRACKED_IMAGE}"
})"
if [[ ! "${release_id}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Image has no valid revision label" >&2
  exit 1
fi
image_digest="$({
  docker image inspect --format '{{index .RepoDigests 0}}' "${TRACKED_IMAGE}"
})"
if [[ ! "${image_digest}" =~ ^registry\.yandexcloud\.net/cn12qrbnsnck5l6a3en0/ubas-backend@sha256:[0-9a-f]{64}$ ]]; then
  echo "Registry returned an unexpected image digest" >&2
  exit 1
fi

release_dir="${DEPLOY_ROOT}/staging/${release_id}"
rm -rf -- "${release_dir}"
install -d -m 0750 "${release_dir}"
container_id="$(docker create "${image_digest}")"
for file in compose.yaml deploy-production.sh planner.json poll-production.sh; do
  docker cp \
    "${container_id}:/opt/ubas-deployment/${file}" \
    "${release_dir}/${file}"
done
docker rm "${container_id}" >/dev/null
container_id=""

/bin/bash "${release_dir}/deploy-production.sh" \
  "${image_digest}" \
  "${release_id}"
printf '%s\n' "${image_id}" >"${DEPLOY_ROOT}/.deployed-image-id.next"
mv -f \
  "${DEPLOY_ROOT}/.deployed-image-id.next" \
  "${DEPLOY_ROOT}/.deployed-image-id"
