resource "helm_release" "metrics_server" {
  name             = "metrics-server"
  repository       = "https://kubernetes-sigs.github.io/metrics-server/"
  chart            = "metrics-server"
  version          = "3.12.2"
  namespace        = "kube-system"
  cleanup_on_fail  = true

  values = [
    yamlencode({
      args = [
        "--kubelet-insecure-tls"
      ]
    })
  ]
}

resource "helm_release" "argocd" {
  name             = "argo-cd"
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-cd"
  version          = "7.8.9"
  namespace        = "argocd"
  create_namespace = true
  cleanup_on_fail  = true

  values = [
    yamlencode({
      server = {
        extraArgs = ["--insecure"]
      }
      configs = {
        cm = {
          "timeout.reconciliation" = "30s"
        }
      }
    })
  ]
}

resource "helm_release" "argocd_apps" {
  name            = "argocd-apps"
  repository      = "https://argoproj.github.io/argo-helm"
  chart           = "argocd-apps"
  version         = "2.0.2"
  namespace       = "argocd"
  cleanup_on_fail = true
  depends_on      = [helm_release.argocd]

  values = [
    yamlencode({
      applications = [
        {
          name      = "platform"
          namespace = "argocd"
          project   = "default"
          source = {
            repoURL        = var.repo_url
            targetRevision = var.target_revision
            path           = "deploy/overlays/dev"
          }
          destination = {
            server    = "https://kubernetes.default.svc"
            namespace = "platform"
          }
          syncPolicy = {
            automated = {
              prune    = true
              selfHeal = true
            }
            syncOptions = [
              "CreateNamespace=true",
              "RespectIgnoreDifferences=true"
            ]
          }
          ignoreDifferences = [
            {
              group = "apps"
              kind  = "Deployment"
              jsonPointers = [
                "/spec/replicas"
              ]
            }
          ]
        }
      ]
    })
  ]
}
