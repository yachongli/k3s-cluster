# Kubelet 高级调优与节点组切换

本文说明 k3s-cluster 如何通过 inventory 节点组（`pod` / `kubevirt`）实现 kubelet 差异化配置，以及切换节点角色时的安全验证机制（precheck）。

- 相关配置文件：
  - `inventory/cluster/hosts-sample.yaml` — 节点组定义
  - `inventory/cluster/group_vars/all/main.yaml` — kubelet 预设（`common_args` + `pod` + `kubevirt`）
  - `inventory/cluster/group_vars/pod.yaml` — Pod 组覆盖
  - `inventory/cluster/group_vars/kubevirt.yaml` — KubeVirt 组覆盖
  - `roles/k3s/tasks/precheck.yaml` — 切换前安全检查
  - `roles/k3s/tasks/facts.yaml` — kubelet-arg 拼接逻辑

---

## 1. 为什么需要差异化 kubelet

KubeVirt VM 需要 **static CPU pinning + restricted topology** 来获得确定性延迟：

| kubelet 策略 | 值 | 作用 |
|---|---|---|
| `cpuManagerPolicy` | `static` | 把整核独占给 Guaranteed QoS Pod/VM |
| `topologyManagerPolicy` | `restricted` | 要求 CPU+内存必须在同一 NUMA node |
| `cpuManagerPolicyOptions` | `distribute-cpus-across-numa=true` | 跨 NUMA 均匀分布 pinned CPU |
| `featureGates` | `CPUManagerPolicyAlphaOptions=true` | 启用上述 alpha 选项 |

但这些策略对纯 Pod 节点有副作用：

- Pod 调度变严格（装不进单 NUMA → Pending）
- Guaranteed Pod 独占 CPU 核，其他 Pod 无法使用
- 节点有效 CPU 减少

因此不能集群级一刀切，需要按节点组区分。

---

## 2. Inventory 节点组结构

`hosts.yaml` 把 worker 节点拆成两个子组，`agent` 是父组：

```yaml
server:
  hosts:
    apollo:
    boreas:

pod:                    # 纯 Pod 节点（永不跑 VM）
  hosts:
    chaos:
    crios:

kubevirt:               # 可能跑 VM 的节点
  hosts:
    helios:
    hermes:

agent:                  # 父组 — k3s role 用 groups.agent 做 HA 检测
  children:
    pod:
    kubevirt:

cluster:
  children:
    server:
    agent:
```

**归属规则：**

- **永不跑 VM** → `pod` 组
- **可能跑 VM**（即使只是偶尔）→ `kubevirt` 组
  - kubevirt 预设对 Pod 也安全，反过来不行
- **不要同时放两个组** — 变量优先级会混淆

---

## 3. kubelet-arg 拼接逻辑

kubelet 参数由三部分拼接，在 `roles/k3s/tasks/facts.yaml` 中完成：

```
common_args + preset(pod|kubevirt) + override
```

### 3.1 common_args（所有节点共享）

定义在 `group_vars/all/main.yaml`：

```yaml
kubelet:
  common_args:
    - runtime-request-timeout=15m
    - container-log-max-files=3
    - container-log-max-size=10Mi
```

### 3.2 preset（按组自动选择）

```yaml
  # Pod 预设：空（kubelet 用内置默认值）
  pod: []

  # KubeVirt 预设：static CPU + restricted topology
  kubevirt:
    - cpu-manager-policy=static
    - topology-manager-policy=restricted
    - feature-gates=CPUManagerPolicyAlphaOptions=true
    - cpu-manager-policy-options=distribute-cpus-across-numa=true
```

选择逻辑（`facts.yaml`）：

```yaml
args: >-
  {%- if inventory_hostname in (groups.kubevirt | default([])) -%}
  {{- common_args + kubevirt_preset + kubevirt_kubelet_override + kubevirt_kubelet_override_host -}}
  {%- else -%}
  {{- common_args + pod_preset + pod_kubelet_override + pod_kubelet_override_host -}}
  {%- endif -%}
```

- 节点在 `kubevirt` 组 → 用 kubevirt 预设
- 其他节点（pod 组、server 节点）→ 用 pod 预设

### 3.3 override（两级追加）

override 分为组级和单机级，**追加**（非替换）到预设后面：

| 变量 | 放在哪 | 作用域 |
|---|---|---|
| `pod_kubelet_override` | `group_vars/pod.yaml` | 所有 pod 节点 |
| `pod_kubelet_override_host` | `host_vars/<node>.yaml` | 单个 pod 节点（追加） |
| `kubevirt_kubelet_override` | `group_vars/kubevirt.yaml` | 所有 kubevirt 节点 |
| `kubevirt_kubelet_override_host` | `host_vars/<node>.yaml` | 单个 kubevirt 节点（追加） |

**为什么需要 `_host` 后缀？**

Ansible 变量优先级：`host_vars > group_vars`。如果两者用同名变量，host_vars 会**替换** group_vars 的值。用不同变量名（`_host` 后缀）让两者在 `facts.yaml` 中显式拼接，实现追加效果。

**示例：**

```yaml
# group_vars/kubevirt.yaml — 所有 kubevirt 节点
kubevirt_kubelet_override:
  - reserved-cpus=0
  - memory-manager-policy=Static
  - system-reserved=memory=4Gi
  - kube-reserved=memory=4Gi
  - reserved-memory=0:memory=1Gi;1:memory=1Gi;2:memory=1Gi;3:memory=1Gi;4:memory=1Gi;5:memory=1Gi;6:memory=1Gi;7:memory=1Gi
```

```yaml
# host_vars/helios.yaml — 只对 helios（追加到上面的组级 override）
kubevirt_kubelet_override_host:
  - reserved-cpus=0,1
```

helios 最终的 kubelet-arg：

```
common_args + kubevirt_preset + [reserved-cpus=0, memory-manager-policy=Static, ...] + [reserved-cpus=0,1]
```

两组 override 都保留。

---

## 4. 渲染输出

kubelet-arg 最终渲染到 `/etc/rancher/k3s/config.yaml` 的 `kubelet-arg:` 节：

**Pod 节点：**

```yaml
kubelet-arg:
  - config=/etc/rancher/k3s/kubelet.yaml
  - runtime-request-timeout=15m
  - container-log-max-files=3
  - container-log-max-size=10Mi
```

**KubeVirt 节点：**

```yaml
kubelet-arg:
  - config=/etc/rancher/k3s/kubelet.yaml
  - runtime-request-timeout=15m
  - container-log-max-files=3
  - container-log-max-size=10Mi
  - cpu-manager-policy=static
  - topology-manager-policy=restricted
  - feature-gates=CPUManagerPolicyAlphaOptions=true
  - cpu-manager-policy-options=distribute-cpus-across-numa=true
```

`/etc/rancher/k3s/kubelet.yaml`（KubeletConfiguration 文件）只包含集群级设置（eviction、systemReserved），不包含 cpuManager/topologyManager 等 —— 这些全部通过 `kubelet-arg` CLI 参数传递。

---

## 5. 节点组切换流程

### 5.1 从 pod 切换到 kubevirt

1. **cordon + drain：**
   ```bash
   kubectl cordon <node>
   kubectl drain <node> --ignore-daemonsets --delete-emptydir-data
   ```

2. **在 `hosts.yaml` 中把节点从 `pod` 组移到 `kubevirt` 组**

3. **重跑 k3s role：**
   ```bash
   ansible-playbook provisioning.yaml -t k3s
   ```

4. **uncordon：**
   ```bash
   kubectl uncordon <node>
   ```

kubelet 会以新参数重启（static CPU + restricted topology）。第 1 步 drain 已经把工作负载驱逐走了。

### 5.2 从 kubevirt 切换到 pod

**这是危险方向** —— 节点可能正在跑 VM，kubelet 重启会杀掉它们。

流程相同，但 precheck 会在第 3 步之前拦截（见下文）。

---

## 6. Precheck 安全验证

`roles/k3s/tasks/precheck.yaml` 在 kubelet 配置应用前自动运行，检测 `cpu-manager-policy` 是否正在变化，如果变化且节点上有运行中的 VM，则 **fail** 阻止操作。

### 6.1 工作流程

```
1. 检查 server 上 kubeconfig 是否存在
   ├─ 不存在 → 跳过（首次部署）
   └─ 存在 → 继续

2. KubeVirt 是否启用？
   ├─ 否 → 跳过（无 VM 风险）
   └─ 是 → 继续

3. 读取目标节点当前 k3s config
   ├─ 文件不存在 → 跳过（新节点）
   └─ 存在 → 检测 cpu-manager-policy 是否变化

4. policy 变化？
   ├─ 否 → 跳过（幂等重跑，kubelet 不重启）
   └─ 是 → 委托 server 查询 API

5. server 查询 VMI（所有 namespace）
   └─ 过滤 status.nodeName == 当前节点 + phase in [Running, Scheduled]

6. 有 VM？
   ├─ 否 → 通过，继续 provisioning
   └─ 是 → FAIL，列出 VM 名称，给出迁移/drain 指令
```

### 6.2 API 查询由 server 节点执行

agent 节点（pod/kubevirt worker）没有 kubeconfig（`/etc/rancher/k3s/k3s.yaml` 只在 server 上）。precheck 通过 `delegate_to: k3s_map.server.default_host` 把 k8s API 查询委托给 server 节点执行。

### 6.3 FAIL 时的输出示例

```
ERROR: Node helios has 2 running VM(s) and its kubelet CPU policy is about
to change (static ↔ none). Kubelet will restart and kill these VMs.

Running VMs:
  - default/ubuntu-vm (phase: Running)
  - kubevirt/windows-vm (phase: Running)

FIXES:
  1. Migrate VMs to another node:
       virtctl migrate <vm-name> -n <namespace>
  2. Or drain the node (stops all VMs):
       kubectl cordon helios
       kubectl drain helios --ignore-daemonsets --delete-emptydir-data
  3. Re-run: ansible-playbook provisioning.yaml -t k3s
```

### 6.4 跳过条件

precheck 在以下情况自动跳过，不会阻塞部署：

| 场景 | 跳过原因 |
|---|---|
| 首次部署 | server 上没有 kubeconfig |
| KubeVirt 未启用 | `enable_kubevirt: false`，无 VM 风险 |
| 新节点 | k3s config 文件不存在 |
| 幂等重跑 | 当前 config 与目标 config 一致，policy 未变化 |

---

## 7. 完整示例

### 7.1 Pod 节点开 static CPU（不切组）

某些 Pod 节点跑延迟敏感型工作负载（数据库、ML 推理），需要 static CPU 但不需要 NUMA 亲和：

```yaml
# group_vars/pod.yaml
pod_kubelet_override:
  - cpu-manager-policy=static
```

topology manager 保持 `none`，Pod 拿到独占 CPU 但不强制 NUMA 对齐。

### 7.2 KubeVirt 节点加 memory manager

```yaml
# group_vars/kubevirt.yaml
kubevirt_kubelet_override:
  - memory-manager-policy=Static
  - system-reserved=memory=4Gi
  - kube-reserved=memory=4Gi
  - reserved-memory=0:memory=1Gi;1:memory=1Gi;2:memory=1Gi;3:memory=1Gi;4:memory=1Gi;5:memory=1Gi;6:memory=1Gi;7:memory=1Gi
```

### 7.3 单机 reserved_cpus（硬件相关）

```yaml
# host_vars/helios.yaml
kubevirt_kubelet_override_host:
  - reserved-cpus=0,1
```

只对 helios 生效，追加到组级 override 后面。其他 kubevirt 节点不受影响。

查询 NUMA topology 确定 reserved_cpus：

```bash
numactl --hardware    # CPU -> NUMA node 映射
lscpu -e              # 在线 CPU 列表
```
