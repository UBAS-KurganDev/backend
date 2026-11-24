terraform {
  required_version = ">= 1.9.0"

  required_providers {
    random = {
      source  = "registry.terraform.io/hashicorp/random"
      version = "3.9.1"
    }
    yandex = {
      source  = "registry.terraform.io/yandex-cloud/yandex"
      version = "0.250.0"
    }
  }
}
