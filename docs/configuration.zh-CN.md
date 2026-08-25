# 配置指南

本文档说明**如何配置** k3s-cluster 部署。
如需了解设计原由，请参阅[架构设计](./architecture.zh-CN.md)。

## 文件布局

```
inventory/cluster/
├── hosts-sample.yaml           # 节点清单模板（复制为 hosts.yaml）
├── hosts.yaml                  # 你的清单（gitignored）
└── group_vars/
    └── all/                    # 'all' 组的 group_vars（自动加载）
        ├── main.yaml           # 仓库基线 + defaults 映射 + 密钥桥接 + 标签
        ├── globals-sample.yaml # 覆盖模板（复制为 globals.yaml）
        ├── globals.yaml        # 用户覆盖（kolla 风格，gitignored）← 改这个文件
        └── passwords.yaml      # 所有 vault 加密密钥（kolla passwords.yml）
```

Ansible 会自动加载 `group_vars/all/` 下的所有文件。目录名 `all` 对应
隐含的 `all` 组，所以这些变量作用于所有主机。

## 什么时候改什么文件

| 文件 | 何时编辑 | 内容 |
|------|---------|------|
| `globals.yaml` | **单集群调优** | `enable_*` 开关、覆盖变量（注释形式） |
| `passwords.yaml` | **密钥设置** | vault 加密的密码 + 凭据 |
| `all.yaml` | **更改仓库基线**（很少） | `defaults` 映射、标签、凭据桥接 |
| `hosts.yaml` | **节点清单** | server/agent 主机名（从 `hosts-sample.yaml` 复制） |
| `roles/*/defaults/main.yaml` | **高级调优**（很少） | 嵌套角色变量、chart 版本 |

## 快速开始

```bash
# 1. 生成内部密码（UUID 风格）
python3 generate-passwords.py

# 2. 填入外部凭据（Cloudflare、iCloud、Slack、NAS、SSH）
python3 setup-secrets.py

# 3. 编辑 globals.yaml 配置你的网络
vim inventory/cluster/group_vars/all/globals.yaml

# 4. 校验
ansible-playbook validation.yaml

# 5. 部署
ansible-playbook provisioning.yaml
```

## globals.yaml — 你需要编辑的唯一文件

### 组件启用开关

```yaml
enable_cilium: true
enable_coredns: true
enable_cert_manager: true
enable_external_dns: true
enable_longhorn: true
enable_metrics_server: true
enable_victoria_logs: true
enable_victoria_metrics: true
enable_argo_cd: false
enable_kured: false
enable_kubevirt: false
enable_multus: false           # meta-CNI，支持 Pod 多网卡
enable_kube_ovn: false          # 附加 CNI（通过 Multus）
enable_gpu_operator: false      # NVIDIA GPU（需要 GPU 硬件）
```

设置 `enable_<component>: false` 会：
- 跳过部署和 postinstall
- 触发派生默认值（如 storage class → `local-path`）
- `reset.yaml` **不受** enable 过滤——总是清理所有组件

### 常用覆盖（取消注释即生效）

```yaml
# cluster_api_host: "192.168.4.10"              # LB / K3s API VIP
# cluster_architecture: "aarch64"                # 覆盖自动检测
# cluster_storage_class: "rook-ceph"             # 覆盖所有存储消费者
# cilium_lb_ip_start: "192.168.4.20"            # LoadBalancer IP 池
# cilium_routing_mode: "tunnel"                  # native | tunnel
# cilium_ipam_mode: "kubernetes"                # cluster-pool | kubernetes
# victoria_metrics_storage_size: "100Gi"         # vmsingle PVC 大小
# kubevirt_use_emulation: true                   # 无 /dev/kvm 时
# cluster_kubelet_kubevirt_profile: true         # VM CPU 绑定
```

### 派生默认值（自动适配，无需手动配置）

| 设置 | 自动适配 | 覆盖入口 |
|------|---------|---------|
| 存储类 | `enable_longhorn` 为真用 `longhorn`，否则 `local-path` | `cluster_storage_class` |
| ServiceMonitor | `enable_victoria_metrics` 为真则启用 | `cluster_service_monitor_enabled` |
| Ingress class | `enable_cilium` 为真用 `cilium`，否则 `traefik` | `cluster_ingress_class` |
| CNI 独占 | `enable_multus` 为真则关闭 | `cilium_cni_exclusive` |
| 架构 | 来自 `ansible_facts.machine` | `cluster_architecture` |

### Pod 资源档位

```yaml
# cluster_pod_resources:
#   small:                                    # sidecars、operators、multus
#     limits: {cpu: 1, memory: 512Mi}
#     requests: {cpu: 100m, memory: 256Mi}
#   medium:                                   # agents（cilium、grafana、vmagent）
#     limits: {cpu: 2, memory: 2Gi}
#     requests: {cpu: 500m, memory: 1Gi}
#   large:                                    # 存储（vmsingle、vmstorage）
#     limits: {cpu: 4, memory: 8Gi}
#     requests: {cpu: 1, memory: 4Gi}
```

9 个角色中的 33 个资源块全部引用这些档位。覆盖一个档位即可调整整个集群。

## 密钥管理

### passwords.yaml

所有密钥以扁平 vault 加密变量形式存储：

| 变量 | 类型 | 设置方式 |
|------|------|---------|
| `password_argocd_admin` | 内部 | `generate-passwords.py`（UUID） |
| `password_argocd_user` | 内部 | `generate-passwords.py`（UUID） |
| `password_grafana_admin` | 内部 | `generate-passwords.py`（UUID） |
| `credential_cloudflare_api_token` | 外部 | `setup-secrets.py` |
| `credential_postfix_alias` | 外部 | `setup-secrets.py` |
| `credential_postfix_name` | 外部 | `setup-secrets.py` |
| `credential_postfix_password` | 外部 | `setup-secrets.py` |
| `credential_slack_webhook_url` | 外部 | `setup-secrets.py` |
| `credential_longhorn_backup_password` | 外部 | `setup-secrets.py` |

SSH 连接凭据（`ansible_user`、`ansible_password`、`ansible_become_password`）
**不**存放在 vault 中。默认部署模式为 SSH key + 免密 sudo。仅当目标主机
需要密码认证时，才在 `globals.yaml` 中设置；或在命令行使用
`--ask-pass` / `--ask-become-pass`。

### 命令

```bash
# 生成内部 UUID 密码（ArgoCD、Grafana）
python3 generate-passwords.py
python3 generate-passwords.py --force    # 重新生成全部

# 交互式填入外部凭据
python3 setup-secrets.py

# 列出 / 加密 / 轮换 vault 密码
ansible-playbook vault.yaml
```

## 常见场景

### 禁用 Longhorn，使用 local-path

```yaml
enable_longhorn: false
```
存储类自动派生为 `local-path`，所有存储消费者生效。

### 多 CNI：Multus + Cilium + Kube-OVN

```yaml
enable_multus: true
enable_kube_ovn: true
```
Cilium 的 `cni.exclusive` 自动关闭。Kube-OVN 运行在 Non-Primary 模式。

CNI 配置顺序：`00-multus.conf` → `05-cilium.conflist` → `10-kube-ovn.conflist`

### KubeVirt + GPU 透传

```yaml
enable_kubevirt: true
enable_gpu_operator: true
gpu_operator_sandbox_workloads_enabled: true
cluster_kubelet_kubevirt_profile: true    # CPU 绑定 + NUMA 对齐
```

### Tunnel 路由（云环境）

```yaml
cilium_routing_mode: "tunnel"
cilium_tunnel_protocol: "geneve"
```

### 仅对单个组件使用自定义存储类

```yaml
enable_longhorn: true                           # 基线 = longhorn
victoria_metrics_storage_class: "rook-ceph"     # 仅 VM 用 rook
```

### 跳过 ArgoCD

```yaml
enable_argo_cd: false
```

## 校验

```bash
# 全量校验
ansible-playbook validation.yaml

# 单个组件
ansible-playbook validation.yaml -t cilium

# 升级模拟（helm diff）
ansible-playbook validation.yaml -t victoria-metrics
```

检查项：URL 可达性、Helm values 渲染、`helm diff`、kubeconfig 有效性。

## 部署

```bash
# 全量部署
ansible-playbook provisioning.yaml

# 分阶段部署
ansible-playbook provisioning.yaml -t cluster       # OS + 硬件
ansible-playbook provisioning.yaml -t kubernetes     # K3s + Helm
ansible-playbook provisioning.yaml -t charts         # 所有 chart 角色

# 单个组件
ansible-playbook provisioning.yaml -t kubevirt

# 升级
ansible-playbook upgrade.yaml -t cilium

# 重置（清理所有，包括已禁用的组件）
ansible-playbook reset.yaml
ansible-playbook reset.yaml -t kubevirt    # 单个组件
```

## 添加新的覆盖项

1. **`all.yaml`** — 在 `defaults` 映射中添加：
   ```yaml
   defaults:
     cilium:
       new_param: default_value
   ```

2. **角色 `defaults/main.yaml`** — 引用带回退：
   ```yaml
   some_key: '{{ cilium_new_param | default(defaults.cilium.new_param) }}'
   ```

3. **`globals.yaml`** — 添加注释提示（可选）：
   ```yaml
   # cilium_new_param: "default_value"
   ```

## 变量参考

权威来源是 `all.yaml` 中的 `defaults:` 映射。
`globals.yaml` 以注释形式镜像同样的键，作为快速参考。
