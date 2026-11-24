variable "folder_id" {
  description = "Yandex Cloud folder where the node VM, network and service account are created."
  type        = string

  validation {
    condition     = length(trimspace(var.folder_id)) > 0
    error_message = "folder_id must not be empty."
  }
}

variable "cloud_id" {
  description = "Optional Yandex Cloud ID. Falls back to YC_CLOUD_ID when null."
  type        = string
  default     = null
}

variable "zone" {
  description = "Availability zone for the VM and subnet."
  type        = string
  default     = "ru-central1-a"
}

variable "name" {
  description = "Prefix for the VM, network, subnet and service account names."
  type        = string
  default     = "ubas-scraper-node"
}

variable "node_count" {
  description = "Number of independent scraper node VMs to create."
  type        = number
  default     = 1

  validation {
    condition     = var.node_count >= 1 && var.node_count <= 10
    error_message = "node_count must be between 1 and 10."
  }
}

variable "image" {
  description = "Container image with the ubas-scraper-node console script."
  type        = string
  default     = "registry.yandexcloud.net/cn12qrbnsnck5l6a3en0/ubas-backend:master"

  validation {
    condition     = length(trimspace(var.image)) > 0
    error_message = "image must not be empty."
  }
}

variable "image_family" {
  description = "Public Yandex Cloud image family used as the VM boot image."
  type        = string
  default     = "ubuntu-2404-lts"
}

variable "platform_id" {
  description = "Yandex Cloud compute platform."
  type        = string
  default     = "standard-v3"
}

variable "cores" {
  description = "vCPU count. Chromium with two browser slots needs at least two."
  type        = number
  default     = 2
}

variable "memory_gb" {
  description = "RAM in GiB. Chromium nodes should not use less than four."
  type        = number
  default     = 4
}

variable "core_fraction" {
  description = "Guaranteed vCPU share, percent."
  type        = number
  default     = 100
}

variable "disk_size_gb" {
  description = "Boot disk size in GiB."
  type        = number
  default     = 20
}

variable "disk_type" {
  description = "Boot disk type: network-ssd or network-hdd."
  type        = string
  default     = "network-ssd"
}

variable "preemptible" {
  description = "Use a preemptible VM. Cheaper, but searches can be interrupted."
  type        = bool
  default     = false
}

variable "subnet_cidr" {
  description = "IPv4 CIDR for the dedicated VPC subnet."
  type        = string
  default     = "10.20.0.0/24"
}

variable "node_port" {
  description = "Port the node HTTP API listens on."
  type        = number
  default     = 8101

  validation {
    condition     = var.node_port >= 1 && var.node_port <= 65535
    error_message = "node_port must be a valid TCP port."
  }
}

variable "node_concurrency" {
  description = "Browser slots per node, passed as UBAS_AGENT_MAX_CONCURRENCY."
  type        = number
  default     = 2

  validation {
    condition     = var.node_concurrency >= 1 && var.node_concurrency <= 16
    error_message = "node_concurrency must be between 1 and 16."
  }
}

variable "node_timeout_seconds" {
  description = "Per-capture timeout inside the node."
  type        = number
  default     = 30
}

variable "node_max_attempts" {
  description = "Source retry attempts inside the node."
  type        = number
  default     = 3
}

variable "node_token" {
  description = "Optional shared node token. Generated when null."
  type        = string
  default     = null
  sensitive   = true

  validation {
    condition = (
      var.node_token == null ||
      can(regex("^[A-Za-z0-9_-]{16,128}$", var.node_token))
    )
    error_message = "node_token must be null or 16-128 characters [A-Za-z0-9_-]."
  }
}

variable "node_allowed_cidrs" {
  description = "CIDRs allowed to call the node API. Restrict this to the worker network."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "ssh_allowed_cidrs" {
  description = "CIDRs allowed to connect over SSH. Empty disables the SSH rule."
  type        = list(string)
  default     = []
}

variable "ssh_public_key" {
  description = "Optional OpenSSH public key for the ubuntu user."
  type        = string
  default     = ""
}
