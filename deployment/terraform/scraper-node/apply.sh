#!/usr/bin/env bash
# One-command prototype: create the node VM(s), run the container and wait for /healthz.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${root}"

if [ -z "${TF_CLI_CONFIG_FILE:-}" ]; then
  export TF_CLI_CONFIG_FILE="${root}/terraformrc.yandex"
fi

if command -v yc >/dev/null 2>&1; then
  if [ -z "${YC_TOKEN:-}" ]; then
    YC_TOKEN="$(yc iam create-token)"
    export YC_TOKEN
  fi
  if [ -z "${YC_CLOUD_ID:-}" ]; then
    YC_CLOUD_ID="$(yc config get cloud-id 2>/dev/null || true)"
    export YC_CLOUD_ID
  fi
  if [ -z "${YC_FOLDER_ID:-}" ]; then
    YC_FOLDER_ID="$(yc config get folder-id 2>/dev/null || true)"
    export YC_FOLDER_ID
  fi
fi

: "${YC_TOKEN:?Set YC_TOKEN or authenticate the yc CLI first}"

if [ -z "${TF_VAR_folder_id:-}" ] && [ -n "${YC_FOLDER_ID:-}" ]; then
  export TF_VAR_folder_id="${YC_FOLDER_ID}"
fi
if [ -z "${TF_VAR_folder_id:-}" ] && [ ! -f terraform.tfvars ]; then
  echo "Set TF_VAR_folder_id, YC_FOLDER_ID or create terraform.tfvars." >&2
  exit 1
fi

command -v terraform >/dev/null 2>&1 || {
  echo "terraform is not installed" >&2
  exit 1
}

terraform init -input=false
terraform apply -auto-approve "$@"

print_worker_env() {
  echo
  echo "Worker environment (contains the node token; do not commit it):"
  terraform output -raw worker_env
  echo
}

if [ "${WAIT_FOR_NODE:-1}" = "0" ]; then
  terraform output
  print_worker_env
  exit 0
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is not installed; skipping the health check." >&2
  terraform output
  print_worker_env
  exit 0
fi
if command -v python3 >/dev/null 2>&1; then
  python_bin=python3
elif command -v python >/dev/null 2>&1; then
  python_bin=python
else
  echo "python is not installed; skipping the health check." >&2
  terraform output
  print_worker_env
  exit 0
fi

urls_json="$(terraform output -json node_urls)"
token="$(terraform output -raw node_token)"
while IFS= read -r url; do
  printf 'Waiting for %s/healthz ' "${url}"
  for attempt in $(seq 1 90); do
    if curl --fail --silent --show-error --max-time 5 \
      --header "X-UBAS-Node-Token: ${token}" \
      "${url}/healthz" >/dev/null; then
      echo "ready"
      break
    fi
    if [ "${attempt}" -eq 90 ]; then
      echo
      echo "Node did not become ready within 15 minutes: ${url}" >&2
      echo "Inspect cloud-init: ssh ubuntu@<ip> sudo cloud-init status --long" >&2
      exit 1
    fi
    printf '.'
    sleep 10
  done
done < <("${python_bin}" -c 'import json, sys; print("\n".join(json.load(sys.stdin)))' <<<"${urls_json}")

print_worker_env
