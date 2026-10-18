variable "kubeconfig_path" {
  description = "Path to the kubeconfig file"
  type        = string
  default     = "~/.kube/config"
}

variable "kubeconfig_context" {
  description = "Context within kubeconfig"
  type        = string
  default     = "kind-self-healing"
}

variable "repo_url" {
  description = "Git repository URL for ArgoCD application"
  type        = string
  default     = "https://github.com/CassideousxD/self-healing-platform.git"
}

variable "target_revision" {
  description = "Git revision/branch to track"
  type        = string
  default     = "main"
}
