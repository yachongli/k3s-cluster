# Longhorn 存储原理与使用指南

本项目通过 [Longhorn](https://longhorn.io/) 提供分布式块存储，作为 Kubernetes
集群的默认 StorageClass。本文说明其工作原理、数据路径，以及
VictoriaMetrics / VictoriaLogs 如何使用它。

- 相关配置：`globals.yaml` 中 `存储 - Longhorn` 一节
- Role 文件：`roles/longhorn/`
- 默认 StorageClass：`longhorn`（`enable_longhorn: true` 时自动设为默认）

> **重要：两种"副本"不要混淆**
>
> | 层次 | 关键词 | 作用 | 默认值 | 由谁负责 |
> |------|--------|------|-------|---------|
> | **存储副本** | `longhorn_replica_count` | 磁盘数据的物理冗余 | `2` | Longhorn |
> | **应用副本** | `victoria_metrics_cluster_enabled` | VM 进程的横向扩展 | `false` | VM chart |
>
> 本文只讨论**存储副本**。应用层 HA 见 [VICTORIA-METRICS.md](./VICTORIA-METRICS.md)。

---

## 1. Longhorn 是什么

Longhorn 是专为 Kubernetes 设计的**分布式块存储**（Distributed Block Storage）。
每个卷 = 一个独立的存储引擎 + N 个副本，以 Pod 形式运行在集群节点上。

**核心特征：**

| 特性 | 说明 |
|------|------|
| 架构 | 每个卷 = 1 个 Engine + N 个 Replica，都是用户态进程 |
| 前端 | iSCSI（v1 引擎，默认）/ NVMe-oF（v2 引擎，可选） |
| 副本 | 同步复制到多个节点，写完成 = 所有副本确认 |
| 快照 | 增量快照，Copy-on-Write 链 |
| 备份 | 增量备份到 CIFS / NFS / S3 |
| 精简置备 | 稀疏文件，按需分配 |
| 动态供应 | StorageClass 自动创建 PV |

---

## 2. 组件全景

Longhorn 部署后在 `kube-system` 和 `longhorn-system` 两个 namespace 中运行：

### kube-system（数据面）

| 组件 | 类型 | 每节点 | 作用 |
|------|------|--------|------|
| **longhorn-manager** | DaemonSet | ✅ | 大脑：管理卷 CRUD、副本调度、节点监控 |
| **longhorn-csi-plugin** | DaemonSet | ✅ | 翻译器：对接 K8s CSI，把 PVC 请求翻译成 Longhorn API |
| **longhorn-driver-deployer** | Deployment | 1 | 安装器：部署 CSI 侧车组件 |
| **longhorn-ui** | Deployment | 2 | Web 管理界面 |

### longhorn-system（控制面）

| 组件 | 作用 |
|------|------|
| **instance-manager** | 运行在 longhorn-manager pod 内部，管理 engine/replica 进程 |
| **engine** | 每卷 1 个，iSCSI target，接收写请求并同步到所有 replica |
| **replica** | 每卷 N 个（=副本数），实际存储数据到本地磁盘 |

> **注意**：instance-manager、engine、replica **不是独立 Pod**。
> 它们是 longhorn-manager pod 内的子进程，`kubectl get pods` 看不到。
> 用 `kubectl get engines.longhorn.io` 和 `kubectl get replicas.longhorn.io` 查看。

---

## 3. 工作原理

### 3.1 创建 PVC 全流程

```mermaid
flowchart TD
    PVC["用户创建 PVC<br/>StorageClass=longhorn, size=50Gi"]
    S1["1. K8s 调度器决定 Pod → k8s-test-2<br/>kubelet 发现 Pod 需要 PVC"]
    S2["2. CSI Provisioner → longhorn-csi-plugin<br/>创建一个 50Gi 的卷，2 副本"]
    S3["3. longhorn-manager 调度副本"]
    S3F["筛选条件（按顺序）：<br/>① 节点是否 cordon/drain？<br/>② 节点是否有 longhorn 磁盘？<br/>③ 磁盘空间是否足够？<br/>④ replica_node_tag 是否匹配？<br/>⑤ data_locality=best-effort<br/>⑥ replica_auto_balance=least-effort"]
    S3R["结果：<br/>Replica-1 → k8s-test-2（Pod 同节点）<br/>Replica-2 → k8s-test-1（另一节点）"]
    S4["4. longhorn-manager 创建 Engine<br/>Engine 固定在 Pod 所在节点（k8s-test-2）<br/>iSCSI target 必须和 Pod 同节点（本地回环）"]
    S5["5. CSI Attacher 挂载卷<br/>iSCSI initiator → iSCSI target (Engine)<br/>→ /dev/disk/by-path/... → Pod 容器"]

    PVC --> S1 --> S2 --> S3
    S3 --> S3F --> S3R --> S4 --> S5
```

### 3.2 写入数据路径

```mermaid
flowchart TD
    Pod["Pod 写入数据<br/>把 4KB 写到 offset 0x1000"]
    ISI["iSCSI initiator<br/>内核, 本地回环"]
    ENG["Engine (k8s-test-2, Pod 同节点)<br/>接收 iSCSI 写请求"]

    R1["Replica-1 (k8s-test-2, 本地)<br/>pwrite(volume-head.img,<br/>offset=0x1000, data=...)"]
    R2["Replica-2 (k8s-test-1, 远端)<br/>TCP [offset + data]<br/>pwrite(volume-head.img,<br/>offset=0x1000, data=...)"]
    ACK["Engine 等所有 Replica 确认<br/>→ 回复 Pod '写完成'"]

    Pod --> ISI --> ENG
    ENG -- "TCP (端口 9500+)<br/>块级写入指令" --> R1
    ENG -- "TCP (端口 9500+)<br/>块级写入指令" --> R2
    R1 --> ACK
    R2 --> ACK
```

**关键点：**
- 传输的是 `[offset + data]` 块级写入指令，**不是文件传输**
- 类似 RAID 1 的网络镜像
- 同步复制：Engine 等所有 Replica 确认后才回复 Pod
- 写性能 = 最慢的 Replica 的响应时间

### 3.3 读取数据路径

```mermaid
flowchart LR
    Pod["Pod 读取数据<br/>读 offset 0x1000, 4KB"]
    ISI["iSCSI initiator<br/>内核, 本地回环"]
    ENG["Engine (k8s-test-2)"]
    R1["Replica-1 (k8s-test-2, 本地)<br/>✅ 优先读, 无网络开销"]
    R2["Replica-2 (k8s-test-1, 远端)<br/>⚠️ 仅 Replica-1 不可用时<br/>跨网络读"]

    Pod --> ISI --> ENG
    ENG -- "优先" --> R1
    ENG -. "fallback" .-> R2
```

读默认走同节点的 Replica，不产生跨网络流量。

---

## 4. 本地存储格式

每个 Replica 在节点的 `/var/lib/longhorn/replicas/<pvc-id>-<hash>/` 下存储数据：

```
/var/lib/longhorn/replicas/pvc-1af59d16-...-08b93d81/
├── volume-head-002.img              ← 活跃写入层（当前写操作的目标）
├── volume-head-002.img.meta         ← head 元数据
├── volume-snap-1849776d-...img      ← 快照层（只读）
├── volume-snap-1849776d-...img.meta ← 快照元数据
└── volume.meta                       ← 卷级元数据
```

**稀疏文件：**
```
$ ls -lh volume-head-002.img
-rw-r--r-- 1 root root 50G    ← 虚拟大小（卷容量）

$ du -sh volume-head-002.img
3.2G                         ← 实际磁盘占用（只算写过的块）
```

`.img` 文件是稀疏文件，`ls` 显示虚拟大小，`du` 显示实际占用。

---

## 5. 快照（Snapshot）机制

### 5.1 Copy-on-Write 链

```mermaid
flowchart TD
    subgraph "创建快照时"
        OLD["volume-head-002.img<br/>当前活跃写入层"]
        SNAP["volume-snap-002.img<br/>冻结为只读"]
        NEW["volume-head-003.img<br/>新建空, 可写"]
        OLD -->|冻结| SNAP
        OLD2[" "] -.->|新建| NEW
    end

    subgraph "读 offset=0x1000（回溯链）"
        RH["1. 查 head-003"] -->|有?| RET1["返回数据"]
        RH -->|没有| RS2["2. 查 snap-002"]
        RS2 -->|有?| RET2["返回数据"]
        RS2 -->|没有| RS1["3. 查 snap-001"]
        RS1 -->|有?| RET3["返回数据"]
        RS1 -->|没有| ZERO["4. 返回零块（未写过）"]
    end

    subgraph "写 offset=0x1000"
        WH["直接写 head-003<br/>snap 永远不可变"]
    end
```

### 5.2 快照的作用

| 作用 | 说明 |
|------|------|
| **增量备份** | 备份只传相邻 snap 之间变化的块，不传整个卷 |
| **一致性快照** | 快照是只读的，备份时不受并发写入影响 |
| **副本重建** | 副本恢复时只传差异块，不重传整个卷 |
| **卷回滚** | revert 到某个 snap，丢弃之后的写入 |

### 5.3 备份流程

```mermaid
flowchart TD
    TRIG["触发备份<br/>手动或定时任务"]
    S1["1. 当前 head 冻结为 snap<br/>只读一致性点"]
    S2["2. 新建空 head<br/>Pod 继续写入, 不受影响"]
    S3["3. 从 snap 读取数据<br/>增量传输到备份目标 (CIFS)<br/>只传上次备份后变化的块"]
    S4["4. 备份完成<br/>snap 标记为已备份<br/>autoCleanup → 自动清理"]

    TRIG --> S1 --> S2 --> S3 --> S4
```

---

## 6. 默认配置

本项目 `globals.yaml` 中的默认值：

| 配置 | 默认值 | 说明 |
|------|--------|------|
| `longhorn_replica_count` | `2` | 每个卷 2 份数据副本 |
| `longhorn_backup_enabled` | `true` | 开启备份 |
| `longhorn_backup_target` | `cifs://nas.noty.cc/backup` | 备份目标（用户需修改） |
| `longhorn_data_locality` | `best-effort` | 尽量在 Pod 节点放副本 |
| `longhorn_replica_node_tag` | `""` | 不限制副本节点 |
| 存储路径 | `/var/lib/longhorn/` | 每个节点的数据目录 |
| 引擎版本 | `v1`（iSCSI） | 默认数据引擎 |
| 自动均衡 | `least-effort` | 尽量均匀但少迁移 |
| 节点驱逐 | `block-for-eviction` | drain 时先迁移副本 |
| 快照清理 | `auto` | 系统/备份产生的 snap 自动清理 |

---

## 7. VictoriaMetrics / VictoriaLogs 如何使用 Longhorn

### 7.1 组件 PVC 一览

| 组件 | PVC 大小 | 用途 | StorageClass |
|------|---------|------|-------------|
| **vmsingle** | 50Gi | TSDB 时序数据存储 | longhorn |
| **grafana** | 5Gi | 仪表盘、用户、配置 | longhorn |
| **vlsingle** (VictoriaLogs) | 5Gi | 日志存储 | longhorn |
| vmagent | 无（emptyDir） | 临时缓冲，无需持久化 | — |
| vmalert | 无 | 无状态规则引擎 | — |
| alertmanager | 5Gi (默认禁用) | 告警静音状态 | longhorn |

### 7.2 数据布局（2 节点集群示例）

```mermaid
flowchart LR
    subgraph "k8s-test-2 (Pod 所在节点)"
        ENG1["Engine<br/>vmsingle"]
        ENG2["Engine<br/>grafana"]
        R1A["Replica-1 (本地)<br/>pvc-1af59d16"]
        R2A["Replica-1 (本地)<br/>pvc-4718b8e2"]
        ENG1 --> R1A
        ENG2 --> R2A
    end

    subgraph "k8s-test-1"
        R1B["Replica-2 (远端)<br/>pvc-1af59d16<br/>50Gi 稀疏, 实占 ~3-29G"]
        R2B["Replica-2 (远端)<br/>pvc-4718b8e2<br/>5Gi 稀疏, 实占 <1G"]
        ENG1 -. "TCP 复制" .-> R1B
        ENG2 -. "TCP 复制" .-> R2B
    end
```

每个 Engine 固定在 Pod 所在节点，2 个 Replica 分布在两个节点上保证数据冗余。

### 7.3 写入路径（VictoriaMetrics 视角）

```mermaid
flowchart TD
    KUBE["kubelet /metrics"]
    AGENT["vmagent 抓取<br/>缓冲到 emptyDir (不走 Longhorn)"]
    SINGLE["vmsingle (k8s-test-2)<br/>写入 TSDB 数据"]
    PVC["vmsingle PVC<br/>/dev/disk/by-path/... (iSCSI 块设备)"]
    ENG["Engine (k8s-test-2)<br/>同步到 2 个 Replica"]
    R1["Replica-1 (k8s-test-2, 本地)<br/>pwrite → /var/lib/longhorn/.../volume-head.img"]
    R2["Replica-2 (k8s-test-1, 远端)<br/>TCP [offset+data] → pwrite → /var/lib/longhorn/.../volume-head.img"]

    KUBE --> AGENT --> SINGLE --> PVC --> ENG
    ENG --> R1
    ENG --> R2
```

### 7.4 故障场景

**场景 1：k8s-test-1 磁盘故障**

```mermaid
flowchart TD
    R2["Replica-2 (k8s-test-1) 丢失"]
    DET["Longhorn 检测到副本数 < 期望值"]
    RB["在 k8s-test-2 上重建副本<br/>或等待 k8s-test-1 恢复后增量同步"]
    OK["vmsingle 继续运行<br/>Engine 和 Replica-1 都在 k8s-test-2<br/>数据不丢, 服务不中断"]

    R2 --> DET --> RB --> OK
```

**场景 2：k8s-test-2 宕机（Pod 所在节点）**

```mermaid
flowchart TD
    DOWN["vmsingle Pod 挂掉<br/>Engine 挂掉<br/>Replica-1 (k8s-test-2) 不可用"]
    SCHED["K8s 在 k8s-test-1 上重新调度 vmsingle Pod"]
    NEWENG["Longhorn 在 k8s-test-1 创建新 Engine<br/>从 Replica-2 (k8s-test-1, 本地) 读取数据"]
    RECOVER["Pod 恢复<br/>数据从 Replica-2 提供<br/>数据完整"]

    DOWN --> SCHED --> NEWENG --> RECOVER
```

**场景 3：全部节点宕机**

```mermaid
flowchart TD
    ALL["所有 Replica 丢失"]
    BACKUP["从备份目标 (CIFS) 恢复"]
    NEWVOL["创建新卷 → 从备份恢复数据 → Pod 挂载"]

    ALL --> BACKUP --> NEWVOL
```

---

## 8. 常见运维操作

### 查看卷状态

```bash
# 所有卷
kubectl get volumes.longhorn.io -n longhorn-system

# 引擎（每个卷一个，显示在哪个节点）
kubectl get engines.longhorn.io -n kube-system

# 副本（每个卷 N 个，显示分布）
kubectl get replicas.longhorn.io -n kube-system

# PVC
kubectl -n kube-system get pvc
```

### 查看实际磁盘占用

```bash
# 每个节点的 Longhorn 数据
du -sh /var/lib/longhorn/replicas/*/

# 单个卷
du -sh /var/lib/longhorn/replicas/pvc-1af59d16-*/
```

### 限制副本到指定节点

```yaml
# globals.yaml
longhorn_replica_node_tag: "storage"
```

```bash
# 给节点打 tag
longhornctl update node k8s-test-1 --tags storage
longhornctl update node k8s-test-2 --tags storage
```

### 更换存储后端

```yaml
# globals.yaml — 关闭 Longhorn，用 local-path
enable_longhorn: false
# 所有组件自动切换到 local-path StorageClass
```

```yaml
# 或切换到外部 Ceph
enable_longhorn: false
enable_ceph_csi: true
cluster_storage_class: "ceph-rbd"
```

---

## 9. 与其他存储方案对比

| | Longhorn | Ceph CSI | local-path |
|---|---------|----------|------------|
| 类型 | 分布式块存储 | 分布式块存储 | 本地目录 |
| 数据冗余 | 内置（应用层复制） | 内置（底层复制） | 无 |
| 节点故障 | 数据不丢 | 数据不丢 | 数据丢 |
| 写性能 | 中（同步复制） | 中（同步复制） | 高（无开销） |
| 部署 | Helm chart，简单 | 需要外部 Ceph 集群 | K3s 内置 |
| 适合 | 中小规模集群 | 大规模、已有 Ceph | 临时/单节点 |
| 备份 | 内置 CIFS/NFS/S3 | Ceph 自身 | 无 |

---

## 10. 相关配置

| 变量 | 位置 | 说明 |
|------|------|------|
| `enable_longhorn` | globals.yaml | 是否部署 Longhorn |
| `longhorn_replica_count` | globals.yaml | 卷副本数（默认 2） |
| `longhorn_backup_enabled` | globals.yaml | 是否开启备份 |
| `longhorn_backup_target` | globals.yaml | 备份目标 URL |
| `longhorn_data_locality` | globals.yaml | 数据本地性策略 |
| `longhorn_replica_node_tag` | globals.yaml | 限制副本节点 |
| `longhorn_chart_version` | globals.yaml | Helm chart 版本 |
| `longhorn_cli_version` | globals.yaml | longhornctl 版本 |
| `cluster_storage_class` | globals.yaml | 覆盖默认 StorageClass |

完整变量列表见 `globals-sample.yaml` 的 `存储 - Longhorn` 一节。
