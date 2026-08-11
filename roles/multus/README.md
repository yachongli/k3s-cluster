# multus

![Version: 4.3.0](https://img.shields.io/badge/Version-4.3.0-informational?style=flat-square)

The role performs various tasks related to `multus` [cluster](https://github.com/k8snetworkplumbingwg/multus-cni/releases/tag/v4.3.0) deployment, reset and validation. Review the [documentation](https://axivo.com/k3s-cluster/wiki/guide/configuration/roles/multus), for additional details.

## Role Variables

See the related role variables listed below, defined into [main.yaml](./defaults/main.yaml) defaults file. Advanced user role variables are defined into [facts.yaml](./tasks/facts.yaml) `multus_map` collection.

> [!TIP]
> - Use [Renovate](https://axivo.com/k3s-cluster/tutorials/handbook/tools/#renovate), to automate the release pull requests and keep dependencies up-to-date
> - Use [Robusta KRR](https://axivo.com/k3s-cluster/tutorials/handbook/tools/#robusta-krr), to optimize the cluster resources allocation

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| multus_vars.kubernetes.cni_config_file | string | `"00-multus.conf"` |  |
| multus_vars.kubernetes.image.repository | string | `"ghcr.io/k8snetworkplumbingwg/multus-cni"` |  |
| multus_vars.kubernetes.image.tag | string | `"v4.3.0"` |  |
| multus_vars.kubernetes.namespace | string | `"kube-system"` |  |
| multus_vars.kubernetes.resources.limits.cpu | string | `"100m"` |  |
| multus_vars.kubernetes.resources.limits.memory | string | `"50Mi"` |  |
| multus_vars.kubernetes.resources.requests.cpu | string | `"100m"` |  |
| multus_vars.kubernetes.resources.requests.memory | string | `"50Mi"` |  |
| multus_vars.kubernetes.tolerations | list | `[]` |  |
