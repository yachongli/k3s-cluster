# ceph-csi

![Version: 1.0.4](https://img.shields.io/badge/Version-1.0.4-informational?style=flat-square)

The role performs various tasks related to `ceph-csi` [chart](https://github.com/ceph/ceph-csi-operator/tree/v1.0.4/deploy/charts/ceph-csi-drivers) deployment, reset and validation. Review the [documentation](https://axivo.com/k3s-cluster/wiki/guide/configuration/roles/ceph-csi), for additional details.

## Role Variables

See the related role variables listed below, defined into [main.yaml](./defaults/main.yaml) defaults file. Advanced user role variables are defined into [facts.yaml](./tasks/facts.yaml) `cephcsi_map` collection.

> [!TIP]
> - Use [Renovate](https://axivo.com/k3s-cluster/tutorials/handbook/tools/#renovate), to automate the release pull requests and keep dependencies up-to-date
> - Use [Robusta KRR](https://axivo.com/k3s-cluster/tutorials/handbook/tools/#robusta-krr), to optimize the cluster resources allocation

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| cephcsi_vars.kubernetes.ceph.cephfs_fsname | string | `""` | CephFS filesystem name (empty = disabled) |
| cephcsi_vars.kubernetes.ceph.connection_name | string | `"external"` | Name for the Ceph connection CR |
| cephcsi_vars.kubernetes.ceph.monitors | list | `[]` | Ceph monitor addresses (IP:port) |
| cephcsi_vars.kubernetes.ceph.rbd_pool | string | `""` | RBD pool name for block storage |
| cephcsi_vars.kubernetes.client_key_secret | string | `"ceph-admin-key"` | K8s Secret name holding cephx client key |
| cephcsi_vars.kubernetes.drivers.cephfs.enabled | bool | `false` | Enable CephFS driver |
| cephcsi_vars.kubernetes.drivers.rbd.enabled | bool | `true` | Enable RBD driver |
| cephcsi_vars.kubernetes.helm.chart.name | string | `"ceph-csi-drivers"` |  |
| cephcsi_vars.kubernetes.helm.chart.version | string | `"1.0.4"` |  |
| cephcsi_vars.kubernetes.helm.repository.name | string | `"ceph-csi-operator"` |  |
| cephcsi_vars.kubernetes.helm.repository.org | string | `"ceph"` |  |
| cephcsi_vars.kubernetes.helm.repository.url | string | `"https://ceph.github.io/ceph-csi-operator"` |  |
| cephcsi_vars.kubernetes.namespace | string | `"ceph-csi"` |  |
| cephcsi_vars.kubernetes.storage_class.cephfs | string | `"ceph-cephfs"` | StorageClass name for CephFS |
| cephcsi_vars.kubernetes.storage_class.rbd | string | `"ceph-rbd"` | StorageClass name for RBD |
