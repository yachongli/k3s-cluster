# kube-ovn

![Version: 1.17.0](https://img.shields.io/badge/Version-1.17.0-informational?style=flat-square)

The role performs various tasks related to `kube-ovn` [chart](https://github.com/kubeovn/kube-ovn/tree/v1.17.0/charts/kube-ovn) deployment, reset and validation. Review the [documentation](https://axivo.com/k3s-cluster/wiki/guide/configuration/roles/kube-ovn), for additional details.

## Role Variables

See the related role variables listed below, defined into [main.yaml](./defaults/main.yaml) defaults file. Advanced user role variables are defined into [facts.yaml](./tasks/facts.yaml) `kubeovn_map` collection.

> [!TIP]
> - Use [Renovate](https://axivo.com/k3s-cluster/tutorials/handbook/tools/#renovate), to automate the release pull requests and keep dependencies up-to-date
> - Use [Robusta KRR](https://axivo.com/k3s-cluster/tutorials/handbook/tools/#robusta-krr), to optimize the cluster resources allocation

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| kubeovn_vars.kubernetes.cni.bin_dir | string | `"/opt/cni/bin"` |  |
| kubeovn_vars.kubernetes.cni.conf_file | string | `"/kube-ovn/10-kube-ovn.conflist"` |  |
| kubeovn_vars.kubernetes.cni.config_dir | string | `"/etc/cni/net.d"` |  |
| kubeovn_vars.kubernetes.cni.config_priority | string | `"10"` | 00=multus, 05=cilium, 10=kube-ovn — kube-ovn is ALWAYS additional CNI |
| kubeovn_vars.kubernetes.func.enable_keep_vm_ip | bool | `true` |  |
| kubeovn_vars.kubernetes.func.enable_lb | bool | `true` |  |
| kubeovn_vars.kubernetes.func.enable_nat_gw | bool | `true` |  |
| kubeovn_vars.kubernetes.func.enable_np | bool | `true` |  |
| kubeovn_vars.kubernetes.helm.chart.name | string | `"kube-ovn"` |  |
| kubeovn_vars.kubernetes.helm.chart.version | string | `"v1.17.0"` |  |
| kubeovn_vars.kubernetes.helm.repository.name | string | `"kube-ovn"` |  |
| kubeovn_vars.kubernetes.helm.repository.org | string | `"kubeovn"` |  |
| kubeovn_vars.kubernetes.helm.repository.url | string | `"https://kubeovn.github.io"` |  |
| kubeovn_vars.kubernetes.namespace | string | `"kube-system"` |  |
| kubeovn_vars.kubernetes.networking.join_cidr | string | `"100.64.0.0/16"` |  |
| kubeovn_vars.kubernetes.networking.net_stack | string | `"ipv4"` |  |
| kubeovn_vars.kubernetes.networking.pod_cidr | string | `"10.16.0.0/16"` |  |
| kubeovn_vars.kubernetes.networking.pod_gateway | string | `"10.16.0.1"` |  |
| kubeovn_vars.kubernetes.networking.svc_cidr | string | `"10.96.0.0/12"` |  |
| kubeovn_vars.kubernetes.non_primary_cni | bool | `true` |  |
