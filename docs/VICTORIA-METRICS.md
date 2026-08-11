# VictoriaMetrics & VictoriaLogs 部署指南

本项目通过 [victoria-metrics-k8s-stack](https://github.com/VictoriaMetrics/helm-charts/tree/master/charts/victoria-metrics-k8s-stack) 与 [victoria-logs-single](https://github.com/VictoriaMetrics/helm-charts/tree/master/charts/victoria-logs-single) 两张 Helm chart 提供可观测栈。本文说明它们的默认行为、存储选型与常见运维场景。

- 前置：`enable_longhorn: true`（默认）；如果关闭，请先阅读文末的"更换存储后端"。
- 相关配置文件：`inventory/cluster/group_vars/all/globals-sample.yaml`（或中文版 `globals-sample.zh-CN.yaml`）中的 `可观测性 - VictoriaLogs` / `可观测性 - VictoriaMetrics` 两节。

> **重要概念澄清 —— 两种"副本"不要混淆**
>
> 本文会用到"副本"这个词两次，指的是**两件完全独立的事**：
>
> | 层次 | 关键词 | 作用 | 默认值 | 由谁负责 |
> |------|--------|------|-------|---------|
> | **存储副本** | `longhorn_replica_count` | 磁盘数据的物理冗余，防节点/磁盘丢失 | `2`（已开启） | Longhorn CSI |
> | **应用副本** | `victoria_metrics_cluster_enabled` | VictoriaMetrics 进程本身的横向扩展与 HA | `false`（未开启） | VM chart |
>
> - **默认情况下**：存储层 2 副本，应用层单进程（vmsingle）。数据不会因单节点磁盘故障丢失，但 vmsingle Pod 挂掉 → 服务短暂中断（等 Pod 重启）。
> - **想要应用层 HA**：**必须**显式设置 `victoria_metrics_cluster_enabled: true`，切换到 vmcluster 架构。
> - 两者是正交的：可以只要一个、可以都要、也可以都不要。详见下文 §6 应用层 HA。

---

## 1. 组件全景

VictoriaMetrics k8s-stack 是一个"打包套件"，会同时拉起多个组件；VictoriaLogs 相对简单，只有 server + 采集端。

### VictoriaMetrics 组件

| 组件 | 是否有 PVC | 默认副本 | 说明 |
|------|-----------|---------|------|
| **vmsingle** | ✅ 50Gi | 1 | 单节点 TSDB 存储（`victoria_metrics_cluster_enabled: false` 时使用） |
| **vmstorage** | ✅ 50Gi × N | N | HA 模式的分布式 TSDB（`cluster_enabled: true` 时使用） |
| **vmselect** | ⭕ 默认 emptyDir | 2 | 查询节点，仅在 HA 模式启用 |
| **vminsert** | ❌ 无 | 2 | 写入路由节点，仅在 HA 模式启用 |
| **vmagent** | ⭕ 默认 emptyDir | 1 | 抓取 ServiceMonitor 转发到 vmsingle/vminsert |
| **vmalert** | ❌ 无 | 1 | 告警规则执行器 |
| **AlertManager** | ⭕ 默认 emptyDir | 2 | 告警路由与静音 |
| **Grafana** | ✅ 5Gi | 1 | 仪表板前端 |
| **vm-operator** | ❌ 无 | 1 | 管理 vmsingle/vmcluster CR |
| **kube-state-metrics** | ❌ 无 | 1 | 集群对象指标 |
| **prometheus-node-exporter** | ❌ 无 | DaemonSet | 节点级 CPU/内存/磁盘指标 |

> ⭕ = chart 里 `storage.enabled: false`，默认走 emptyDir；数据只在 Pod 生命周期内保留。

### VictoriaLogs 组件

| 组件 | 是否有 PVC | 默认副本 | 说明 |
|------|-----------|---------|------|
| **vlsingle**（server） | ✅ 5Gi | 1 | 日志存储 + 查询接口 |
| **vector** | ❌ 无 | HPA 1–3 | 每节点抓取容器日志的 sidecar/agent |

---

## 2. 默认部署会发生什么

假设你什么都不改，只是 `enable_longhorn: true` + `enable_victoria_metrics: true` + `enable_victoria_logs: true`：

### 存储层

集群里会新增以下 PVC（均使用 `longhorn` StorageClass）：

| PVC | 大小 | Longhorn 存储副本 |
|-----|------|-----------------|
| `vmsingle-vmks-victoria-metrics-k8s-stack-0` | 50Gi | 2 |
| `grafana` | 5Gi | 2 |
| `server-victoria-logs-single-0` | 5Gi | 2 |

每个 PVC 由 **2 份 Longhorn 存储副本** 承载（`longhorn_replica_count: 2`），
副本按 `replica_auto_balance: least-effort` 策略自动分散到集群里的多个 Longhorn 节点。
这**只是磁盘层面的数据冗余**，跟 vmsingle / vlsingle 进程本身没关系 ——
它们**依旧是单进程**（应用副本 = 1）。

### 调度层

- **无 nodeSelector、无 tolerations** —— K8s 调度器根据资源余量、亲和/反亲和、拓扑分布约束自由挑节点。
- **vmsingle / vlsingle / Grafana** 都是单副本，各自随机落在一个 worker 上。
- **AlertManager** 副本数 2，chart 内置反亲和会尽量把两副本放到不同节点。
- **prometheus-node-exporter** 与 **vector** 是 DaemonSet，每个节点各起一个 Pod。

### 数据持久化行为

Longhorn `data_locality: best-effort` 生效：

1. Pod 首次调度时，Longhorn **尽力**在该节点上放一个副本 → 本地读取零跨节点开销
2. Pod 因节点故障 / drain / 升级被驱逐到另一台节点
3. 新节点上如果**没有本地副本**，Longhorn Engine 会通过 iSCSI 跨节点连远端副本 → **数据依然可读**，只是延迟略升
4. 稍后 Longhorn 会自动**迁移**一个副本到新节点（"eviction"），恢复本地性

这就是 Longhorn 让 VM 与 K8s 调度解耦的核心机制。

### 保留期 & 采集

- **Metrics**：72 小时（`victoria_metrics_retention`）
- **Logs**：7 天（`victoria_logs_retention`）
- **抓取间隔**：由 `victoriametrics_map.service.monitor.scrape.interval` 决定（默认 30s）
- **vmagent** 抓所有已启用的 ServiceMonitor（Cilium / CoreDNS / Longhorn 等 chart 已内建 ServiceMonitor）

### 网络暴露

- 默认 `enable_external_dns: false` → chart 里所有 Ingress 都被关闭（`values.j2` 里有 `if externaldns_vars.cloudflare.host.domain != 'disabled'` 守卫）
- 只能通过 `kubectl port-forward` 访问 Grafana / vmsingle / vlogs：

```bash
# Grafana
kubectl -n kube-system port-forward svc/vmks-grafana 3000:80
#   浏览器打开 http://localhost:3000
#   用户名: admin
#   密码:   $(python3 tools/secrets.py list | grep password_grafana_admin)

# vmsingle 查询接口
kubectl -n kube-system port-forward svc/vmsingle-vmks-victoria-metrics-k8s-stack 8428:8428
#   http://localhost:8428/vmui

# VictoriaLogs
kubectl -n kube-system port-forward svc/victoria-logs-single-server 9428:9428
#   http://localhost:9428/select/vmui
```

---

## 3. 存储选型

Sample 里给出了 4 个预设选项，行为差异总结如下：

| 选项 | StorageClass | 数据分布 | Pod 调度 | 适用场景 |
|------|-------------|---------|---------|---------|
| **A** | longhorn | 全集群自动均衡 | 自由 | 默认，多节点集群最省事 |
| **B** | longhorn（带 tag） | 3 台存储节点 | 固定到 3 台 | 生产推荐，隔离监控负载 |
| **C** | ceph-rbd | 外部 Ceph | 自由 | 已有 Ceph 集群时首选 |
| **D** | local-path | 单节点 hostPath | 必须钉死 | 单节点集群 / 开发环境 |

### 选择建议

**多节点集群 + 有 Longhorn** → **选项 A**  
默认配置。副本自动分布，Pod 自由漂移，Longhorn 会处理跨节点访问。

**多节点集群 + 希望隔离监控负载** → **选项 B**  
适合以下情况（推荐生产使用）：
- 集群规模较大（≥ 5 台节点），业务负载多样
- 希望 vmsingle 的内存/磁盘增长不影响业务 Pod
- 有几台节点适合专门承担有状态服务
- 详见 §4 "为什么建议给 VM 分配独立节点" 与 §5 "独立部署 VM 节点：完整步骤"

**已有 Ceph 集群** → **选项 C**  
适合以下情况：
- 集群外已经有稳定运行的 Ceph
- 希望和其他工作负载共用同一套存储
- 追求 3 副本 + CRUSH 策略级别的可靠性

**单节点集群** → **选项 D**  
只有这种情况可以用 hostPath。**多节点用 local-path 是错误的** —— Pod 会被 PV 的
`nodeAffinity` 强制拉回原节点，一旦该节点故障就永远起不来。详见 §8 "local-path
的真实风险"。

---

## 4. 为什么建议给 VM 分配独立节点

在讨论"存储选型"和"细节配置"之前，先说明本项目对 VM 部署位置的核心建议：

**VictoriaMetrics 与 VictoriaLogs 是本集群里"资源占用最重、增长最不可控"的
系统服务，生产环境建议把它们固定到 2~3 台独立节点上运行。**

### 资源占用对比

| 组件 | 内存 | 磁盘 | 增长趋势 |
|------|------|------|----------|
| **vmsingle** | 1~8+ GiB | 50 GiB+ PVC | 随 series 数量线性增长 |
| **vlsingle** | 500 MiB~4 GiB | 5~50 GiB PVC | 随日志量增长 |
| Grafana | 200~500 MiB | 5 GiB PVC | 稳定 |
| Cilium agent | 200~500 MiB | 无 | 稳定 |
| KubeVirt operator | 100~300 MiB | 无 | 稳定 |
| CoreDNS / cert-manager / kured 等 | < 200 MiB | 无 | 稳定 |

VM 一家占了整个可观测栈**约 90% 的资源**。其他系统服务（Cilium、CoreDNS、
KubeVirt 等）资源占用小且稳定，可以和业务负载共存；但 VM 的写入速率、
retention、series 基数都可能随集群规模持续增长。

### 不隔离的后果

如果不做节点亲和，K8s 调度器会把 vmsingle 随便挑一台节点：

- **该节点内存吃紧**：即使 vmsingle 有 `limits: 8Gi` 保护自己不 OOMKilled，
  它仍然会占用 CPU、磁盘 IOPS、网络带宽 —— 挤压同节点上业务 Pod 的响应
- **Longhorn 副本重压**：vmsingle 持续大量写入 → 该节点的 Longhorn 副本
  IOPS 打满 → 同节点其他 PVC 变慢
- **抓取放大效应**：随着集群 Pod 数增长，vmagent 抓取指标 → vmsingle 内存/
  磁盘持续上涨 → 排挤业务 Pod 的资源余量
- **节点重启风险**：任何一台节点重启都会触发 vmsingle 迁移；如果 VM
  分布不确定，很难提前规划维护窗口

### 隔离带来的好处

把 VM 固定到 2~3 台"可观测栈专用节点"后：

- **业务节点资源可预测**：业务 Pod 不用和 vmsingle 抢内存/IOPS
- **VM 增长有界**：只影响可观测节点，触及上限时告警清晰、易于扩容
- **维护窗口可控**：升级 VM / 扩容 Longhorn 只影响这几台机器
- **成本可测**：清楚知道观测栈用了多少资源

### 需要几台节点？

| 集群规模 | 建议 | 说明 |
|---------|------|------|
| 单节点 lab | 不做隔离 | 只有一台机器，谈不上隔离 |
| 3~5 台节点 | 可以不做隔离 | 集群本身不大，全共享也够用；但预留 1 台跑观测更稳 |
| ≥ 5 台节点 | **强烈推荐**独立 2~3 台 | 观测节点独占，业务节点纯净 |

### 怎么做

`globals.yaml` 里配置 `victoria_metrics_node_selector` /
`victoria_logs_node_selector`，配套 K8s 节点 label。详细步骤见下一节。

---

## 5. 独立部署 VM 节点：完整步骤

假设你挑选了 3 台节点专门跑可观测栈（节点名任意，这里以 `<node1>` `<node2>`
`<node3>` 表示）：

### 第 1 步：给节点打 label（K8s 层）

```bash
# label 的 key/value 完全由你自定义，这里以 role=observability 为例。
# 之后 globals.yaml 里的 node_selector 要和这里保持一致。
for n in <node1> <node2> <node3>; do
  kubectl label node "$n" role=observability
done
```

### 第 2 步（可选）：让 Longhorn 副本也只落在这几台节点

如果你希望 vmsingle / vlsingle 的 Longhorn 副本也集中在这几台节点上（避免
观测数据的磁盘 IO 打到业务节点）：

```bash
# Longhorn tag 名称任意；示例用 "data"
for n in <node1> <node2> <node3>; do
  longhornctl update node "$n" --tags data
done
```

无 `longhornctl` 时，可在 Longhorn UI 里逐台节点添加 tag，或用 kubectl 直接
改 CRD：

```bash
kubectl -n longhorn-system patch node.longhorn.io <node1> \
  --type merge -p '{"spec":{"tags":["data"]}}'
```

### 第 3 步：修改 `globals.yaml`

```yaml
# --- Longhorn 副本侧（可选，仅当做了第 2 步时才需要） ---
longhorn_replica_node_tag: "data"

# --- VictoriaMetrics Pod 侧 ---
victoria_metrics_node_selector:
  role: observability                    # 与第 1 步打的 label 保持一致

# --- VictoriaLogs Pod 侧 ---
victoria_logs_node_selector:
  role: observability
```

### 第 4 步：应用配置

```bash
ansible-playbook provisioning.yaml -t longhorn         # 若改了 replica_node_tag
ansible-playbook provisioning.yaml -t victoria-metrics
ansible-playbook provisioning.yaml -t victoria-logs
```

### 第 5 步：验证

```bash
# 确认所有 VM 组件都调度到了目标节点
kubectl -n kube-system get pod -l app.kubernetes.io/instance=vmks -o wide
kubectl -n kube-system get pod -l app.kubernetes.io/name=victoria-logs-single -o wide

# 若做了第 2 步：确认 Longhorn 副本只在带 tag 的节点上
kubectl -n longhorn-system get replicas.longhorn.io -o wide | grep vmsingle
```

---

## 6. 应用层 HA（vmcluster）

> ⚠️ 前面所有内容讨论的都是**存储层**（Longhorn 副本）。这一节讨论的是**应用层** ——
> VictoriaMetrics 进程本身的横向扩展与故障切换。

### 默认部署（vmsingle）为什么不算 HA？

默认 `victoria_metrics_cluster_enabled: false`：

- 只有 **1 个** `vmsingle` StatefulSet Pod
- 所有写入 / 查询都必须过这个进程
- 该 Pod 挂掉 → 查询失败、写入积压在 vmagent 侧
- 数据不会丢（Longhorn 副本还在），但服务会中断一段时间（直到 Pod 被 K8s 重启）

对绝大多数场景（< 100 万活跃 series）这**已经够用**，因为：
- Longhorn 保护了数据安全
- Pod 重启一般 30 秒内完成
- vmagent 有本地缓冲，短暂中断不会丢数据点

### 什么时候需要显式启用 vmcluster

**性能上界触顶**：

- 抓取目标 Pod 数量 > 500
- 每秒采集样本数 > 100k
- 活跃 series > 100 万

**应用层容灾要求**：

- 不接受单个 Pod 挂掉导致的短暂读写中断
- 需要跨 AZ / 跨机架部署，容忍机架级故障
- 需要读写路径解耦（vmselect / vminsert 独立扩缩）

### 启用方法

**必须显式开启**（不会自动切换）：

```yaml
victoria_metrics_cluster_enabled: true
```

启用后 chart 会**替换整个应用架构**：

| | vmsingle 模式（默认） | vmcluster 模式 |
|-|--------------------|--------------|
| 写入组件 | vmagent → vmsingle | vmagent → vminsert × 2 → vmstorage × N |
| 查询组件 | vmsingle | vmselect × 2 → vmstorage × N |
| 有状态 Pod | 1（vmsingle） | N（vmstorage） |
| 无状态 Pod | 0 | 4（vminsert × 2 + vmselect × 2） |
| PVC 数量 | 1（vmsingle） | N（每个 vmstorage 一个） |
| replication_factor | N/A | 2（默认，每个数据点写入 2 个 vmstorage 副本） |
| 应用层 HA | ❌ | ✅ |
| 资源占用 | 小 | 大（至少多 4 个 Pod） |

`vmsingle` 会**完全不部署**。

### vmcluster 里的三层"副本"

如果你启用了 vmcluster + Longhorn，实际上会有**三层冗余**同时存在：

1. **Longhorn 存储副本**（默认 2）—— 每个 vmstorage PVC 的磁盘冗余
2. **应用复制因子** `replication_factor: 2` —— 每个数据点写入 2 个 vmstorage 分片
3. **无状态副本** —— vminsert × 2、vmselect × 2 承担写入/查询路由

举例：假设 `vmstorage.replicas: 3` + `replication_factor: 2` + `longhorn_replica_count: 2`：
- 一个数据点在**应用层**写入 3 个 vmstorage 中的 2 个
- 每个 vmstorage 的磁盘数据又被 Longhorn 复制 2 份到不同节点
- 总物理拷贝数 = 2 × 2 = **4 份**

这在生产环境是可接受的，但要注意存储成本。如果用 Ceph（自带副本），可以把
`longhorn_replica_count: 1` 或 `replication_factor: 1` 中的一个降到 1 避免过度冗余。

### 与独立部署 VM 节点的配合

- `victoria_metrics_node_selector` 会**同时**作用于 vmstorage / vmselect / vminsert（三个组件的模板里都注入了）
- `longhorn_replica_node_tag` 作用于所有走 default longhorn SC 的 PVC，vmstorage 的 PVC 也走同一 SC

### 副本数怎么调

`victoriametrics_vars.kubernetes.vmcluster.vmstorage.replicas` 默认为 1，
在生产环境请至少改成 3（让 `replication_factor: 2` 有意义）：

```yaml
# roles/victoria-metrics/defaults/main.yaml（当前需要改 role defaults）
vmcluster:
  vmstorage:
    replicas: 3
```

后续可以考虑把它提升为顶层 override 变量。

---

## 7. 常见问题

### Q1: vmsingle Pod 一直 Pending

**症状**：`kubectl describe pod vmsingle-...` 里显示 `0/5 nodes are available: ... pod has unbound immediate PersistentVolumeClaims`。

**排查**：

```bash
# 看 PVC 状态
kubectl -n kube-system get pvc | grep vmsingle
# 应该是 Bound；如果是 Pending：

# 看 PV 分配失败原因
kubectl -n kube-system describe pvc <pvc-name>
```

常见原因：
- Longhorn 副本无法满足（节点不够 / disk 空间不够）
- 用了 `longhorn_replica_node_tag` 但节点没打 tag
- `victoria_metrics_node_selector` 指向的节点不存在或没有相应 label

### Q2: Grafana 打开后 Dashboard 无数据

**排查步骤**：

```bash
# 1. vmagent 是否在抓
kubectl -n kube-system logs -l app.kubernetes.io/name=vmagent --tail=50 | grep -i error

# 2. vmsingle 是否有数据落盘
kubectl -n kube-system exec -it vmsingle-vmks-victoria-metrics-k8s-stack-0 -- \
  wget -qO- 'http://localhost:8428/api/v1/query?query=up' | head

# 3. Grafana datasource 配置对不对
#    默认应该指向 http://vmsingle-vmks-victoria-metrics-k8s-stack:8428
```

### Q3: 节点重启后 vmsingle Pod 卡在 ContainerCreating

**根因**：Longhorn 卷在跨节点重挂时需要 `nodeDrainPolicy` / detach 完成，如果节点重启不干净会残留 `AttachOrDetach failed`。

**处理**：

```bash
# 看 volume 状态
kubectl -n longhorn-system get volumes.longhorn.io | grep vmsingle

# 强制 detach（谨慎）
kubectl -n longhorn-system patch volumes.longhorn.io <volume-name> \
  --type merge -p '{"spec":{"nodeID":""}}'
```

日常预防：让节点走 `kured` 有序重启，避免硬关机。

### Q4: 关掉 Longhorn 后 VM 部署失败

**症状**：`enable_longhorn: false` 后，`victoria_metrics_storage_class` 自动派生
为 `local-path`。此时 vmsingle Pod 会被 local-path PV 强制绑到某台节点，一旦该
节点故障就永远无法恢复；节点磁盘故障则数据永久丢失。

**处理**：

见 §8 "local-path 的真实风险" 与 §9 "更换存储后端"。生产场景应该切到 Longhorn
（选项 A/B）或外部 Ceph（选项 C），而不是继续用 local-path。

---

## 8. local-path 的真实风险

选项 D（local-path）看似"最简单"—— 只需要 K3s 内置的 local-path-provisioner，
无需任何分布式存储。但它有一个**根本性**的隐患，理解清楚再决定是否用它。

### 认识：Pod 路径不会变，PV 会被绑死

Pod 内部的挂载路径由 Pod spec 里的 `volumeMounts.mountPath` 决定（比如 vmsingle
永远挂 `/vm-data`），Pod 重建后 spec 不变，**Pod 视角**看到的路径永远稳定。

真正的问题在 PV 层。当 vmsingle 第一次申请 PVC 时，local-path-provisioner 做了
以下动作：

1. 挑一台节点（如 `node1`），在它的本地文件系统上开一个目录
   `/var/lib/rancher/k3s/storage/pvc-abc123_default_data`
2. 生成一个 PV，携带 `nodeAffinity: kubernetes.io/hostname=node1`
3. PVC 状态变成 `Bound`，从此**这个 PV 被永久绑到 node1 上**

后果：Pod 重建时 K8s 一看 PV 的 nodeAffinity，就会**强制**把 Pod 调度回 node1，
即使你没配 `nodeSelector`。

### 风险清单

| 场景 | 数据 | 服务 |
|------|------|------|
| Pod 重启（同节点） | ✅ 不丢 | ✅ 秒级恢复 |
| Pod 被 K8s 重建（不同 Pod name，同 PVC） | ✅ 不丢 | ✅ 恢复 |
| StatefulSet 副本扩缩 | ✅ 每个 ordinal 独立 PVC | ✅ 各自绑各自节点 |
| **单节点集群整机重启** | ✅ 不丢 | ✅ 节点回来即恢复 |
| **多节点集群，PV 所在节点故障 / cordon / 报废** | ⚠️ 数据在磁盘上还在，**但取不回来** | ❌ Pod 永远 Pending |
| **PV 所在节点磁盘物理损坏** | ❌❌ **永久丢失** | ❌ 无法恢复 |
| **手误 `kubectl delete pv`** | ❌❌ 永久丢失 | ❌ 无法恢复 |

对比 Longhorn：磁盘坏一块 → 从其他副本恢复；节点报废 → 换台机器加入集群，
Longhorn 自动重建副本；备份到 CIFS/S3 → 灾难场景也能拉回。

### 为什么"钉死节点"也不安全

`victoria_metrics_node_selector: {kubernetes.io/hostname: node1}` 只是让 Pod
永远调度到 node1 —— 但这本来就是 local-path PV 会自动做的事。它**没有**给
数据加任何保护：

- node1 磁盘坏 → 数据没了
- node1 换成新机器 → 数据没了（新磁盘目录是空的）
- 想加台新节点分担负载 → 做不到，PV 绑死在 node1

**"钉死节点"只解决了 Pod 调度问题，没解决数据可靠性问题。**

### 真正安全的做法

local-path 只适用于以下情况：

- **临时实验**：数据丢了不心疼
- **开发环境**：本地跑一天两天，重装成本可接受
- **单节点 lab**：整个集群就一台机器，本来也没有分布式存储可选

对**生产**的 vmsingle / vlsingle / Grafana，请务必：

- 选 Longhorn（选项 A/B）或外部 Ceph（选项 C）
- 或者接受"vmsingle 属于易失存储"的假设，配合 `longhorn_backup_target` 定期
  备份到外部（CIFS/S3），最坏情况下从备份恢复

一句话：**local-path 不是"简易的持久化"，它是"节点绑定的临时存储"**。

---

## 9. 更换存储后端

### 换到外部 Ceph

前提：`enable_ceph_csi: true`，且 `ceph_csi_monitors` / `credential_ceph_admin_key` 已配好（见 [docs/CONFIGURATION.md](CONFIGURATION.md) 中 Ceph CSI 节）。

```yaml
enable_longhorn: false                           # 可选，也可以两者共存
enable_ceph_csi: true
ceph_csi_rbd_pool: "rbd-pool"

victoria_metrics_storage_class: "ceph-rbd"
victoria_metrics_storage_size: "100Gi"
victoria_metrics_aux_storage_size: "10Gi"

victoria_logs_storage_class: "ceph-rbd"
victoria_logs_storage_size: "10Gi"
```

因为 RBD 支持任意节点挂载，**不需要**配置 `node_selector`。

### 换到 local-path（仅单节点）

> ⚠️ 阅读本节前请先看 §8 "local-path 的真实风险"，理解你在放弃什么。

```yaml
enable_longhorn: false                           # 关掉 Longhorn，避免额外资源占用

victoria_metrics_storage_class: "local-path"
victoria_metrics_node_selector:
  kubernetes.io/hostname: node1                  # 显式钉死到唯一节点

victoria_logs_storage_class: "local-path"
victoria_logs_node_selector:
  kubernetes.io/hostname: node1
```

**多节点集群绝对不要用 local-path**：即使加了 `nodeSelector`，PV 也会被 local-path
的 nodeAffinity 绑到某台节点，该节点故障时整套观测栈直接不可用，节点磁盘损坏
时数据永久丢失。

### 迁移已有数据（Longhorn → Ceph）

1. 关闭 vmagent 停止新数据写入：`kubectl -n kube-system scale --replicas=0 statefulset/vmagent-vmks-victoria-metrics-k8s-stack`
2. 触发 vmsingle 落盘：`kubectl -n kube-system exec vmsingle-... -- wget -qO- 'http://localhost:8428/internal/force_flush'`
3. `kubectl -n longhorn-system` 里做一次 backup（备份到 CIFS/S3）
4. 修改 `globals.yaml` 换 SC，重新部署 vmsingle
5. 从新 PVC 里用 `vmctl` 导入历史数据（需要临时挂载旧 volume）

日常场景一般不做这种迁移 —— 直接换个 SC 从头开始积累是最省事的。

---

## 10. 参考

- VictoriaMetrics 官方文档：<https://docs.victoriametrics.com/>
- victoria-metrics-k8s-stack 参数：<https://github.com/VictoriaMetrics/helm-charts/tree/master/charts/victoria-metrics-k8s-stack#parameters>
- VictoriaLogs 官方文档：<https://docs.victoriametrics.com/victorialogs/>
- 本项目 role：
  - [`roles/victoria-metrics/`](../roles/victoria-metrics/)
  - [`roles/victoria-logs/`](../roles/victoria-logs/)
