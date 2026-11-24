output "node_names" {
  description = "VM and Docker container names."
  value       = yandex_compute_instance.node[*].name
}

output "node_public_ips" {
  description = "Public IPv4 addresses of the scraper nodes."
  value       = yandex_compute_instance.node[*].network_interface[0].nat_ip_address
}

output "node_internal_ips" {
  description = "Private IPv4 addresses for a worker in the same VPC."
  value       = yandex_compute_instance.node[*].network_interface[0].ip_address
}

output "node_internal_urls" {
  description = "Private node origins for UBAS_AGENT_NODE_URLS inside the VPC."
  value = [
    for instance in yandex_compute_instance.node :
    "http://${instance.network_interface[0].ip_address}:${var.node_port}"
  ]
}

output "node_urls" {
  description = "Public node API origins, ready for UBAS_AGENT_NODE_URLS."
  value       = local.node_urls
}

output "healthcheck_urls" {
  description = "GET /healthz endpoints."
  value       = [for url in local.node_urls : "${url}/healthz"]
}

output "node_token" {
  description = "Shared token for the X-UBAS-Node-Token header."
  value       = local.node_token
  sensitive   = true
}

output "worker_env" {
  description = "Worker environment fragment for all created nodes."
  value = join(
    "\n",
    [
      "UBAS_AGENT_NODE_URLS=${join(",", local.node_urls)}",
      "UBAS_AGENT_NODE_CONCURRENCY=${var.node_concurrency}",
      "UBAS_AGENT_NODE_TOKEN=${local.node_token}",
    ],
  )
  sensitive = true
}
