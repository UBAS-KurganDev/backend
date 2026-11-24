#!/usr/bin/env bash
# Destroy the Terraform-managed scraper node resources.
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

terraform init -input=false
terraform destroy -auto-approve "$@"
