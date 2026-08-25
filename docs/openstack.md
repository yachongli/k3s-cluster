# OpenStack 部署注意事项

在 OpenStack 上部署 k3s-cluster 时，需要注意以下三个关键问题。这些问题在裸金属环境不会遇到，但在 OpenStack 的 L2/L3 网络安全模型下会导致 Pod 网络不通。

---

## 1. Kube-OVN Pod CIDR 不得与虚拟机网络重叠

### 问题

Kube-OVN 安装时会创建 `ovn-default` 子网，默认 Pod CIDR 为 `10.16.0.0/16`。如果你的 OpenStack 虚拟机也在 `10.16.0.0/16` 网段，会导致路由冲突：

- Kube-OVN 的 Pod IP 和虚拟机 IP 在同一个 `/16` 里
- 内核路由表无法区分 Pod 流量和虚拟机流量
- 节点上的 OVN 网关路由会覆盖物理网络路由

### 症状

- `ovn-default` 子网创建成功，但 Pod 无法通信
- 节点网络间歇性不通
- `kubectl get subnet` 显示 CIDR 与节点 IP 重叠

### 解决方案

在 `globals.yaml` 中修改 Kube-OVN 的 Pod CIDR，避开虚拟机网络：

```yaml
# 假设虚拟机在 10.16.0.0/16
kube_ovn_pod_cidr: "10.18.0.0/16"
kube_ovn_pod_gateway: "10.18.0.1"
```

同时确认 Cilium 的 Pod CIDR（默认 `10.42.0.0/16`）也不与虚拟机网络重叠。

### 验证

```bash
# 确认子网 CIDR 不与节点 IP 重叠
kubectl get subnet ovn-default
# CIDR 应为 10.18.0.0/16，不是 10.16.0.0/16

# 确认节点 IP 不在 Pod CIDR 内
kubectl get nodes -o wide
# 节点 IP 应为 10.16.x.x，不在 10.18.0.0/16 内
```

---

## 2. OpenStack 安全组必须允许节点间通信

### 问题

OpenStack 默认的 security group 只允许 VM 自己发起的流量回来（stateful firewall），**不允许其他 VM 主动连接**。K3s 集群的节点之间需要大量端口通信：

| 端口 | 用途 |
|------|------|
| 6443 | K3s API server |
| 8472/UDP | Cilium VXLAN 隧道 |
| 4244/TCP | Cilium health checking |
| 10250 | kubelet API |
| 2379, 2380 | etcd (HA 模式) |
| 6641-6644/TCP | OVN 数据库 (Kube-OVN) |

### 症状

- Cilium health check 失败：`Cluster health: 0/2 reachable`
- VXLAN 隧道不通：跨节点 Pod 无法 ping 通
- CoreDNS 无法连接 API server
- `kubectl get nodes` 显示节点 NotReady

### 解决方案

**方案 A：使用 remote security group（推荐）**

创建一个 security group，让所有节点互相放行：

```bash
# 创建 security group
openstack security group create k3s-cluster

# 允许组内成员互相访问（所有端口、所有协议）
openstack security group rule create k3s-cluster \
  --ingress --protocol tcp --remote-group k3s-cluster

openstack security group rule create k3s-cluster \
  --ingress --protocol udp --remote-group k3s-cluster

openstack security group rule create k3s-cluster \
  --ingress --protocol icmp --remote-group k3s-cluster

# 将所有节点加入这个 group
openstack server add security group k8s-test-1 k3s-cluster
openstack server add security group k8s-test-2 k3s-cluster
```

**方案 B：按 CIDR 放行**

如果不想用 remote-group，可以直接按节点网段放行：

```bash
openstack security group rule create <sg_name> \
  --ingress --protocol tcp --remote-ip 10.16.0.0/16

openstack security group rule create <sg_name> \
  --ingress --protocol udp --remote-ip 10.16.0.0/16

openstack security group rule create <sg_name> \
  --ingress --protocol icmp --remote-ip 10.16.0.0/16
```

### 验证

```bash
# 节点间 VXLAN 端口
nc -uz 10.16.2.27 8472

# Cilium health check
kubectl exec -n kube-system ds/cilium -- cilium-health status
# 应显示 2/2 reachable

# 跨节点 Pod 通信
kubectl run test --rm -it --image=busybox --restart=Never -- \
  ping -c 3 <对端节点上的Pod IP>
```

---

## 3. Cilium masquerade 配置不得包含节点网段

### 问题

Cilium 的 `ip-masq-agent` ConfigMap 定义了 `nonMasqueradeCIDRs`——这些 CIDR 的流量**不做 SNAT**，保持 Pod IP 作为源地址。

默认值是：
```json
{"nonMasqueradeCIDRs":["10.0.0.0/8","192.168.0.0/16"]}
```

`10.0.0.0/8` 包含了几乎所有私有地址，**包括你的虚拟机 IP**（如 `10.16.x.x`）。这导致：

- Pod → 节点 IP 的流量**不做 masquerade**
- 包以 Pod IP（`10.42.x.x`）为源发出物理网卡
- OpenStack port security 发现源 IP 不是虚拟机分配的 IP → **丢包**（anti-spoofing）

### 症状

- Pod → Pod（跨节点）通（走 VXLAN，外层是节点 IP）
- Pod → Service（TCP）不通（eBPF 转换后到节点 IP，但 masquerade 没生效）
- Pod → 节点 IP 不通（源 IP 是 Pod IP，被 OpenStack 丢弃）
- CoreDNS 报 `Failed to watch`（无法连接 API server）
- `tcpdump` 在物理网卡上看到源 IP 是 `10.42.x.x`（Pod IP），不是节点 IP

### 解决方案

修改 `nonMasqueradeCIDRs`，**只包含 Pod CIDR 和 Service CIDR**：

```bash
kubectl edit cm -n kube-system ip-masq-agent
# 改成：
# config: '{"nonMasqueradeCIDRs":["10.42.0.0/16","10.43.0.0/16"]}'

# 重启 Cilium 让配置生效
kubectl rollout restart -n kube-system ds/cilium
```

或者在 `globals.yaml` 中永久配置：

```yaml
cilium_ip_masq_agent_enabled: true
cilium_non_masquerade_cidrs:
  - "10.42.0.0/16"    # Pod CIDR
  - "10.43.0.0/16"    # Service CIDR
```

### 原理

修改后的流量行为：

| 场景 | 源 IP（出物理网卡时） | OpenStack 是否认可 |
|------|----------------------|-------------------|
| Pod → Pod（跨节点） | 节点 IP（VXLAN 外层） | ✅ |
| Pod → Service | 节点 IP（BPF masquerade） | ✅ |
| Pod → 节点 IP | 节点 IP（BPF masquerade） | ✅ |
| Pod → 外网 | 节点 IP（BPF masquerade） | ✅ |
| Pod → Pod（同节点） | 不出物理网卡 | N/A |

### 验证

```bash
# 确认 ConfigMap
kubectl get cm -n kube-system ip-masq-agent -o jsonpath='{.data.config}'
# 应为 {"nonMasqueradeCIDRs":["10.42.0.0/16","10.43.0.0/16"]}

# 从 Pod 测试 Service
kubectl run test --rm -it --image=nicolaka/netshoot --restart=Never -- \
  curl -k --max-time 5 https://10.43.0.1:443/version
# 应返回 K8s 版本 JSON

# 抓包确认源 IP 是节点 IP（不是 Pod IP）
tcpdump -i enp3s0 -ennp 'port 6443' -c 5
# 源 IP 应为 10.16.x.x（节点 IP），不是 10.42.x.x（Pod IP）
```

---

## 4. 不需要 allowed_address_pairs

### 结论

在正确配置 Cilium masquerade 的前提下，**不需要** 在 OpenStack VM port 上添加 `allowed_address_pairs`。

原因：
- Cilium BPF masquerade 保证所有出物理网卡的流量源 IP 都是节点 IP
- Cilium VXLAN 隧道的外层也是节点 IP
- OpenStack port security 看到的都是合法的节点 IP

### 例外

如果禁用了 masquerade（`cilium_ip_masq_agent_enabled: false`），或者有其他组件绕过了 Cilium 的 masquerade，则需要：

```bash
openstack port set --allowed-address ip-address=10.42.0.0/16 <port_id>
```

但这不是推荐的做法——应该优先修复 masquerade 配置。

---

## 5. Service IP 的 ICMP 行为

### 限制

`ping <service_ip>` 会失败——这是**正常的**，不是 bug。

### 原因

Cilium 的 `KubeProxyReplacement` 通过 eBPF 在 **socket 层** 拦截 `connect()` 系统调用来转换 Service IP。ICMP 不走 `connect()`，所以 eBPF 不介入，包以原始 Service IP 为目标直接发出，被网关丢弃。

### 正确的测试方式

```bash
# 用 TCP 测试 Service（正确）
curl -k --max-time 5 https://10.43.0.1:443/version

# 用 TCP 测试 CoreDNS
kubectl run test --rm -it --image=nicolaka/netshoot --restart=Never -- \
  nslookup kubernetes.default.svc.cluster.local 10.43.0.10

# ping Pod IP 或节点 IP（不走 Service）
ping 10.42.1.180
ping 10.16.2.27
```

---

## 快速排查清单

| 症状 | 可能原因 | 排查命令 |
|------|---------|---------|
| 跨节点 Pod 不通 | 安全组未放行 8472/UDP | `nc -uz <对端IP> 8472` |
| Pod → Service 不通 | masquerade 配置包含节点网段 | `kubectl get cm ip-masq-agent -n kube-system` |
| CoreDNS `Failed to watch` | Pod 无法到 API server | `curl -k https://10.43.0.1:443/version` from Pod |
| Kube-OVN 子网 CIDR 冲突 | Pod CIDR 与节点网络重叠 | `kubectl get subnet ovn-default` |
| `ping <service_ip>` 失败 | ICMP 不走 eBPF（正常） | 用 TCP 测试代替 |
| Cilium health 0/N | 安全组或 VXLAN 端口不通 | `cilium-health status` |
