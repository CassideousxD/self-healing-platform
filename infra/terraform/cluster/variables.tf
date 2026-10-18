variable "cluster_name" {
  description = "Name of the kind cluster"
  type        = string
  default     = "self-healing"
}

variable "kind_config_path" {
  description = "Relative path from module to kind-config.yaml"
  type        = string
  default     = "../../kind-config.yaml"
}
