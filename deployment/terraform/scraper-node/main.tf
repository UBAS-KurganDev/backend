provider "yandex" {
  cloud_id  = var.cloud_id
  folder_id = var.folder_id
  zone      = var.zone
}

locals {
  node_names = [
    for index in range(var.node_count) : format("%s-%02d", var.name, index + 1)
  ]
  registry_host = split("/", var.image)[0]
  node_token = (
    var.node_token != null && var.node_token != ""
    ? var.node_token
    : random_password.node_token.result
  )
  node_urls = [
    for instance in yandex_compute_instance.node :
    "http://${instance.network_interface[0].nat_ip_address}:${var.node_port}"
  ]
  cloud_init = [
    for name in local.node_names : templatefile(
      "${path.module}/cloud-init.yaml.tftpl",
      {
        container_name    = name
        image             = var.image
        node_concurrency  = var.node_concurrency
        node_max_attempts = var.node_max_attempts
        node_port         = var.node_port
        node_timeout      = var.node_timeout_seconds
        node_token        = local.node_token
        registry_host     = local.registry_host
      }
    )
  ]
}

resource "random_password" "node_token" {
  length  = 48
  special = false
}

resource "yandex_iam_service_account" "node" {
  name = "${var.name}-vm"
}

resource "yandex_resourcemanager_folder_iam_member" "registry_pull" {
  folder_id = var.folder_id
  role      = "container-registry.images.puller"
  member    = "serviceAccount:${yandex_iam_service_account.node.id}"
}

resource "yandex_vpc_network" "node" {
  name = "${var.name}-net"
}

resource "yandex_vpc_subnet" "node" {
  name           = "${var.name}-subnet"
  zone           = var.zone
  network_id     = yandex_vpc_network.node.id
  v4_cidr_blocks = [var.subnet_cidr]
}

resource "yandex_vpc_security_group" "node" {
  name       = "${var.name}-sg"
  network_id = yandex_vpc_network.node.id

  ingress {
    description    = "Scraper node API"
    protocol       = "TCP"
    port           = var.node_port
    v4_cidr_blocks = var.node_allowed_cidrs
  }

  dynamic "ingress" {
    for_each = length(var.ssh_allowed_cidrs) > 0 ? [1] : []
    content {
      description    = "SSH administration"
      protocol       = "TCP"
      port           = 22
      v4_cidr_blocks = var.ssh_allowed_cidrs
    }
  }

  egress {
    description    = "Outbound internet for Docker, registry and agent.ru"
    protocol       = "ANY"
    v4_cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "yandex_compute_instance" "node" {
  count                     = var.node_count
  name                      = local.node_names[count.index]
  zone                      = var.zone
  platform_id               = var.platform_id
  service_account_id        = yandex_iam_service_account.node.id
  allow_stopping_for_update = true

  resources {
    cores         = var.cores
    memory        = var.memory_gb
    core_fraction = var.core_fraction
  }

  boot_disk {
    initialize_params {
      image_id = data.yandex_compute_image.ubuntu.image_id
      type     = var.disk_type
      size     = var.disk_size_gb
    }
  }

  network_interface {
    subnet_id          = yandex_vpc_subnet.node.id
    nat                = true
    security_group_ids = [yandex_vpc_security_group.node.id]
  }

  metadata = merge(
    { "user-data" = local.cloud_init[count.index] },
    var.ssh_public_key == "" ? {} : {
      "ssh-keys" = "ubuntu:${var.ssh_public_key}"
    },
  )

  scheduling_policy {
    preemptible = var.preemptible
  }

  depends_on = [yandex_resourcemanager_folder_iam_member.registry_pull]
}

data "yandex_compute_image" "ubuntu" {
  family = var.image_family
}
