# 架构与设计说明

本文档说明 k3s-cluster 分支的设计目的和原由。
如需了解**如何配置**集群，请参阅[配置指南](./配置指南.md)。

## 概述

本分支在上游 [axivo/k3s-cluster](https://github.com/axivo/k3s-cluster) 基础上，
引入了 kolla-ansible 风格的配置模型。目标：**只改一个文件即可定制整个集群**，
无需触碰角色内部代码。

主要改动：

1. **kolla 风格 `globals.yaml`** — 单一覆盖文件，含 `enable_*` 开关
2. **`all.yaml` 中的 `defaults` 映射** — 默认值的唯一真相来源
3. **派生默认值** — storage class、ServiceMonitor、ingress class、架构
   自动适配 `enable_*` 开关
4. **多 CNI 共存** — Multus + Cilium + Kube-OVN
5. **统一 Pod 资源档位** — 3 档（small/medium/large）供所有角色共享
6. **KubeVirt 集成** — kubelet CPU 绑定 profile、GPU operator、feature gates
7. **独立 `passwords.yaml`** — 扁平 vault 加密变量（kolla `passwords.yml` 模式）
8. **架构自动检测** — 二进制后缀由 `ansible_facts.machine` 推导

---

## 1. 为什么用三层而非 kolla 的两层？

### kolla-ansible 的做法

kolla 使用**两层结构且变量名相同**：

```
ansible/group_vars/all.yml     →  enable_neutron: "yes"  （默认值）
/etc/kolla/globals.yml         →  enable_neutron: "no"   （覆盖，通过 -e）
```

这能工作是因为 kolla 的变量**全是扁平顶层**，覆盖就是简单的同名替换。

### 我们的问题：嵌套字典 + hash_behaviour

本项目的角色使用**嵌套字典**（`cilium_vars.kubernetes.bpf.datapath_mode`、
`victoriametrics_vars.kubernetes.vmsingle.storage.class`……）。Ansible 默认的
`hash_behaviour = replace` 会导致用户在 `globals.yaml` 中写嵌套键时
**静默抹除**整个角色的默认值：

```yaml
# ❌ 危险：会替换整个 cilium_vars 字典
cilium_vars:
  kubernetes:
    kube_proxy:
      replacement: false
```

### 解决方案：扁平覆盖变量 + Jinja 回退

引入**扁平顶层变量**（`cilium_kube_proxy_replacement`），角色 defaults 通过
Jinja `default()` 引用：

```
globals.yaml        →  cilium_kube_proxy_replacement: false     （用户覆盖）
                           ↓ 未定义时
all.yaml defaults   →  defaults.cilium.kube_proxy_replacement   （仓库基线）
                           ↓ 被引用
role defaults        →  cilium_vars.kubernetes.kube_proxy.replacement
                           ↓ 被消费
values.j2            →  kubeProxyReplacement: false              （Helm 输入）
```

**结果**：用户只编辑 `globals.yaml` 里的扁平变量，不会破坏角色内部结构。

---

## 2. 派生默认值（自动适配 enable_* 开关）

多个默认值**自动推导**，无需用户手动联动：

| 变量 | 推导逻辑 | 手动覆盖 |
|------|---------|---------|
| `storage_class` | `enable_longhorn` 为真用 `longhorn`，否则 `local-path` | `cluster_storage_class` |
| `service_monitor_enabled` | 跟随 `enable_victoria_metrics` | `cluster_service_monitor_enabled` |
| `ingress_class` | `enable_cilium` 为真用 `cilium`，否则 `traefik` | `cluster_ingress_class` |
| `cni_exclusive` | 除非 `enable_multus`，否则 `true` | `cilium_cni_exclusive` |
| `architecture` | 来自 `ansible_facts.machine` | `cluster_architecture` |
| `architecture_binary` | 由 `architecture` 推导（arm64/amd64/arm） | （派生，不可覆盖） |

### 覆盖优先级（从高到低）

1. **组件级专用**（`victoria_metrics_storage_class: "nfs-client"`）
2. **集群级覆盖**（`cluster_storage_class: "rook-ceph"`）
3. **由 `enable_*` 开关自动推导**
4. **仓库基线**（`all.yaml` 中的 `defaults` 映射）
5. **角色 defaults**（极少触达）

例如：禁用 Longhorn 后，所有存储消费者（VM、VLogs）自动切换到 `local-path`，
无需任何额外配置。

---

## 3. 多 CNI 架构

### CNI 配置文件排序

```
/etc/cni/net.d/
├── 00-multus.conf           ← Multus（meta-plugin，最先加载）
├── 05-cilium.conflist        ← Cilium（主 CNI，所有 Pod 都有）
└── 10-kube-ovn.conflist      ← Kube-OVN（附加 CNI，标注的 Pod 才挂载）
```

kubelet 按**字典序**加载 CNI 配置。`00` < `05` < `10`，所以 Multus 是入口。
Multus 再 delegate 给 Cilium（默认）和 Kube-OVN（通过 NetworkAttachmentDefinition）。

### Cilium `cni.exclusive` 自动禁用

Cilium 的 `exclusive: true` 会把其他 CNI 配置改名为 `*.cilium_bak`，这会
破坏 Multus 的 `00-multus.conf`。当 `enable_multus: true` 时，
`defaults.cluster.cni_exclusive` 自动推导为 `false`，保证共存。

### Kube-OVN 作为 Non-Primary CNI

Kube-OVN **永远不是主 CNI**。它运行在 `NON_PRIMARY_CNI: true` 模式下，
通过 Multus NetworkAttachmentDefinition 暴露给 Pod。Cilium 始终是默认网络。

---

## 4. 统一 Pod 资源档位

### 问题

9 个角色中有 33 个硬编码资源块，每个值都很小（`10m CPU / 128Mi 内存`）。
Multus 的 OOM 问题（[#1416](https://github.com/k8snetworkplumbingwg/multus-cni/issues/1416)）
就是 `limits == requests == 50Mi`——没有 burst 空间，启动即 OOM。

### 解决方案：3 个共享档位

所有 33 个块现在引用 `defaults.cluster.pod_resources.<tier>`：

| 档位 | limits | requests | 用于 |
|------|--------|---------|------|
| `small` | 1 CPU / 512Mi | 100m / 256Mi | sidecars、operators、controllers、multus |
| `medium` | 2 CPU / 2Gi | 500m / 1Gi | cilium agent、grafana、vlogs server、vmagent |
| `large` | 4 CPU / 8Gi | 1 / 4Gi | vmsingle、vmstorage、vmcluster |

**limits > requests** = burst 空间。Pod 可临时超过 requests 而不被 OOM-kill
（修复了 Multus 的启动 OOM 问题）。

覆盖一个档位即可调整整个集群：

```yaml
# globals.yaml
cluster_pod_resources:
  large:
    limits: {cpu: 8, memory: 16Gi}
    requests: {cpu: 2, memory: 8Gi}
```

---

## 5. KubeVirt 集成

### Feature gates

`NetworkBindingPlugins` 默认启用——它开启 `managedTap` 网络绑定模式，
VM 通过 Multus 多网卡必需。

Feature gates 暴露为可配置列表（`kubevirt_feature_gates`）。

### Kubelet CPU 绑定 profile

KubeVirt VM 设置 `resources.requests.cpu: N` + `cpuManagerPolicy: static`
可获得专用 CPU 核心。`cluster_kubelet_kubevirt_profile` 标志自动配置：

- `cpuManagerPolicy: static`
- `cpuManagerPolicyOptions: {distribute-cpus-across-numa: "true"}`
- `topologyManagerPolicy: restricted`
- `feature-gates: CPUManagerPolicyAlphaOptions=true`

默认关闭（`false`）。仅在运行需要 CPU pinning 的 KubeVirt VM 时开启。

### GPU Operator

`enable_gpu_operator: true` 部署 NVIDIA GPU Operator，包含：
- CDI 启用（KubeVirt GPU 透传必需）
- `sandboxWorkloads.mode: kubevirt`（GPU 到 VM）
- DCGM exporter → VictoriaMetrics ServiceMonitor

---

## 6. 架构自动检测

`defaults.cluster.architecture` 和 `architecture_binary` 由
`ansible_facts.machine` 在运行时推导（由 `cluster` 角色的 `tasks/facts.yaml` 设置）。

| `ansible_facts.machine` | 二进制后缀 | 受影响的文件 |
|------------------------|-----------|------------|
| `x86_64` | `amd64` | k3s、cilium、hubble、cmctl、argocd、virtctl、longhornctl、kubepug |
| `aarch64` | `arm64` | （同上） |
| `armv7l` | `arm` | （同上） |
| （其他） | `amd64`（兜底） | |

**异构集群**：`cluster_map` 是 per-host 的，每台节点下载匹配自身架构的二进制，
无需全局架构设置。

---

## 7. 密钥分离

参照 kolla 的 `passwords.yml` 模式，密钥放在独立的 `passwords.yaml` 文件中，
使用扁平变量名：

| 类型 | 前缀 | 示例 | 生成方式 |
|------|------|------|---------|
| 内部密码 | `password_*` | `password_argocd_admin` | `generate-passwords.py`（UUID） |
| 外部凭据 | `credential_*` | `credential_cloudflare_api_token` | `setup-secrets.py`（用户输入） |

`all.yaml` 将这些桥接到角色模板期望的嵌套 `global_map.credentials.*` 结构——
**模板零改动**。

```yaml
# all.yaml（桥接层）
global_map:
  credentials:
    argocd:
      server:
        admin:
          password: '{{ password_argocd_admin }}'   # ← 引用 passwords.yaml
```

---

## 8. 命名规范

| 层级 | 模式 | 示例 |
|------|------|------|
| 用户覆盖（globals.yaml） | `<component>_<param>`（snake_case） | `cilium_routing_mode` |
| 仓库基线（all.yaml defaults） | `defaults.<component>.<param>` | `defaults.cilium.routing_mode` |
| 角色内部 | `<role>_vars.kubernetes.<section>.<key>` | `cilium_vars.kubernetes.routing.mode` |
| Helm chart 值 | camelCase（按上游 chart） | `routingMode` |

含连字符的组件名在扁平变量中改用下划线：`enable_external_dns`、
`enable_victoria_metrics`、`enable_argo_cd`、`enable_kube_ovn`、
`enable_gpu_operator`。

---

## 9. 本分支新增的角色

| 角色 | 类型 | 用途 |
|------|------|------|
| `kubevirt` | Manifest | Kubernetes 中的虚拟机（operator + CR） |
| `multus` | Manifest | meta-CNI，支持 Pod 多网卡 |
| `kube-ovn` | Helm | 附加 CNI（Non-Primary 模式，通过 Multus） |
| `gpu-operator` | Helm | NVIDIA GPU 管理 + KubeVirt 透传 |

所有角色遵循统一模式：
- `globals.yaml` 中的 `enable_*` 开关
- `all.yaml` `defaults` 映射中的扁平变量
- 角色 defaults 通过 `{{ var | default(defaults.<role>.<key>) }}` 引用
- 标签注册在 `global_map.tags.role/charts/postinstall`
- `reset.yaml` 包含清理逻辑（不受 enable 开关过滤）
