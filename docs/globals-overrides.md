# globals.yaml 指南

`globals.yaml` 是本仓库**唯一需要日常修改的配置文件**。本文档深入说明它的
定位、变量解析机制、可覆盖项清单，以及修改后应该运行哪个 playbook。

- 快速上手与常见场景：[配置指南](./配置指南.md)
- 设计原由（为何用扁平变量而非嵌套覆盖）：[架构设计](./架构设计.md)

## 1. 定位

```
inventory/cluster/group_vars/all/
├── main.yaml            # 仓库基线：defaults: 映射 + global_map + 密钥桥接（不要改）
├── globals-sample.yaml  # 覆盖模板（进 Git）
├── globals.yaml         # 你的覆盖（gitignored）← 唯一日常编辑的文件
└── passwords.yaml       # 全部密钥，vault 加密（不在 globals.yaml 里）
```

```bash
cp inventory/cluster/group_vars/all/globals-sample.yaml \
   inventory/cluster/group_vars/all/globals.yaml
```

`globals.yaml` 被 `.gitignore` 忽略——它是集群特有配置，不进 Git；
`globals-sample.yaml` 才是进 Git 的模板。Ansible 自动加载
`group_vars/all/` 下的所有文件，无需任何注册动作。

## 2. 变量解析顺序

本仓库的角色变量是**嵌套结构**（如 `cilium_vars.kubernetes.bpf.datapath_mode`）。
由于 Ansible 默认 `hash_behaviour = replace`，直接在 `globals.yaml` 里写嵌套
key 会**整体覆盖**角色默认值，造成静默破坏。因此采用 kolla 风格的
**扁平覆盖变量 + Jinja 回退**机制：

```
globals.yaml（你的覆盖，最高优先级）
    cilium_kube_proxy_replacement: false
        ↓ 变量未定义时回退到
main.yaml 的 defaults: 映射（仓库基线）
    defaults.cilium.kube_proxy_replacement: true
        ↓ 被角色 defaults 引用
roles/cilium/defaults/main.yaml
    cilium_vars.kubernetes.kube_proxy.replacement: '{{ cilium_kube_proxy_replacement | default(defaults.cilium.kube_proxy_replacement) }}'
        ↓ 渲染进 Helm values
roles/cilium/templates/values.j2
    kubeProxyReplacement: false
```

要点：

| 层 | 文件 | 优先级 | 何时编辑 |
|----|------|--------|---------|
| 扁平覆盖变量 | `globals.yaml` | 最高 | **单集群调优（日常）** |
| `defaults:` 基线 | `main.yaml` | 中 | 改变"无覆盖时的默认值"（很少） |
| 角色兜底字面量 | `roles/*/defaults/main.yaml` | 低 | 高级调优（很少，如 chart 版本字面量） |

- 大多数参数走 `{{ <扁平变量> | default(defaults.<组件>.<参数>) }}` 两级回退。
- chart 版本等少数变量在角色 defaults 里直接写字面量兜底：
  `{{ cilium_chart_version | default("v1.19.3") }}`——覆盖效果相同。
- `enable_*` 开关只存在于 `globals.yaml`；playbook 通过
  `lookup('vars', 'enable_' ~ role, default=true)` 读取，
  **变量完全未定义时回退为 true**（即不复制 sample 直接跑会部署全部组件）。

## 3. 命名约定

```yaml
enable_<component>: true|false    # 组件部署开关
<component>_<param>: value        # 覆盖 defaults.<component>.<param>
cluster_<param>: value            # 集群级派生默认值的强制覆盖
```

含连字符的组件名在变量中改用下划线：`enable_external_dns`、
`enable_victoria_metrics`、`enable_victoria_logs`、`enable_argo_cd`、
`enable_kube_ovn`、`enable_gpu_operator`（tag 仍用连字符，如 `-t argo-cd`）。

## 4. 必须设置

```yaml
cluster_api_host: "192.168.4.10"    # LB / K3s API VIP（Keepalived 绑定的地址）

# 可选：Keepalived 绑定 VIP 的网卡（默认 eth0）
# 必须在每个 server 节点上都存在，且与 cluster_api_host 同二层广播域
# network_interface: "eth0"
```

## 5. 组件开关一览

| 组件 | 开关 | 默认 | 说明 |
|------|------|------|------|
| Cilium | `enable_cilium` | `true` | eBPF CNI、kube-proxy 替代、Gateway API |
| Multus | `enable_multus` | `false` | meta-CNI，Pod 多网卡的前提 |
| Kube-OVN | `enable_kube_ovn` | `false` | 次级 CNI（经 Multus 挂载） |
| CoreDNS | `enable_coredns` | `true` | 集群 DNS |
| ExternalDNS | `enable_external_dns` | `false` | Cloudflare 记录同步（需凭据） |
| cert-manager | `enable_cert_manager` | `true` | ACME / Let's Encrypt |
| Longhorn | `enable_longhorn` | `true` | 分布式块存储 |
| Ceph CSI | `enable_ceph_csi` | `false` | 对接外部 Ceph（需凭据） |
| metrics-server | `enable_metrics_server` | `true` | HPA/VPA 指标源 |
| VictoriaLogs | `enable_victoria_logs` | `true` | 日志聚合 |
| VictoriaMetrics | `enable_victoria_metrics` | `true` | 指标 + Grafana + 告警 |
| ArgoCD | `enable_argo_cd` | `false` | GitOps CD |
| Kured | `enable_kured` | `false` | 节点自动重启 |
| KubeVirt | `enable_kubevirt` | `false` | 虚拟机 |
| GPU Operator | `enable_gpu_operator` | `false` | NVIDIA GPU（需 GPU 硬件） |

设置 `enable_<component>: false`：

- 跳过该组件的部署与 postinstall（`provisioning.yaml` 中按开关过滤）
- 触发派生默认值自动切换（见第 7 节）
- `reset.yaml` **不受**开关过滤——拆除时总是清理所有组件

## 6. 版本覆盖清单

所有软件版本都可用扁平变量覆盖（完整可调项见
[globals-sample.yaml](../inventory/cluster/group_vars/all/globals-sample.yaml)）：

```yaml
# --- 核心二进制 ---
# k3s_version: "v1.34.6+k3s1"
# helm_version: "v3.20.0-1"
# helm_diff_plugin_version: "v3.15.6"
# kubepug_version: "v1.7.1"

# --- 网络 ---
# cilium_chart_version: "v1.19.3"
# cilium_cli_version: "v0.19.2"
# cilium_gateway_api_version: "v1.5.1"
# cilium_hubble_cli_version: "v1.18.6"
# multus_image_tag: "v4.3.0"
# kube_ovn_chart_version: "v1.16.2"
# coredns_chart_version: "v1.45.2"
# external_dns_chart_version: "v1.20.0"
# cert_manager_chart_version: "v1.20.2"
# cert_manager_cli_version: "v2.4.1"

# --- 存储 ---
# longhorn_chart_version: "v1.11.1"
# longhorn_cli_version: "v1.11.1"
# ceph_csi_chart_version: "1.0.4"

# --- 可观测性 ---
# metrics_server_chart_version: "v3.13.0"
# victoria_logs_chart_version: "v0.12.0"
# victoria_metrics_chart_version: "v0.74.1"
# victoria_metrics_prometheus_chart_version: "v28.0.1"

# --- 应用 / 计算 ---
# argo_cd_chart_version: "v9.5.2"
# argo_cd_cli_version: "v3.3.7"
# kured_chart_version: "v5.11.0"
# kubevirt_version: "v1.8.4"
# gpu_operator_chart_version: "v26.3.3"
```

> 升级版本时用 `upgrade.yaml -t <component>`（见第 12 节），不要重跑
> provisioning。

## 7. 派生默认值

以下设置随 `enable_*` 开关自动推导，**无需手动同步**；确有需要时用
`cluster_*` 变量强制覆盖：

| 设置 | 推导逻辑 | 强制覆盖 |
|------|---------|---------|
| 存储类 | `longhorn`（启用时），否则 `local-path` | `cluster_storage_class` |
| ServiceMonitor | 跟随 `enable_victoria_metrics` | `cluster_service_monitor_enabled` |
| Ingress class | `cilium`（启用时），否则 `traefik` | `cluster_ingress_class` |
| CNI 独占 | 默认 `false`（保留其他 CNI 配置文件） | `cilium_cni_exclusive` |
| CPU 架构 | 从 `ansible_facts.machine` 自动探测 | `cluster_architecture` |

```yaml
# 例：关闭 Longhorn 后强制所有组件使用特定存储类
# cluster_storage_class: "rook-ceph"
```

## 8. 代理与镜像加速

```yaml
# 1) 部署期代理（临时，仅 Ansible 执行期间生效，不写入主机）
# cluster_deploy_proxy: "http://10.0.0.1:7890"
# cluster_apt_proxy: false            # true 时 apt 也走代理

# 2) 持久化代理（写入 /etc/profile.d/、systemd drop-in、git config）
# cluster_http_proxy: "http://10.0.0.1:7890"
# cluster_https_proxy: "http://10.0.0.1:7890"
# cluster_no_proxy:
#   - "127.0.0.1,localhost"
#   - "10.0.0.0/8"
#   - "cluster.local"

# 3) Registry 镜像（docker.io / ghcr.io / nvcr.io 等全部镜像到该端点）
# cluster_registry_endpoint: "http://10.254.25.5:4000"

# 4) helm-diff 插件下载镜像（GitHub 慢/不可达时）
# helm_plugin_diff_url: "https://gitee.com/mirrors_databus23/helm-diff"
```

## 9. Kubelet 调优入口

`kubelet-arg` 由"公共参数 + 组级覆盖 + 主机级追加"三段拼接，
覆盖列表就是完整的组级列表（没有预设模板）：

```yaml
# Pod 工作节点（默认空 = kubelet 内置默认）
# pod_kubelet_override: []

# KubeVirt 工作节点：CPU 静态绑定 + NUMA 对齐的推荐起点
# kubevirt_kubelet_override:
#   - cpu-manager-policy=static
#   - topology-manager-policy=restricted
#   - feature-gates=CPUManagerPolicyAlphaOptions=true
#   - cpu-manager-policy-options=distribute-cpus-across-numa=true
#   - reserved-cpus=4
```

主机级追加（`host_vars/<node>.yaml`，**追加**而非替换组级列表）：

```yaml
kubevirt_kubelet_override_host:
  - reserved-cpus=0,1
```

> kubelet 策略变更前会自动 precheck：若节点上有运行中的 KubeVirt VM
> 且策略在 `static`/`none` 间切换，会直接失败避免杀掉 VM。
> 详解见 [KUBELET-TUNING.md](./KUBELET-TUNING.md)。

## 10. Pod 资源档位

所有角色的资源块统一引用三档共享档位（裸金属 96C/512GB+ 尺寸），
改一处即可整体缩放：

```yaml
# cluster_pod_resources:
#   small:                     # sidecar、operator、multus
#     limits: {cpu: 1, memory: 512Mi}
#     requests: {cpu: 100m, memory: 256Mi}
#   medium:                    # cilium agent、grafana、vmagent
#     limits: {cpu: 2, memory: 2Gi}
#     requests: {cpu: 500m, memory: 1Gi}
#   large:                     # vmsingle、vmstorage、vmcluster
#     limits: {cpu: 4, memory: 8Gi}
#     requests: {cpu: 1, memory: 4Gi}
```

## 11. 与密码系统的联动

`globals.yaml` **不存放任何密钥**。密钥是独立的扁平变量，位于
`passwords.yaml`（vault 加密）或 `/etc/k3s-cluster/passwords.yaml`（明文，
优先级更高）：

- 内部密码（UUID 自动生成）：`password_argocd_admin`、`password_grafana_admin` 等
- 外部凭据（需手动填入）：`credential_cloudflare_api_token`、
  `credential_ceph_admin_key`、`credential_longhorn_backup_password`、
  `credential_slack_webhook_url`、`credential_postfix_*`

```bash
python3 tools/secrets.py init      # 生成内部密码 + 外部凭据占位
python3 tools/secrets.py edit      # 填入外部凭据
python3 tools/secrets.py list      # 查看状态
```

**自动联动**：外部凭据留空时，`tools/secrets.py` 会自动把对应组件的
`enable_*` 开关改写为 `false`（直接修改 `globals.yaml`），避免因缺 key
导致部署失败。填好凭据后重新把开关置回 `true` 即可。

## 12. 修改后运行什么

| 我改了… | Playbook | Tag |
|---------|----------|-----|
| `kubevirt_kubelet_override` / `pod_kubelet_override` | `provisioning.yaml` | `kubernetes` |
| `cluster_http_proxy` 等 OS 级代理 | `provisioning.yaml` | `cluster` |
| 组件参数（chart 值、IP 池、副本数…） | `provisioning.yaml` | `<component>` |
| `k3s_version` | `upgrade.yaml` | `k3s` |
| `helm_version` / `helm_diff_plugin_version` | `upgrade.yaml` | `helm` |
| 组件 chart 版本 | `upgrade.yaml` | `<component>` |
| OS 级设置（postfix、自动更新） | `upgrade.yaml` | `cluster` |

> 用 `-t kubernetes --limit` 时必须包含至少一个 server 节点
> （kubelet precheck 需委托 server 查询 API）。

## 13. 常见问题

**Q：改了 sample 里的注释值为什么不生效？**
A：`globals-sample.yaml` 里绝大多数行是注释掉的模板。必须复制为
`globals.yaml` 并**取消注释**对应行，覆盖才会存在。

**Q：能不能直接写嵌套变量，比如 `cilium_vars:`？**
A：不要。Ansible 默认 `hash_behaviour = replace`，嵌套 key 会把角色的
整个 defaults 字典静默替换掉。永远只写扁平覆盖变量。

**Q：删掉一个覆盖变量会怎样？**
A：直接回退到 `main.yaml` 的 `defaults:` 基线（或角色兜底字面量），
无需任何清理动作。

**Q：为什么 `globals.yaml` 不进 Git？**
A：它是集群特定配置（IP、开关、规模），跨集群差异大；进 Git 的是
`globals-sample.yaml` 模板。备份集群配置时请单独保存此文件
（密码在 `passwords.yaml`，vault 加密，可入 Git）。

**Q：`enable_*` 开关和 `--tags` 是什么关系？**
A：两层过滤，缺一不可——`--tags <component>` 决定"跑哪些角色"，
`enable_<component>` 决定"该角色是否被跳过"。开关为 `false` 时，
即使指定了 tag 也不会部署。
