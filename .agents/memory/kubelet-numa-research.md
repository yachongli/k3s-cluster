# kubelet-numa-research.md 文档记忆

> 本文件供 AI 助手在其他编辑器中编辑 `docs/kubelet-numa-research.md` 时使用。
> 记录该文档的来源、结构、关联代码提交，以及文档与提交之间的已知差异。

---

## 文档定位

- **文件路径**：`docs/kubelet-numa-research.md`（约 2300 行，93579 字符）
- **文档性质**：技术研究文档，非代码文件。描述 kubelet NUMA 分配机制 + KubeVirt NUMA 映射的完整方案。
- **作者**：yachong（yachongli / lyc，GitHub: 879228763@qq.com）
- **kubelet 源码**：`D:\GolandProjects\kubernetes`（release-1.28 分支浅克隆，v1.28.15）。第 1 章引用的代码位于 `pkg/kubelet/cm/` 下：`topologymanager/scope_pod.go`（Admit）、`cpumanager/policy_static.go`（generateCPUTopologyHints）、`devicemanager/manager.go`（GetPodTopologyHints / Allocate / allocateContainerResources / filterByAffinity）。

## 文档来源

本文档由两份原始文档合并、重写、扩充而成：

| 原始文件 | 位置 | 对应章节 |
|---|---|---|
| `k8s的numa研究及优化.md` | `D:\PycharmProjects\k8s-in-action-bak\kubevirt-docs\` | 第 1 章 |
| `kubevirt使用numa映射.md` | 同上 | 第 2 章 |

> 注意：原始文件名是中文，在 cmd/PowerShell 中因 GBK 编码会显示乱码，需用 Python 读取。

另有第三份文档 `libvirt-numa映射原理总结.md`（同目录），当前文档未直接引用，但可作为 libvirt 层面的补充参考。

## 文档结构

### 第 1 章：k8s 的 numa 研究及优化

- 1.1 目标 / 1.2 结论
- 1.3 过程
  - 1.3.1 kubelet（run 方法 → containerManager 对象 → 四个子 Manager）
  - 1.3.2 topologyManager（重组为"收集/决策/执行"三层结构）
    - 1.3.2.1 全景：概念表 + 调用关系总览图（mermaid）+ 三层概括（放在章节开头）
    - 1.3.2.2 准入入口：Admit 方法（含 mermaid 流程图）
    - 1.3.2.3 收集层：三个子 Manager 的 GetPodTopologyHints
      - 1.3.2.3.1 cpuManager（段内小标题：（1）NUMA 掩码、（2）generateCPUTopologyHints、（3）手动算一下、（4）延伸均匀分配已放弃）
      - 1.3.2.3.2 deviceManager（（1）GetPodTopologyHints 含被注释的 mermaid、（2）generateDeviceTopologyHints）
      - 1.3.2.3.3 memoryManager：calculateHints 与掩码穷举（staticPolicy.calculateHints 源码 + minAffinitySize 导致 cpu/memory hints 不一致的分析；2026-08-24 从 1.5.1 迁入，使三个子 Manager 与 1.3.2.3 开头的说明真正对应）
    - 1.3.2.4 决策层：Merge 出 bestHint 并存入 scope（（1）Merge、（2）存入 scope）
    - 1.3.2.5 执行层：allocateAlignedResources
  - 1.3.3 分配（执行层详解）
    - 1.3.3.1 Allocate 方法（含 mermaid 流程图）/ 1.3.3.1.1 allocateContainerResources（含 mermaid）/ 1.3.3.1.2 filterByAffinity（概念性丰富：取回 bestHint → 分堆 aligned/unaligned/noAffinity → devicesToAllocate 按优先级消费；开头有完整调用链 ASCII 图；无源码追踪，源码在 kubernetes devicemanager/manager.go L700）
  - 1.3.4 kubelet 重启与状态恢复（Pod 不重启；bestHint 重算 vs 三 Manager checkpoint 读取：device-plugins/kubelet_internal_checkpoint、cpu_manager_state、memory_manager_state；策略名不一致会拒绝启动）
  - 1.4 调整（pci-passthrough：device-plugin 上报 Topology）
  - 1.4.1.1 将 topology 上传上去（iommuMap/deviceMap mermaid 结构图）
  - 1.4.1.2 上报的 Device 信息（手工修改方案：Device ID = iommu group id、iommuNodeMap 查表填 Topology、env 含整组地址；开头引用块记录了曾评估上游新版但因 env 只报请求设备 BDF 与第 2 章不兼容而暂不切换，合并事宜移至第 2 章整理）
  - 1.4.1.3 测试结果
- 1.5 内存分配（开篇问题 1/2 保留并引用 1.3.2.3.3；源码解读已迁至 1.3.2.3.3）
  - 1.5.1 不使用 static 模式（原 1.5.1.1 升格）
  - 1.5.2 为什么会 OOM kill（原 1.5.1.2 升格，含 cgroup 使用小节）
  - 1.5.3 KubeVirt 的内存匹配（原 1.5.2 顺延；1.5.3.1 hugePages）
- 1.6 cpu 分配（distribute-cpus-across-numa）
- 1.7 代码路径 / 1.8 kubelet 配置文件

### 第 2 章：KubeVirt 使用 NUMA 映射

- 2.1 简介（device-plugin / kubevirt 组件）
- 2.2 修改
  - 2.2.1 IOMMU/NUMA 映射（device-plugin 上报 Topology）
  - 2.2.2 显卡与声卡共存（auxFlag / SubDevice）
  - 2.2.3 allocate 两版对比（第一版手工拼接 → 第二版 convertNumaToIndexMap + sortIommuByInt）
  - 2.2.4 上报 NUMA 信息的使用（修改 KubeVirt 本身）
    - 2.2.4.1 解析（CreateHostDevices → NewPCIAddressPool → load）
    - 2.2.4.2 修改 load 方法（`_WithNUMA` env）
    - 2.2.4.3 修改 createDevice 方法（SplitPCIAddressIfNuma / createPCIEBus / createPcieRoot / attachHostDeviceToController）
- 2.3 总结（修改对照表）
- **待办：上游新版 kubevirt-gpu-device-plugin 的合并整理**（源起 1.4 的评估事件，详见下文"上游新版评估事件"）

## 关联代码提交

### 1. kubevirt 项目

**路径**：`D:\GolandProjects\kubevirt`

#### 提交 8dcaaf00（第一版，2025-03-26）

- **作者**：lyc <liyc@paratera.com>
- **message**：`To solve it simply: 1. The problem of coexistence of graphics card and sound card 2. The issue of numa mapping between graphics card and sound card`
- **改动**：10 文件，+306/-49
- **关键文件**：
  - `pkg/virt-launcher/virtwrap/device/hostdevice/addresspool.go` — 新增 `RLen` 方法
  - `pkg/virt-launcher/virtwrap/device/hostdevice/hostdev.go` — 新增 `getNumaMapFromEnv`、`createPCIEBus`、`createPcieRoot`、`attachHostDeviceToController`；重构 `createHostDevices`（按 resourceName 分组、auxFlag 成对 Pop、NUMA 映射生成 pxb-pcie 控制器）
  - `pkg/virt-launcher/virtwrap/api/schema.go` — `BusNRTarget` 类型（第二版被重命名为 `Target`）
  - `pkg/virt-launcher/virtwrap/device/hostdevice/gpu/hostdev.go` — 注释掉 `validateCreationOfAllDevices`（因声卡存在导致校验失败）
- **对应文档**：2.2.2 显卡声卡共存、2.2.4.3 createDevice 方法（第一版）

#### 提交 631d3906（第二版，2026-01-15）

- **作者**：yachongli <879228763@qq.com>
- **message**：`lyc_test`
- **改动**：5 文件，+167/-51
- **关键变化**：
  - `schema.go`：`BusNRTarget` → `Target`；`HostDevice` 新增 `SubDevice []HostDevice` 字段；`Target` 新增 `Port` 字段
  - `addresspool.go`：`load` 方法新增 `_WithNUMA` env 读取逻辑
  - `hostdev.go`：
    - `getNumaMapFromEnv` 改用 `util.ResourceNameToEnvVar()`（第一版是手工 ToUpper + Replace + Join）
    - 新增 `getNumaNodeIndexFromMap`（NUMA 节点 ID → 索引映射，**文档中未展示此函数**）
    - 新增 `SplitPCIAddressIfNuma`（从地址中切分 NUMA）
    - `createHostDevices` 重构：auxHostDevice 改为指针初始化、err 处理细化、SubDevice 替代直接 append
    - `busNR` 从 30 改为 40，`index` 从 10 改为 20
  - `converter.go`：新增 magicUUID + firmwareUUIDns（注释掉的 UUID 生成逻辑，与 NUMA 无关）
- **对应文档**：2.2.4.2 修改 load 方法、2.2.4.3 createDevice 方法（第二版）

### 2. kubevirt-gpu-device-plugin 项目

**路径**：`D:\GolandProjects\kubevirt-gpu-device-plugin`

#### 提交 f22efc21（2026-07-24）

- **作者**：yachongli <879228763@qq.com>
- **message**：`123`
- **改动**：3 文件，+197/-3
- **关键变化**：
  - `pkg/device_plugin/device_plugin.go`：
    - 新增 `iommuNodeMap`（iommu group → numa node）
    - 新增 `readNumaFromFilefunc`、`sortIommuByInt`
    - 新增 `readIommuBlacklistFile` + `iommuBlacklist`（黑名单机制）
    - `createDevicePlugins` 中为每个 Device 附加 `TopologyInfo{Nodes: [{ID: nodeID}]}`
    - `createIommuDeviceMap` 中读取 `numa_node` 填充 `iommuNodeMap`
  - `pkg/device_plugin/generic_device_plugin.go`：
    - `Allocate` 新增 `convertNumaToIndexMap`（NUMA 节点 ID → 索引）、`sortIommuByInt` 排序
    - 生成 `_WithNUMA` env（`PCI_RESOURCE_..._WithNUMA=addr/index,...`）
  - `generic_device_plugin_test.go`：新增 `TestConvertNumaToIndexMap`
- **对应文档**：2.2.1 IOMMU/NUMA 映射、2.2.3 allocate 两版对比（第二版）

#### 提交 d55f16cc（当前 HEAD，回退到自有方案）

- **决定**：对比 Allocate 返回值后发现上游新版 env 只报请求设备 BDF，与第 2 章 KubeVirt 修改（声卡配对、WithNUMA）不兼容，**恢复我们自己的方案**
- **内容**：将 device_plugin.go / generic_device_plugin.go / generic_device_plugin_test.go 三个文件还原为 f22efc21 版本（`git diff f22efc21` 对这三个文件为空）；vgpu 相关文件保留上游改进（与 passthrough 方案无关）
- **验证**：`go build ./pkg/...` 通过
- **后续待办**：单开章节处理两套方案合并（候选方向：KubeVirt 改用 SubDevice 主动请求声卡，或插件恢复整组上报）
- **对应文档**：1.4.1.2（当前生效方案）、1.4.1.3（上游分析 + 返回值对比表）

- **背景**：合并上游 master（a964696e，NVIDIA 官方仓库）时产生半合并冲突状态（旧手工方案与上游 BDF 新方案类型冲突，无法编译）
- **上游新版关键变化**：
  - `NvidiaGpuDevice` 增加 `numaNode int64` 字段；`deviceMap` 变为 `map[string][]NvidiaGpuDevice`
  - **Device ID 语义变化：iommu group id → PCI BDF**（影响 kubelet checkpoint、Allocate 收到的 DevicesIDs、环境变量内容）
  - `Allocate` 重写为 BDF 流程（bdfToIommuMap 反查组、校验 iommu_group/vendor、iommufd 支持、EGM/Grace 平台支持）
  - 环境变量只报请求设备自己的 BDF（避免兄弟设备抢占 KubeVirt 槽位）
- **本提交的修复内容**：
  - `device_plugin.go`：删除旧 `iommuNodeMap`/`readNumaFromFilefunc` 重复代码；devs 构造改为 `ID: dev.addr` + `Topology` 直接用 `dev.numaNode`；黑名单判定改为经 `bdfToIommuMap` 查组；删除 `getIommuNodeMap`
  - `generic_device_plugin.go`：`Allocate` 重写为上游 BDF 风格 + 保留 `_WithNUMA` env 特性（键名统一大写资源名）；`convertNumaToIndexMap` 签名改为接收 `map[string][]NvidiaGpuDevice`（从 iommuMap 内取 numaNode，输出 BDF→索引）；删除 watch 函数里错误的 `pathDeviceMap[devicePath] = dev.ID`
  - `generic_device_plugin_test.go`：`TestConvertNumaToIndexMap` 适配新签名
- **验证**：`go build ./pkg/...` 通过；vet 仅剩 2 个上游遗留警告（context leak L200、vgpu unreachable L366，上游 a964696e 同样存在）；cmd 链接失败仅为 Windows 缺 NVML 库的平台问题
- **对应文档**：1.4.1.3 上游新版的原生 topology 上报
- **注意**：该提交已被 d55f16cc 回退（三个核心文件还原为 f22efc21），提交本身保留在 git 历史中，未来合并上游时可参考其修复内容

## 上游新版评估事件（2026 年，代码仓库 `D:\GolandProjects\kubevirt-gpu-device-plugin`）

**事件经过**：合并上游 master（a964696e，NVIDIA 官方）时产生半合并冲突（无法编译）。我们曾修复冲突并切到上游新方案（提交 7795f452），但对比 `Allocate` 返回值后发现关键差异，最终回退到自有方案（提交 d55f16cc，三个核心文件还原为 f22efc21）。文档中曾写过的 1.4.1.3"上游新版分析"章节已按用户要求删除，本记录保留全部事实供第 2 章整理时使用。

**上游新版关键变化**（相对 f22efc21）：
- `NvidiaGpuDevice` 增加 `numaNode int64` 字段；`deviceMap` 变为 `map[string][]NvidiaGpuDevice`；新增 `bdfToIommuMap`（BDF → iommu group）
- Device ID 语义变化：**iommu group id → PCI BDF**（影响 kubelet checkpoint、Allocate 收到的 DevicesIDs、环境变量内容）
- Allocate 重写为 BDF 流程（bdfToIommuMap 反查组、校验 iommu_group/vendor、iommufd、EGM/Grace 支持）
- env 键名统一 `ToUpper(deviceName)`

**不兼容的根因**（两套方案 Allocate 返回值对比，以请求 1 块 GPU + 同组声卡、NUMA 0 为例）：

| 字段 | 我们（f22efc21，生效中） | 上游新版 |
|---|---|---|
| 收到的 DevicesIDs | `["49"]`（组号） | `["0000:a1:00.0"]`（BDF） |
| `PCI_RESOURCE_..._<NAME>` | 整组地址（GPU+声卡，数量 = GPU 数 × 2） | 仅请求设备的 BDF |
| `..._<NAME>_WithNUMA` | 整组地址带 `/index` | 仅请求设备 |
| DeviceSpecs | `/dev/vfio/vfio` + `/dev/vfio/<组号>` | 同左（iommufd 时额外挂载） |

第 2 章的 KubeVirt 修改（声卡配对 auxFlag/SubDevice、`getNumaMapFromEnv`、WithNUMA 映射）依赖"env 含整组地址"这一行为，上游新版只报请求设备 BDF，直接切换会导致配对与 NUMA 映射失败。

**第 2 章整理时的候选方向**：
1. 改插件：在新版基础上恢复整组地址上报（改动最小）
2. 改 KubeVirt：声卡作为独立资源由 KubeVirt 主动请求（长期更干净，工作量大）
- 切换时注意：Device ID 从组号变 BDF，kubelet checkpoint 中旧 ID 会成为"孤儿"，已分配 Pod 需重建
- 修复参考：提交 7795f452（保留在 git 历史中，含完整的合并冲突解决代码）；kubelet 侧确认过 hints 生成只依赖 `Device.Topology`、不解析 ID 内容，所以 ID 语义变化不影响 NUMA 对齐本身

## 文档与提交的已知差异

> 编辑文档时需注意以下不一致之处：

1. **`getNumaMapFromEnv` 的实现版本**
   - 文档 2.2.4.3 中展示的 `getNumaMapFromEnv` 使用第一版的手工拼接方式（`strings.Join([]string{v1.PCIResourcePrefix, resourceName, "NUMA"}, "_")`）
   - 但 631d3906 提交已改为 `util.ResourceNameToEnvVar(v1.PCIResourcePrefix, resourceName) + "_NUMA"`
   - **文档此处与第二版提交不一致**，编辑时应确认展示哪个版本

2. **`getNumaNodeIndexFromMap` 函数**
   - 631d3906 提交中新增了此函数（NUMA 节点 ID → 索引映射）
   - **文档中未展示此函数**，但文档 2.2.3 描写的 `convertNumaToIndexMap`（device-plugin 侧）功能类似
   - 编辑时需确认是否需要补充此函数的说明

3. **`BusNRTarget` vs `Target`**
   - 文档 2.2.4.3 中 `createPCIEBus` 使用 `Target`（第二版名称）
   - 第一版提交 8dcaaf00 中是 `BusNRTarget`
   - 文档已同步到第二版，**一致**

4. **`busNR` / `index` 初始值**
   - 文档中 `busNR = 40`、`index = 20`（第二版值）
   - 第一版提交中 `busNR = 30`、`index = 10`
   - 文档已同步到第二版，**一致**

5. **auxHostDevice 的处理**
   - 文档 2.2.4.3 中 `auxHostDevice` 初始化为 `&api.HostDevice{}`（指针），与第二版一致
   - 第一版中是 `createHostDev` 返回值（非指针初始化）
   - 文档已同步到第二版，**一致**

6. **`SubDevice` 字段**
   - 文档 2.2.4.3 中使用 `hostDevice.SubDevice = append(hostDevice.SubDevice, *auxHostDevice)`
   - 这是第二版（631d3906）新增的字段，第一版是直接 `hostDevices = append(hostDevices, *auxHostDevice)`
   - 文档已同步到第二版，**一致**

7. **converter.go 的 UUID 逻辑**
   - 631d3906 提交在 `converter.go` 中添加了 magicUUID 相关代码（但被注释）
   - **文档中未提及此改动**，因为它与 NUMA 无关，是同一提交中的无关改动

## 文档中的图表资源

文档引用以下图片/SVG，位于 `docs/assets/`：

| 文件 | 用途 |
|---|---|
| `kubelet-numa-research-1741330532052.png` | containerManager 四个子 Manager 关系图 |
| `kubelet-numa-research-1742434952440.png` | gpu-operator 对 pci 处理流程 |
| `kubelet-numa-research-1742438400943.png` | kubevirt 组件架构 |
| `kubelet-numa-research-1742449188342.png` | kubevirt numa 映射流程 |
| `kubelet-numa-research-1742463677988.svg` | 2.2.4 完整流程图（VMI → libvirt XML） |

> 这些图片与原始文档共用（原始文档在 `k8s-in-action-bak/kubevirt-docs/assets/` 下有对应的中文命名版本）。

## 编辑注意事项

1. **文档是合并重写版**：不是原始文档的简单复制，内容更丰富（更多代码注释、mermaid 图、ASCII 图表、函数定位说明）。编辑时不要回退到原始文档的简略版本。

2. **代码引用来自两个提交**：
   - 第一版（8dcaaf00）的代码在文档中以"第一版"标注
   - 第二版（631d3906）的代码是文档展示的主要版本
   - 编辑时注意区分文档描述的是哪个版本

3. **env 命名约定**：
   - 不带 NUMA：`PCI_RESOURCE_NVIDIA_COM_GA102_GEFORCE_RTX_3090`
   - 带 NUMA（第一版 device-plugin）：`..._NUMA`（后缀）
   - 带 NUMA（第二版 device-plugin + kubevirt load）：`..._WithNUMA`（后缀）
   - 文档 2.2.4.2 的 `load` 方法使用 `_WithNUMA`，与第二版提交一致

4. **NUMA 索引 vs NUMA 节点 ID**：
   - device-plugin 的 `convertNumaToIndexMap` 将真实 NUMA 节点 ID（如 0,1,6,7）转换为索引（0,1,2,3）
   - 这是为了避免 KubeVirt/libvirt 中 pxb-pcie 的 `Node` 属性使用过大的 NUMA ID
   - 文档 2.2.3 对此有描写，编辑时注意保持索引与节点 ID 的区分

5. **术语**：
   - aux device = 声卡（audio controller），与 GPU 同属一个 iommu group
   - auxFlag = 当 `reqLen == 2 * gpuCount` 时为 true，表示需要成对 Pop（GPU + 声卡）
   - SubDevice = libvirt XML 中 `<subdevice>` 元素，用于将声卡挂为显卡的子设备
   - pxb-pcie = QEMU 的 pcie-expander-bus，用于将 PCI 设备绑定到指定 NUMA 节点

## 总结表（文档 2.3.1）

| 问题 | 修改项目 | 修改点 |
|---|---|---|
| 设备拓扑上报 | kubevirt-gpu-device-plugin | 第 1 章：Device.Topology 附带 NUMA 信息 |
| 显卡与声卡的共存 | KubeVirt 本身 | 2.2.2：auxFlag 成对 Pop；声卡挂为显卡 SubDevice |
| NUMA 信息传递 | kubevirt-gpu-device-plugin | 2.2.3：allocate 返回 `_WithNUMA` 环境变量 |
| NUMA 信息使用 | KubeVirt 本身 | 2.2.4：AddressPool.load 解析 `_WithNUMA`；createHostDevices 按 NUMA 生成 pxb-pcie 控制器 |
