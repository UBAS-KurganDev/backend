# Scraper node VM in Yandex Cloud

Terraform root module for one or more independent `ubas-scraper-node` VMs. It
creates a dedicated VPC, subnet, security group, service account and VM, installs
Docker through cloud-init, pulls the backend image from Yandex Container Registry
and starts the node container. The same module is used by the worker through
`UBAS_AGENT_NODE_URLS`.

This is a prototype and is not connected to the production API/worker VM: it does
not modify `ubas-backend`, its network, its disk or PostgreSQL.

## One command

Prerequisites:

- Terraform >= 1.9;
- an authenticated `yc` CLI or `YC_TOKEN` with permissions to create compute,
  VPC, IAM and service-account resources in the folder;
- the backend image is already pushed to Yandex Container Registry by
  [deploy.yml](../../../.github/workflows/deploy.yml).

`apply.sh` uses `terraformrc.yandex` and the Yandex mirror of
`registry.terraform.io` unless `TF_CLI_CONFIG_FILE` is already set. Override it
for a different provider mirror.

From the repository root:

```shell
./deployment/terraform/scraper-node/apply.sh
```

The script obtains `YC_TOKEN` and the folder ID from `yc` when possible, runs
`terraform init`/`apply`, then waits up to 15 minutes for every `/healthz`. The
final output contains a ready-to-use worker environment fragment.

If `yc` is not installed, create an IAM token elsewhere and export it together
with the folder:

```shell
export YC_TOKEN="<iam-token>"
export TF_VAR_folder_id="<folder-id>"
./deployment/terraform/scraper-node/apply.sh
```

`-auto-approve` is intentional: the module creates new resources only. Read the
plan before the first production-like run. Disable the post-apply wait with
`WAIT_FOR_NODE=0`.

## Configuration

Copy `terraform.tfvars.example` to `terraform.tfvars` and edit it, or use
`TF_VAR_*` variables. `terraform.tfvars` is ignored by Git.

| Variable | Default | Meaning |
| --- | --- | --- |
| `folder_id` | required | Folder for the VM, network and service account |
| `cloud_id` | `null` | Falls back to `YC_CLOUD_ID` |
| `zone` | `ru-central1-a` | VM and subnet zone |
| `name` | `ubas-scraper-node` | Resource name prefix |
| `node_count` | `1` | Number of node VMs |
| `image` | `.../ubas-backend:master` | Container image with the node script |
| `node_port` | `8101` | Node HTTP port |
| `node_concurrency` | `2` | Browser slots per node |
| `node_allowed_cidrs` | `["0.0.0.0/0"]` | Who may call the node API |
| `ssh_allowed_cidrs` | `[]` | Who may use SSH |
| `ssh_public_key` | `""` | Optional key for the `ubuntu` user |
| `cores` / `memory_gb` | `2` / `4` | Chromium capacity |
| `preemptible` | `false` | Cheaper, but searches can be interrupted |

Two nodes in one command:

```shell
TF_VAR_node_count=2 ./deployment/terraform/scraper-node/apply.sh
```

or in `terraform.tfvars`:

```hcl
folder_id  = "<folder-id>"
node_count = 2
```

## Outputs

- `node_urls` — list for `UBAS_AGENT_NODE_URLS`;
- `healthcheck_urls` — `/healthz` endpoints;
- `node_public_ips` — VM public addresses;
- `node_internal_ips` / `node_internal_urls` — private VPC addresses;
- `node_token` — sensitive shared token for `X-UBAS-Node-Token`;
- `worker_env` — sensitive environment fragment.

Put the fragment into the worker environment and add
`UBAS_AGENT_NODE_CONCURRENCY` matching the module value. The node URL uses the
public IP; a worker inside the same VPC should use internal addresses instead.

## Security and lifecycle

- The default `node_allowed_cidrs` is open to the internet. The generated token
  protects the endpoint, but restrict the CIDR to the worker egress network.
  If you narrow it, include the machine running `apply.sh` as well, otherwise the
  post-apply health check cannot reach the node; use `WAIT_FOR_NODE=0` instead.
- The token is stored in Terraform state and in VM metadata/user-data. Use an
  encrypted remote backend for shared state and do not commit state files.
- The VM service account gets only `container-registry.images.puller`; no
  permanent IAM token is written to disk.
- Cloud-init runs once. Changing `image` does not automatically rerun it. To
  update an existing node, replace the VM:

  ```shell
  terraform apply -replace='yandex_compute_instance.node[0]'
  ```

- Remove everything created by the module:

  ```shell
  ./deployment/terraform/scraper-node/destroy.sh
  ```

  or `terraform destroy` with the same authentication and variables.

- The module does not configure TLS or mTLS. Keep the node in a private network
  or behind a restricted security group for anything beyond the prototype.

## Troubleshooting

Cloud-init status and node logs:

```shell
ssh ubuntu@<public-ip> sudo cloud-init status --long
ssh ubuntu@<public-ip> sudo journalctl -u cloud-init --no-pager
ssh ubuntu@<public-ip> sudo docker logs ubas-scraper-node-01
```

Health check from the Terraform host:

```shell
curl -H "X-UBAS-Node-Token: <token>" http://<public-ip>:8101/healthz
```

If `apply.sh` reports that the node is not ready, check that the image pull
succeeded and that the VM can reach `agent.ru` and the Yandex registry.
