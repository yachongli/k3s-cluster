# gpu-operator

![Version: 26.3.3](https://img.shields.io/badge/Version-26.3.3-informational?style=flat-square)

The role performs various tasks related to `gpu-operator` [chart](https://github.com/NVIDIA/gpu-operator/tree/v26.3.3/deployments/gpu-operator) deployment, reset and validation. Review the [documentation](https://axivo.com/k3s-cluster/wiki/guide/configuration/roles/gpu-operator), for additional details.

## Role Variables

See the related role variables listed below, defined into [main.yaml](./defaults/main.yaml) defaults file. Advanced user role variables are defined into [facts.yaml](./tasks/facts.yaml) `gpuoperator_map` collection.

> [!TIP]
> - Use [Renovate](https://axivo.com/k3s-cluster/tutorials/handbook/tools/#renovate), to automate the release pull requests and keep dependencies up-to-date
> - Use [Robusta KRR](https://axivo.com/k3s-cluster/tutorials/handbook/tools/#robusta-krr), to optimize the cluster resources allocation

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| gpuoperator_vars.kubernetes.cdi.enabled | bool | `true` | Container Device Interface (required for KubeVirt GPU passthrough) |
| gpuoperator_vars.kubernetes.dcgm_exporter.enabled | bool | `true` | DCGM exporter (GPU metrics for VictoriaMetrics) |
| gpuoperator_vars.kubernetes.driver.enabled | bool | `true` | Install NVIDIA driver via container |
| gpuoperator_vars.kubernetes.helm.chart.name | string | `"gpu-operator"` |  |
| gpuoperator_vars.kubernetes.helm.chart.version | string | `"v26.3.3"` |  |
| gpuoperator_vars.kubernetes.helm.repository.name | string | `"gpu-operator"` |  |
| gpuoperator_vars.kubernetes.helm.repository.org | string | `"nvidia"` |  |
| gpuoperator_vars.kubernetes.helm.repository.url | string | `"https://nvidia.github.io/gpu-operator"` |  |
| gpuoperator_vars.kubernetes.mig.enabled | bool | `false` | Multi-Instance GPU |
| gpuoperator_vars.kubernetes.mig.strategy | string | `"single"` |  |
| gpuoperator_vars.kubernetes.namespace | string | `"gpu-operator"` |  |
| gpuoperator_vars.kubernetes.nfd.enabled | bool | `true` | Node Feature Discovery |
| gpuoperator_vars.kubernetes.nfd.node_feature_rules | bool | `false` |  |
| gpuoperator_vars.kubernetes.sandbox_workloads.enabled | bool | `false` | GPU passthrough to KubeVirt VMs |
| gpuoperator_vars.kubernetes.sandbox_workloads.mode | string | `"kubevirt"` | kubevirt or kata |
| gpuoperator_vars.kubernetes.time_slicing.enabled | bool | `false` | GPU time-slicing |
| gpuoperator_vars.kubernetes.time_slicing.replicas | int | `1` |  |
