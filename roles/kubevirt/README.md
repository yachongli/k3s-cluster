# kubevirt

![Version: 1.8.4](https://img.shields.io/badge/Version-1.8.4-informational?style=flat-square)

The role performs various tasks related to `kubevirt` [cluster](https://github.com/kubevirt/kubevirt/releases/tag/v1.8.4) deployment, reset and validation. Review the [documentation](https://axivo.com/k3s-cluster/wiki/guide/configuration/roles/kubevirt), for additional details.

## Role Variables

See the related role variables listed below, defined into [main.yaml](./defaults/main.yaml) defaults file. Advanced user role variables are defined into [facts.yaml](./tasks/facts.yaml) `kubevirt_map` collection.

> [!TIP]
> - Use [Renovate](https://axivo.com/k3s-cluster/tutorials/handbook/tools/#renovate), to automate the release pull requests and keep dependencies up-to-date
> - Use [Robusta KRR](https://axivo.com/k3s-cluster/tutorials/handbook/tools/#robusta-krr), to optimize the cluster resources allocation

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| kubevirt_vars.kubernetes.configuration.customize_components.patches | list | `[]` |  |
| kubevirt_vars.kubernetes.configuration.developer.feature_gates.enabled | bool | `true` |  |
| kubevirt_vars.kubernetes.configuration.developer.feature_gates.entries | list | `["DownwardMetrics","ExpandDisks"]` | See [documentation](https://kubevirt.io/user-guide/operations/feature_gates/), for details |
| kubevirt_vars.kubernetes.configuration.developer.use_emulation | bool | `false` | Set `true` on hosts without hardware virtualization support (`/dev/kvm` missing), e.g. nested VMs |
| kubevirt_vars.kubernetes.configuration.image_pull_policy | string | `"IfNotPresent"` |  |
| kubevirt_vars.kubernetes.configuration.infra.node_placement.enabled | bool | `false` |  |
| kubevirt_vars.kubernetes.configuration.infra.node_placement.node_selector | object | `{}` |  |
| kubevirt_vars.kubernetes.configuration.infra.node_placement.tolerations | list | `[]` |  |
| kubevirt_vars.kubernetes.configuration.network.default_network_interface | string | `"masquerade"` | Available options are `masquerade`, `bridge` and `slirp` |
| kubevirt_vars.kubernetes.configuration.uninstall_strategy | string | `"BlockUninstallIfWorkloadsExist"` |  |
| kubevirt_vars.kubernetes.configuration.workload_update_strategy.batch_eviction_interval | string | `"1m"` |  |
| kubevirt_vars.kubernetes.configuration.workload_update_strategy.batch_eviction_size | int | `10` |  |
| kubevirt_vars.kubernetes.configuration.workload_update_strategy.methods | list | `["LiveMigrate"]` |  |
| kubevirt_vars.kubernetes.configuration.workloads.node_placement.enabled | bool | `false` |  |
| kubevirt_vars.kubernetes.configuration.workloads.node_placement.node_selector | object | `{}` |  |
| kubevirt_vars.kubernetes.configuration.workloads.node_placement.tolerations | list | `[]` |  |
| kubevirt_vars.kubernetes.monitoring.service_monitor.enabled | bool | `true` |  |
| kubevirt_vars.kubernetes.monitoring.service_monitor.scrape.interval | string | `nil` | If `null`, default value is `victoriametrics_map.service.monitor.scrape.interval` |
| kubevirt_vars.kubernetes.monitoring.service_monitor.scrape.timeout | string | `nil` | If `null`, default value is `victoriametrics_map.service.monitor.scrape.timeout` |
| kubevirt_vars.kubernetes.namespace | string | `"kubevirt"` |  |
| kubevirt_vars.kubernetes.network_policies.enabled | bool | `true` |  |
| kubevirt_vars.release.repository.name | string | `"kubevirt"` |  |
| kubevirt_vars.release.repository.org | string | `"kubevirt"` |  |
| kubevirt_vars.release.version | string | `"v1.8.4"` |  |
