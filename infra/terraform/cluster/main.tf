resource "kind_cluster" "this" {
  name           = var.cluster_name
  wait_for_ready = true
  kind_config    = file("${path.module}/${var.kind_config_path}")
}
