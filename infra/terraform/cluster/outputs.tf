output "cluster_name" {
  description = "The name of the created kind cluster"
  value       = kind_cluster.this.name
}

output "endpoint" {
  description = "Kubernetes API server endpoint"
  value       = kind_cluster.this.endpoint
}

output "client_certificate" {
  description = "Client certificate for Kubernetes authentication"
  value       = kind_cluster.this.client_certificate
  sensitive   = true
}

output "client_key" {
  description = "Client key for Kubernetes authentication"
  value       = kind_cluster.this.client_key
  sensitive   = true
}

output "cluster_ca_certificate" {
  description = "Cluster CA certificate"
  value       = kind_cluster.this.cluster_ca_certificate
  sensitive   = true
}
