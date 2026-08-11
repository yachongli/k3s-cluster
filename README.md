# K3s Cluster

**English** | [简体中文](README.zh-CN.md)

A [kolla-ansible](https://opendev.org/openstack/kolla-ansible)-inspired K3s
deployment framework. Forked from [axivo/k3s-cluster](https://github.com/axivo/k3s-cluster)
and extended with multi-CNI (Multus + Kube-OVN), KubeVirt, NVIDIA GPU Operator,
external Ceph CSI, plaintext-based secrets, and cloud-underlay support (OpenStack,
proxy, registry mirror).

## What you get

| Layer | Components |
|-------|-----------|
| **OS** | Ubuntu LTS, firewall, users, optional postfix/unattended-upgrades |
| **Control plane** | K3s with embedded etcd, HAProxy + Keepalived VIP |
| **CNI** | Cilium (eBPF, kube-proxy replacement, Gateway API, Hubble) — optional Multus + Kube-OVN as secondary CNI |
| **DNS** | CoreDNS (cluster), ExternalDNS (Cloudflare) |
| **TLS** | cert-manager with ACME / Let's Encrypt |
| **Storage** | Longhorn (built-in), Ceph CSI (external cluster) |
| **Observability** | metrics-server, VictoriaMetrics + Grafana + AlertManager, VictoriaLogs + Vector |
| **App mgmt** | ArgoCD, Kured |
| **Compute** | KubeVirt, NVIDIA GPU Operator (with KubeVirt passthrough) |

Everything except `cluster`, `helm`, `k3s` is delivered as a Helm chart. Each
component is toggled by a single `enable_<component>: true|false` in
`globals.yaml`.

## Layout

```
.
├── provisioning.yaml     # Full deploy (cluster + k3s + charts)
├── validation.yaml       # Pre-flight & per-role validation
├── upgrade.yaml          # Per-role upgrade (tag-driven)
├── reset.yaml            # Teardown
├── vault.yaml            # Interactive vault helper (list/encrypt/update)
├── tools/
│   ├── secrets.py        # Plaintext-first secrets tool (init/edit/list/decrypt)
│   └── helm-repo.sh
├── inventory/cluster/
│   ├── hosts-sample.yaml
│   └── group_vars/all/
│       ├── main.yaml            # Repo baseline (defaults + global_map + tags)
│       ├── globals-sample.yaml  # Copy → globals.yaml (your only edit)
│       └── passwords.yaml       # Vault-encrypted fallback (Git-safe)
├── roles/                       # 18 roles, all share the same layout
└── docs/
    ├── ARCHITECTURE.md   /  架构设计.md
    ├── CONFIGURATION.md  /  配置指南.md
    ├── OPENSTACK.md
    └── VICTORIA-METRICS.md
```

## Prerequisites

- **Control host**: Python 3.8+, Ansible 2.19+, `ansible-vault`, `kubernetes` PyPI module
- **Target nodes**: Ubuntu LTS, SSH-key access, passwordless sudo (or set `ansible_become_password`)
- Optional: NVIDIA GPUs (for `gpu-operator`), external Ceph (for `ceph-csi`)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install ansible kubernetes
ansible-galaxy collection install -r collections/requirements.yaml
```

## Configure

### 1. Inventory

```bash
cp inventory/cluster/hosts-sample.yaml inventory/cluster/hosts.yaml
```

```yaml
server:                # control plane (1 for single-node, 3 for HA)
  hosts:
    node1:
    node2:
    node3:
agent:                 # workers
  hosts:
    node4:
    node5:
cluster:
  children:
    server:
    agent:
```

### 2. Globals

`globals.yaml` is the only file you edit. Every default lives under
`defaults:` in `inventory/cluster/group_vars/all/main.yaml`; uncomment
the flat key here to override.

```bash
cp inventory/cluster/group_vars/all/globals-sample.yaml \
   inventory/cluster/group_vars/all/globals.yaml
```

Minimal override:

```yaml
cluster_api_host: "192.168.4.10"          # your K3s API VIP

# Feature toggles (defaults shown)
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

# Cilium LoadBalancer pool (must be in your LAN)
cilium_lb_ip_start: "192.168.4.20"
cilium_lb_ip_stop:  "192.168.4.100"
```

See [`globals-sample.yaml`](inventory/cluster/group_vars/all/globals-sample.yaml)
for every knob, grouped by component (versions, chart tags, tuning).

### 3. Secrets

Two files, one direction — plaintext beats vault:

```
/etc/k3s-cluster/passwords.yaml          ← plaintext, source of truth
        ↓ auto-loaded at playbook start
inventory/cluster/group_vars/all/passwords.yaml   ← vault-encrypted (Git fallback)
```

If the plaintext file exists, its values shadow the vault. No manual sync.

```bash
python3 tools/secrets.py init      # generate UUID internal + empty external
python3 tools/secrets.py edit      # $EDITOR to fill Cloudflare/Ceph/Slack/…
python3 tools/secrets.py list      # READY | EMPTY | PENDING | MISSING
python3 tools/secrets.py decrypt   # recover plaintext from vault
```

**Credentials the tool tracks:**

| Kind | Variable | Component |
|------|----------|-----------|
| internal (UUID) | `password_argocd_admin` `password_argocd_user` `password_grafana_admin` | ArgoCD, Grafana |
| external | `credential_cloudflare_api_token` | ExternalDNS |
| external | `credential_ceph_admin_key` | Ceph CSI |
| external | `credential_longhorn_backup_password` | Longhorn NAS backup |
| external | `credential_slack_webhook_url` | Kured Slack |
| external | `credential_postfix_alias/name/password` | Postfix (iCloud) |

Leaving an external credential empty **auto-disables** its component via
`globals.yaml` / role defaults during `sync` — no failed deploys because of
missing keys.

SSH auth (`ansible_user`, `ansible_password`, `ansible_become_password`) is
**not** managed here. Default: SSH key + passwordless sudo.

## Deploy

```bash
ansible-playbook validation.yaml         # pre-flight checks (per role)
ansible-playbook provisioning.yaml       # full deploy

# Phase-scoped
ansible-playbook provisioning.yaml -t cluster       # OS + firewall + kubelet tuning
ansible-playbook provisioning.yaml -t kubernetes    # K3s + HAProxy + Helm
ansible-playbook provisioning.yaml -t charts        # all enabled components

# Component-scoped (one tag = one role)
ansible-playbook provisioning.yaml -t cilium
ansible-playbook provisioning.yaml -t kubevirt
```

Valid component tags: `cilium multus kube-ovn coredns cert-manager external-dns argo-cd kured longhorn ceph-csi metrics-server victoria-logs victoria-metrics kubevirt gpu-operator`

K3s bootstraps in rolling batches (`serial: 1 → 2 → 5`) so quorum forms
correctly on the first server and workers join without racing.

## Upgrade & reset

```bash
# Upgrade one component (mandatory --tags)
ansible-playbook upgrade.yaml -t cilium
ansible-playbook upgrade.yaml -t k3s

# Reset one component
ansible-playbook reset.yaml -t kubevirt

# Full teardown (prompts for apt package removal)
ansible-playbook reset.yaml
```

## Common scenarios

### Multi-CNI (Cilium primary + Kube-OVN via Multus)

```yaml
enable_multus: true
enable_kube_ovn: true
```

CNI file order on each node:
`00-multus.conf` → `05-cilium.conflist` → `10-kube-ovn.conflist`

Kube-OVN runs in **non-primary** mode: pods keep Cilium as the default network
and attach Kube-OVN via `NetworkAttachmentDefinition`.

### KubeVirt + GPU passthrough

```yaml
enable_kubevirt: true
enable_gpu_operator: true
gpu_operator_sandbox_workloads_enabled: true
gpu_operator_cdi_enabled: true
cluster_kubelet_kubevirt_profile: true    # static CPU + NUMA topology
```

Enables kubelet `cpuManagerPolicy=static`,
`topologyManagerPolicy=restricted`, `distribute-cpus-across-numa`.

### External Ceph

```yaml
enable_ceph_csi: true
ceph_csi_monitors: ["10.0.0.1:6789", "10.0.0.2:6789"]
ceph_csi_client_user: admin
ceph_csi_rbd_pool: rbd-pool
ceph_csi_cephfs_enabled: false
```

Then:
```bash
# On a Ceph host:  ceph auth get-key client.admin
python3 tools/secrets.py edit         # fill credential_ceph_admin_key
```

### Registry mirror (air-gapped / slow upstream)

```yaml
cluster_registry_endpoint: "http://harbor.internal:4000"
```

Mirrors `docker.io`, `gcr.io`, `ghcr.io`, `quay.io`, `registry.k8s.io`, `nvcr.io`.

### Proxy

Two modes, use one or both:

```yaml
# Ansible-only (helm/git/get_url) — never written to hosts
cluster_deploy_proxy: "http://proxy.example.com:3128"
cluster_apt_proxy: false                # true to also route apt

# Persistent on target hosts (systemd drop-ins + /etc/profile.d/)
cluster_http_proxy:  "http://proxy.example.com:3128"
cluster_https_proxy: "http://proxy.example.com:3128"
cluster_no_proxy:
  - "127.0.0.1,localhost"
  - "10.0.0.0/8"
  - "cluster.local"
```

### Tunnel routing (cloud / no L2 adjacency)

```yaml
cilium_routing_mode: "tunnel"           # default
cilium_tunnel_protocol: "vxlan"         # or "geneve"
```

### OpenStack / cloud with port security

Three gotchas — full walkthrough in [docs/OPENSTACK.md](docs/OPENSTACK.md):

1. `cilium_non_masquerade_cidrs` must contain **only** Pod + Service CIDRs.
   Adding the node network leaks Pod IPs to the physical NIC → Neutron drops as spoofed.
2. `kube_ovn_pod_cidr` must **not** overlap the node network
   (default `10.18.0.0/16`; do not use `10.16.0.0/16` if nodes live there).
3. No `allowed_address_pairs` needed on VM ports — BPF masquerade + VXLAN
   already hides Pod IPs behind the node IP.

> **Service IP quirk:** only TCP/UDP traverse Service IPs. `ping <svc-ip>`
> always fails — Cilium's eBPF DNAT sits in `connect()`, which ICMP doesn't call.

### Use local-path instead of Longhorn

```yaml
enable_longhorn: false
```

`cluster_storage_class` auto-derives to `local-path`; every consumer
(VictoriaMetrics, VictoriaLogs, …) picks it up.

## Pod resource tiers

All resource blocks reference three shared tiers instead of hardcoding. Sized
for bare-metal (96C / 512GB+); scale the whole cluster by editing one map.

| Tier | limits | requests | Users |
|------|--------|----------|-------|
| small | 1 CPU / 512Mi | 100m / 256Mi | sidecars, operators, multus |
| medium | 2 CPU / 2Gi | 500m / 1Gi | cilium agent, grafana, vmagent |
| large | 4 CPU / 8Gi | 1 / 4Gi | vmsingle, vmstorage, vmcluster |

```yaml
cluster_pod_resources:
  large:
    limits:   {cpu: 8, memory: 16Gi}
    requests: {cpu: 2, memory: 8Gi}
```

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — why the three layers, derived defaults, CNI coexistence
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — every override, common scenarios
- [docs/OPENSTACK.md](docs/OPENSTACK.md) — OpenStack / spoofing-protected underlays
- [docs/VICTORIA-METRICS.md](docs/VICTORIA-METRICS.md) — VictoriaMetrics / VictoriaLogs storage presets, pinning, HA
- [docs/架构设计.md](docs/架构设计.md) — 设计说明（中文）
- [docs/配置指南.md](docs/配置指南.md) — 配置指南（中文）

## Credits

Upstream: [axivo/k3s-cluster](https://github.com/axivo/k3s-cluster) — three-layer
role design, embedded etcd + HAProxy + Keepalived, Helm-driven deploy pattern.

License: BSD 3-Clause (inherited from upstream).
