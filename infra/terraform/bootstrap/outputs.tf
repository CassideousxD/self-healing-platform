output "argocd_server_service" {
  description = "ArgoCD server service name"
  value       = "argo-cd-argocd-server"
}

output "argocd_namespace" {
  description = "Namespace where ArgoCD is installed"
  value       = "argocd"
}
