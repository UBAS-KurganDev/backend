#!/usr/bin/env bash

set -Eeuo pipefail

readonly IMAGE="${1:?Pass the immutable Docker image digest}"
readonly RELEASE_ID="${2:?Pass the release commit SHA}"
readonly DEPLOY_ROOT="${UBAS_DEPLOY_ROOT:-/opt/ubas}"
readonly RELEASE_DIR="${DEPLOY_ROOT}/staging/${RELEASE_ID}"
readonly REGISTRY_PATH="registry.yandexcloud.net/cn12qrbnsnck5l6a3en0/ubas-backend"

if [[ ! "${RELEASE_ID}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Release ID must be a full lowercase Git commit SHA" >&2
  exit 2
fi
image_digest="${IMAGE#"${REGISTRY_PATH}@sha256:"}"
if [[ "${IMAGE}" != "${REGISTRY_PATH}@sha256:${image_digest}" ]] \
  || [[ ! "${image_digest}" =~ ^[0-9a-f]{64}$ ]]; then
  echo "Image must be an immutable digest from the UBAS registry" >&2
  exit 2
fi
for file in compose.yaml deploy-production.sh planner.json poll-production.sh; do
  if [[ ! -f "${RELEASE_DIR}/${file}" ]]; then
    echo "Missing release file: ${RELEASE_DIR}/${file}" >&2
    exit 2
  fi
done
if [[ ! -f "${DEPLOY_ROOT}/.env" ]]; then
  echo "Missing server environment: ${DEPLOY_ROOT}/.env" >&2
  exit 2
fi
if [[ ! -f "${DEPLOY_ROOT}/var/topology-catalog.sqlite3" ]]; then
  echo "Missing production city catalog" >&2
  exit 2
fi
docker image inspect "${IMAGE}" >/dev/null

cd "${DEPLOY_ROOT}"
exec 9>"${DEPLOY_ROOT}/deploy.lock"
flock 9

next_env="$(mktemp "${DEPLOY_ROOT}/.deploy.env.next.XXXXXX")"
rollback_dir="$(mktemp -d "${DEPLOY_ROOT}/.rollback.XXXXXX")"

cleanup() {
  rm -f -- "${next_env}"
  rm -rf -- "${rollback_dir}"
}
trap cleanup EXIT

printf 'UBAS_IMAGE=%s\n' "${IMAGE}" >"${next_env}"
docker compose \
  --file "${RELEASE_DIR}/compose.yaml" \
  --env-file .env \
  --env-file "${next_env}" \
  config --quiet

for path in compose.yaml .deploy.env config/planner.json; do
  if [[ -e "${DEPLOY_ROOT}/${path}" ]]; then
    install -D -m 0644 \
      "${DEPLOY_ROOT}/${path}" \
      "${rollback_dir}/${path}"
  fi
done

install -m 0644 "${RELEASE_DIR}/compose.yaml" "${DEPLOY_ROOT}/compose.yaml.next"
install -m 0644 "${RELEASE_DIR}/planner.json" "${DEPLOY_ROOT}/config/planner.json.next"
mv -f "${DEPLOY_ROOT}/compose.yaml.next" "${DEPLOY_ROOT}/compose.yaml"
mv -f "${DEPLOY_ROOT}/config/planner.json.next" "${DEPLOY_ROOT}/config/planner.json"
mv -f "${next_env}" "${DEPLOY_ROOT}/.deploy.env"

compose=(
  docker compose
  --file "${DEPLOY_ROOT}/compose.yaml"
  --env-file "${DEPLOY_ROOT}/.env"
  --env-file "${DEPLOY_ROOT}/.deploy.env"
  --profile worker
)

"${compose[@]}" stop api worker || true
if ! "${compose[@]}" up \
  --detach \
  --no-build \
  --remove-orphans \
  --wait \
  --wait-timeout 300; then
  echo "Deployment failed; restoring the previous manifest and image" >&2
  for path in compose.yaml .deploy.env config/planner.json; do
    if [[ -e "${rollback_dir}/${path}" ]]; then
      install -D -m 0644 \
        "${rollback_dir}/${path}" \
        "${DEPLOY_ROOT}/${path}"
    fi
  done
  if [[ -f "${rollback_dir}/compose.yaml" ]] \
    && [[ -f "${rollback_dir}/.deploy.env" ]]; then
    rollback_compose=(
      docker compose
      --file "${DEPLOY_ROOT}/compose.yaml"
      --env-file "${DEPLOY_ROOT}/.env"
      --env-file "${DEPLOY_ROOT}/.deploy.env"
      --profile worker
    )
    "${rollback_compose[@]}" up \
      --detach \
      --no-build \
      --remove-orphans \
      --wait \
      --wait-timeout 300 || true
  fi
  exit 1
fi

curl --fail --silent --show-error \
  http://127.0.0.1:8000/openapi.json \
  >/dev/null
printf '%s\n' "${RELEASE_ID}" >"${DEPLOY_ROOT}/current-release.next"
chmod 0644 "${DEPLOY_ROOT}/current-release.next"
mv -f "${DEPLOY_ROOT}/current-release.next" "${DEPLOY_ROOT}/current-release"
install -m 0750 \
  "${RELEASE_DIR}/deploy-production.sh" \
  "${DEPLOY_ROOT}/deploy-production.sh.next"
install -m 0750 \
  "${RELEASE_DIR}/poll-production.sh" \
  "${DEPLOY_ROOT}/poll-production.sh.next"
mv -f "${DEPLOY_ROOT}/deploy-production.sh.next" "${DEPLOY_ROOT}/deploy-production.sh"
mv -f "${DEPLOY_ROOT}/poll-production.sh.next" "${DEPLOY_ROOT}/poll-production.sh"
docker image prune --all --force --filter 'until=168h' >/dev/null
echo "Deployed ${IMAGE} (${RELEASE_ID})"
