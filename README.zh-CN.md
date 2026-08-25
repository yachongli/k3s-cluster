# K3s Cluster

[English](README.md) | **简体中文**

一个受 [kolla-ansible](https://opendev.org/openstack/kolla-ansible) 启发的 K3s
集群部署框架。Fork 自 [axivo/k3s-cluster](https://github.com/axivo/k3s-cluster)，
在其基础上扩展了多 CNI（Multus + Kube-OVN）、KubeVirt、NVIDIA GPU Operator、
外部 Ceph CSI、明文优先的密码管理，以及云环境底层支持（OpenStack、代理、
Registry 镜像加速）。

## 功能一览

| 层次 | 组件 |
|------|------|
| **操作系统** | Ubuntu LTS、防火墙、用户、可选 postfix / unattended-upgrades |
| **控制平面** | K3s（内嵌 etcd）+ HAProxy + Keepalived VIP |
| **CNI** | Cilium（eBPF、替代 kube-proxy、Gateway API、Hubble）；可选 Multus + Kube-OVN 作为次级 CNI |
| **DNS** | CoreDNS（集群内）、ExternalDNS（Cloudflare） |
| **TLS** | cert-manager + ACME / Let's Encrypt |
| **存储** | Longhorn（内置）、Ceph CSI（对接外部集群） |
| **可观测性** | metrics-server、VictoriaMetrics + Grafana + AlertManager、VictoriaLogs + Vector |
| **应用管理** | ArgoCD、Kured |
| **计算** | KubeVirt、NVIDIA GPU Operator（含 KubeVirt 直通） |

除 `cluster`、`helm`、`k3s` 三个基础角色外，其余组件均以 Helm chart 交付。
每个组件通过 `globals.yaml` 中一个 `enable_<component>: true|false` 开关控制。

## 目录结构

```
.
├── provisioning.yaml     # 完整部署（cluster + k3s + charts）
├── validation.yaml       # 部署前 & 分角色校验
├── upgrade.yaml          # 单角色升级（按 tag 触发）
├── reset.yaml            # 拆除
├── vault.yaml            # 交互式 vault 助手（列出/加密/更新）
├── tools/
│   ├── secrets.py        # 明文优先密码工具（init/edit/list/decrypt）
│   └── helm-repo.sh
├── inventory/cluster/
│   ├── hosts-sample.yaml
│   └── group_vars/all/
│       ├── main.yaml            # 仓库基线（defaults + global_map + tags）
│       ├── globals-sample.yaml  # 复制为 globals.yaml（唯一需要改的文件）
│       └── passwords.yaml       # Vault 加密备份（可入 Git）
├── roles/                       # 18 个角色，目录结构统一
└── docs/
    ├── architecture.md           /  architecture.zh-CN.md
    ├── configuration.md          /  configuration.zh-CN.md
    ├── globals-overrides.md
    ├── kubelet-numa-research.md
    ├── kubelet-tuning.md
    ├── longhorn-storage.md
    ├── openstack.md
    └── victoria-metrics.md
```

## 环境要求

- **控制机**：Python 3.8+、Ansible 2.19+、`ansible-vault`、`kubernetes` PyPI 模块
- **目标节点**：Ubuntu LTS、SSH key 登录、免密 sudo（否则需设置 `ansible_become_password`）
- 可选：NVIDIA GPU（用于 `gpu-operator`）、外部 Ceph（用于 `ceph-csi`）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install ansible kubernetes
ansible-galaxy collection install -r collections/requirements.yaml
```

## 配置

### 1. Inventory

```bash
cp inventory/cluster/hosts-sample.yaml inventory/cluster/hosts.yaml
```

```yaml
server:                # 控制平面（单节点 1 台，HA 3 台）
  hosts:
    node1:
    node2:
    node3:
agent:                 # 工作节点
  hosts:
    node4:
    node5:
cluster:
  children:
    server:
    agent:
```

### 2. Globals

`globals.yaml` 是**唯一需要修改的配置文件**。所有默认值都定义在
`inventory/cluster/group_vars/all/main.yaml` 的 `defaults:` 下，
只需在此文件取消对应扁平变量的注释即可覆盖。

```bash
cp inventory/cluster/group_vars/all/globals-sample.yaml \
   inventory/cluster/group_vars/all/globals.yaml
```

最小覆盖示例：

```yaml
cluster_api_host: "192.168.4.10"          # K3s API VIP

# 组件开关（下方展示的是默认值）
enable_cilium: true
enable_multus: false
enable_kube_ovn: false
enable_coredns: true
enable_cert_manager: true
enable_external_dns: false
enable_longhorn: true
enable_ceph_csi: false
enable_metrics_server: true
enable_victoria_logs: true
enable_victoria_metrics: true
enable_argo_cd: false
enable_kured: false
enable_kubevirt: false
enable_gpu_operator: false

# Cilium LoadBalancer 地址池（需属于所在 LAN）
cilium_lb_ip_start: "192.168.4.20"
cilium_lb_ip_stop:  "192.168.4.100"
```

按组件分组、包含全部可调参数（版本、chart tag、调优项）的完整清单见
[`globals-sample.yaml`](inventory/cluster/group_vars/all/globals-sample.yaml)。

### 3. 密码 / 凭据

两个文件，单向覆盖 —— 明文优先：

```
/etc/k3s-cluster/passwords.yaml          ← 明文，唯一可信源
        ↓ Playbook 启动时自动加载
inventory/cluster/group_vars/all/passwords.yaml   ← Vault 加密备份（可入 Git）
```

如果明文文件存在，其值会覆盖 vault 加密版，无需手动同步。

```bash
python3 tools/secrets.py init      # 生成 UUID 内部密码 + 外部凭据占位
python3 tools/secrets.py edit      # $EDITOR 填入 Cloudflare/Ceph/Slack/…
python3 tools/secrets.py list      # 状态：READY | EMPTY | PENDING | MISSING
python3 tools/secrets.py decrypt   # 从 vault 还原明文
```

**工具管理的凭据清单：**

| 类型 | 变量 | 组件 |
|------|------|------|
| 内部（UUID 自动生成） | `password_argocd_admin`、`password_argocd_user`、`password_grafana_admin` | ArgoCD、Grafana |
| 外部 | `credential_cloudflare_api_token` | ExternalDNS |
| 外部 | `credential_ceph_admin_key` | Ceph CSI |
| 外部 | `credential_longhorn_backup_password` | Longhorn NAS 备份 |
| 外部 | `credential_slack_webhook_url` | Kured Slack |
| 外部 | `credential_postfix_alias/name/password` | Postfix（iCloud） |

外部凭据留空时，工具会**自动关闭**对应组件（改写 `globals.yaml` 或 role
defaults），不会因缺 key 而部署失败。

SSH 认证凭据（`ansible_user`、`ansible_password`、`ansible_become_password`）
**不**在此工具管理范围内。默认使用 SSH key + 免密 sudo。

## 部署

```bash
ansible-playbook validation.yaml         # 部署前分角色校验
ansible-playbook provisioning.yaml       # 完整部署

# 按阶段
ansible-playbook provisioning.yaml -t cluster       # 操作系统 + 防火墙 + 内核模块 + 代理
ansible-playbook provisioning.yaml -t kubernetes    # K3s + HAProxy + Helm + kubelet 配置
ansible-playbook provisioning.yaml -t charts        # 所有已启用组件

# 按组件（一个 tag 对应一个 role）
ansible-playbook provisioning.yaml -t cilium
ansible-playbook provisioning.yaml -t kubevirt
```

可用组件 tag：`cilium multus kube-ovn coredns cert-manager external-dns argo-cd kured longhorn ceph-csi metrics-server victoria-logs victoria-metrics kubevirt gpu-operator`

K3s 采用滚动批次启动（`serial: 1 → 2 → 5`），保证第一个 server 先完成 etcd
选举形成 quorum，其余节点再顺序加入。

### Tag 速查

`provisioning.yaml`（play 级 tag）与 `upgrade.yaml` / `validation.yaml` /
`reset.yaml`（role 级 tag）的 tag 体系不同。

#### provisioning.yaml — 阶段 tag

| Tag | 覆盖的 play | 配置内容 |
|-----|------------|---------|
| `cluster` | Cluster Provisioning + LoadBalancer | OS 层：防火墙、用户、内核模块、sysctl、持久化代理、postfix、自动更新 |
| `kubernetes` | LoadBalancer + Kubernetes Provisioning | HAProxy + Keepalived VIP、Helm CLI + 插件、K3s server/agent、**kubelet 配置**（`kubelet-arg`）、containerd 镜像加速 |
| `charts` | Charts Provisioning | 所有已启用的 Helm chart（一次性全跑） |
| `<component>` | Charts Provisioning | 单个 Helm chart（受 `enable_<component>` 过滤） |

组件 tag（15 个）：

```
cilium  multus  kube-ovn  coredns  cert-manager  external-dns
argo-cd  kured  longhorn  ceph-csi  metrics-server
victoria-logs  victoria-metrics  kubevirt  gpu-operator
```

#### upgrade.yaml / validation.yaml / reset.yaml — 角色 tag

这三个 playbook 遍历 `global_map.tags.role`，每个 tag 对应一个 role 的
`upgrade` / `validation` / `reset` 任务文件：

```
argo-cd  ceph-csi  cert-manager  cilium  cluster  coredns
external-dns  gpu-operator  helm  k3s  kube-ovn  kubevirt
kured  longhorn  metrics-server  multus  victoria-logs  victoria-metrics
```

> **关键区别：** `provisioning.yaml` 用 `kubernetes`（helm + k3s 合在一起）；
> `upgrade.yaml` / `validation.yaml` 用 `helm` 和 `k3s` 分开跑。`cluster`
> tag 在 provisioning 中表示 "OS 层"，在 upgrade/validation 中表示
> "cluster 角色"。

#### 改了配置该跑哪个 tag？

| 我改了… | Playbook | Tag |
|---------|----------|-----|
| `kubevirt_kubelet_override` / `pod_kubelet_override` | `provisioning.yaml` | `kubernetes` |
| `cluster_http_proxy` / OS 级代理 | `provisioning.yaml` | `cluster` |
| `cilium_lb_ip_start` / chart 值 | `provisioning.yaml` | `<component>` |
| `k3s_version` | `upgrade.yaml` | `k3s` |
| `helm_version` / `helm_diff_plugin_version` | `upgrade.yaml` | `helm` |
| 组件 chart 版本 | `upgrade.yaml` | `<component>` |
| OS 级设置（postfix、自动更新） | `upgrade.yaml` | `cluster` |

> **`--limit` 注意事项：** 用 `-t kubernetes --limit` 时，必须包含一个
> server 节点（如 `--limit node1,node2`）。kubelet precheck 会委托 server
> 节点查询 API，检测是否有运行中的 KubeVirt VM。

## 升级与重置

```bash
# 单组件升级（必须带 --tags）
ansible-playbook upgrade.yaml -t cilium
ansible-playbook upgrade.yaml -t k3s

# 单组件重置
ansible-playbook reset.yaml -t kubevirt

# 完整拆除（会询问是否卸载 apt 包）
ansible-playbook reset.yaml
```

## 常见场景

### 多 CNI（Cilium 主 + Kube-OVN 通过 Multus 挂载）

```yaml
enable_multus: true
enable_kube_ovn: true
```

节点上 CNI 配置文件顺序：
`00-multus.conf` → `05-cilium.conflist` → `10-kube-ovn.conflist`

Kube-OVN 运行在 **non-primary** 模式：Pod 默认网络仍是 Cilium，Kube-OVN
通过 `NetworkAttachmentDefinition` 作为额外网卡挂入。

### KubeVirt + GPU 直通

```yaml
enable_kubevirt: true
enable_gpu_operator: true
gpu_operator_sandbox_workloads_enabled: true
gpu_operator_cdi_enabled: true
cluster_kubelet_kubevirt_profile: true    # 静态 CPU + NUMA 拓扑
```

会启用 kubelet `cpuManagerPolicy=static`、
`topologyManagerPolicy=restricted`、`distribute-cpus-across-numa`。

### 对接外部 Ceph

```yaml
enable_ceph_csi: true
ceph_csi_monitors: ["10.0.0.1:6789", "10.0.0.2:6789"]
ceph_csi_client_user: admin
ceph_csi_rbd_pool: rbd-pool
ceph_csi_cephfs_enabled: false
```

然后：
```bash
# 在 Ceph 节点执行：ceph auth get-key client.admin
python3 tools/secrets.py edit         # 填入 credential_ceph_admin_key
```

### Registry 镜像加速（离线 / 上游慢）

```yaml
cluster_registry_endpoint: "http://harbor.internal:4000"
```

会将 `docker.io`、`gcr.io`、`ghcr.io`、`quay.io`、`registry.k8s.io`、
`nvcr.io` 全部走该端点。

### 代理

两种模式，可以只用一种，也可以同时启用：

```yaml
# 仅 Ansible 执行期间（helm/git/get_url），不写入主机
cluster_deploy_proxy: "http://proxy.example.com:3128"
cluster_apt_proxy: false                # true 时 apt 也走代理

# 持久化写入目标主机（systemd drop-in + /etc/profile.d/）
cluster_http_proxy:  "http://proxy.example.com:3128"
cluster_https_proxy: "http://proxy.example.com:3128"
cluster_no_proxy:
  - "127.0.0.1,localhost"
  - "10.0.0.0/8"
  - "cluster.local"
```

### 隧道路由（云环境 / 无二层可达）

```yaml
cilium_routing_mode: "tunnel"           # 默认
cilium_tunnel_protocol: "vxlan"         # 或 "geneve"
```

### OpenStack / 启用端口安全的云环境

三个必坑，详解见 [docs/openstack.md](docs/openstack.md)：

1. `cilium_non_masquerade_cidrs` **只能**包含 Pod + Service CIDR。
   加入节点网段会让 Pod IP 泄漏到物理网卡，被 Neutron 判为伪造帧丢弃。
2. `kube_ovn_pod_cidr` **不得**与节点网段重叠
   （默认 `10.18.0.0/16`；如果节点在 `10.16.x.x`，就不能用 `10.16.0.0/16`）。
3. VM Port 上**无需**配置 `allowed_address_pairs` —— BPF masquerade + VXLAN
   已经把 Pod IP 隐藏在节点 IP 之后。

> **Service IP 小坑**：只有 TCP/UDP 能通过 Service IP，`ping <svc-ip>` 一定
> 失败——Cilium 的 eBPF DNAT 在 `connect()` 系统调用中生效，ICMP 不走这条路径。

### 关闭 Longhorn，改用 local-path

```yaml
enable_longhorn: false
```

`cluster_storage_class` 会自动推导为 `local-path`，所有消费方
（VictoriaMetrics、VictoriaLogs 等）同步生效。

## Pod 资源分层

所有 role 的资源块统一引用三档共享档位（不再硬编码）。默认按裸金属
（96C / 512GB+）尺寸，改一处即可整体缩放。

| 档位 | limits | requests | 使用方 |
|------|--------|----------|--------|
| small | 1 CPU / 512Mi | 100m / 256Mi | sidecar、operator、multus |
| medium | 2 CPU / 2Gi | 500m / 1Gi | cilium agent、grafana、vmagent |
| large | 4 CPU / 8Gi | 1 / 4Gi | vmsingle、vmstorage、vmcluster |

```yaml
cluster_pod_resources:
  large:
    limits:   {cpu: 8, memory: 16Gi}
    requests: {cpu: 2, memory: 8Gi}
```

## 文档

- [docs/architecture.md](docs/architecture.md) —— 三层结构、派生默认值、CNI 共存机制（英文）
- [docs/configuration.md](docs/configuration.md) —— 全部覆盖项与常见场景（英文）
- [docs/globals-overrides.md](docs/globals-overrides.md) —— globals.yaml 覆盖机制：变量解析顺序、组件开关、版本覆盖、FAQ
- [docs/kubelet-numa-research.md](docs/kubelet-numa-research.md) —— kubelet topology-manager 源码分析、GPU NUMA 对齐修复、KubeVirt 虚拟机 NUMA 映射
- [docs/kubelet-tuning.md](docs/kubelet-tuning.md) —— 节点组 kubelet 预设、precheck、角色切换
- [docs/longhorn-storage.md](docs/longhorn-storage.md) —— Longhorn 存储原理、副本放置、备份
- [docs/openstack.md](docs/openstack.md) —— OpenStack / 端口安全底层
- [docs/victoria-metrics.md](docs/victoria-metrics.md) —— VictoriaMetrics / VictoriaLogs 存储选项、节点固定、HA
- [docs/architecture.zh-CN.md](docs/architecture.zh-CN.md) —— 设计说明（中文）
- [docs/configuration.zh-CN.md](docs/configuration.zh-CN.md) —— 配置指南（中文）

## 致谢

上游：[axivo/k3s-cluster](https://github.com/axivo/k3s-cluster) —— 三层角色
划分、内嵌 etcd + HAProxy + Keepalived、Helm 驱动的部署范式均出自此。

License：BSD 3-Clause（继承自上游）。
