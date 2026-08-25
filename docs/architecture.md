# Architecture & Design Rationale

This document explains **why** the k3s-cluster fork is designed the way it is.
For **how to configure** your cluster, see [configuration.md](./configuration.md).

## Overview

This fork extends the upstream [axivo/k3s-cluster](https://github.com/axivo/k3s-cluster)
with a kolla-ansible-inspired configuration model. The goal: **edit one file
to customize the entire cluster**, without touching role internals.

Key changes from upstream:

1. **Kolla-style `globals.yaml`** — single override file with `enable_*` switches
2. **`defaults` map in `all.yaml`** — single source of truth for baseline values
3. **Derived defaults** — storage class, ServiceMonitor, ingress class, architecture
   auto-adapt to `enable_*` switches
4. **Multi-CNI support** — Multus + Cilium + Kube-OVN coexistence
5. **Unified pod resource tiers** — 3 tiers (small/medium/large) shared by all roles
6. **KubeVirt integration** — kubelet CPU pining profile, GPU operator, feature gates
7. **Separated `passwords.yaml`** — flat vault-encrypted variables (kolla `passwords.yml`)
8. **Architecture auto-detection** — binary suffix derived from `ansible_facts.machine`

---

## 1. Why three layers instead of kolla's two?

### kolla-ansible's approach

kolla uses **two layers with identical flat variable names**:

```
ansible/group_vars/all.yml     →  enable_neutron: "yes"  (defaults)
/etc/kolla/globals.yml         →  enable_neutron: "no"   (override, via -e)
```

This works because kolla's variables are **all flat top-level**. Override is
simple same-name replacement.

### Our problem: nested dicts + hash_behaviour

This project's roles use **nested dicts** (`cilium_vars.kubernetes.bpf.datapath_mode`,
`victoriametrics_vars.kubernetes.vmsingle.storage.class`, ...). Ansible's default
`hash_behaviour = replace` means writing a nested key in `globals.yaml` would
**silently wipe** the entire role's defaults:

```yaml
# ❌ This replaces the WHOLE cilium_vars dict
cilium_vars:
  kubernetes:
    kube_proxy:
      replacement: false
```

### Solution: flat override variables + Jinja fallback

We introduce **flat top-level variables** (`cilium_kube_proxy_replacement`)
that role defaults reference via Jinja `default()`:

```
globals.yaml        →  cilium_kube_proxy_replacement: false     (user override)
                           ↓ undefined?
all.yaml defaults   →  defaults.cilium.kube_proxy_replacement   (repo baseline)
                           ↓ referenced by
role defaults        →  cilium_vars.kubernetes.kube_proxy.replacement
                           ↓ consumed by
values.j2            →  kubeProxyReplacement: false              (Helm input)
```

**Result**: users edit only flat variables in `globals.yaml`. No risk of
wiping role internals. No need to understand the nested dict structure.

---

## 2. Derived defaults (auto-adapting to enable_* switches)

Instead of requiring users to manually update dependent settings when toggling
components, several defaults **auto-derive** from `enable_*` switches:

| Variable | Derivation logic | Manual override |
|----------|-----------------|-----------------|
| `storage_class` | `longhorn` if `enable_longhorn`, else `local-path` | `cluster_storage_class` |
| `service_monitor_enabled` | follows `enable_victoria_metrics` | `cluster_service_monitor_enabled` |
| `ingress_class` | `cilium` if `enable_cilium`, else `traefik` | `cluster_ingress_class` |
| `cni_exclusive` | `true` unless `enable_multus`, then `false` | `cilium_cni_exclusive` |
| `architecture` | from `ansible_facts.machine` via `cluster_map` | `cluster_architecture` |
| `architecture_binary` | derived from `architecture` (arm64/amd64/arm) | (derived, no override) |

### Override precedence (highest to lowest)

1. **Component-specific** (`victoria_metrics_storage_class: "nfs-client"`)
2. **Cluster-wide override** (`cluster_storage_class: "rook-ceph"`)
3. **Auto-derived** from `enable_*` switch
4. **Repo baseline** in `all.yaml` `defaults` map
5. **Role defaults** (rarely reached)

Example: disabling Longhorn automatically switches all storage consumers
(VM, VLogs) to `local-path` — zero additional config needed.

---

## 3. Multi-CNI architecture

### CNI config file ordering

```
/etc/cni/net.d/
├── 00-multus.conf           ← Multus (meta-plugin, loads first)
├── 05-cilium.conflist        ← Cilium (primary CNI, all pods)
└── 10-kube-ovn.conflist      ← Kube-OVN (additional CNI, annotation-selected pods)
```

kubelet loads CNI configs in **lexicographic order**. `00` < `05` < `10`,
so Multus is the entry point. Multus delegates to Cilium (default) and
optionally to Kube-OVN (via NetworkAttachmentDefinition).

### Cilium `cni.exclusive` auto-disabling

Cilium's `exclusive: true` renames all non-Cilium CNI configs to `*.cilium_bak`.
This would destroy Multus's `00-multus.conf`. When `enable_multus: true`,
`defaults.cluster.cni_exclusive` auto-derives to `false`, preserving coexistence.

### Kube-OVN as Non-Primary CNI

Kube-OVN is **never** the primary CNI. It runs in `NON_PRIMARY_CNI: true` mode,
exposed to pods via Multus NetworkAttachmentDefinition. Cilium remains the
default network for all pods.

---

## 4. Unified pod resource tiers

### Problem

33 resource blocks were hardcoded across 9 roles, each with tiny values
(`10m CPU / 128Mi memory`). The Multus OOM issue
([#1416](https://github.com/k8snetworkplumbingwg/multus-cni/issues/1416))
was caused by `limits == requests == 50Mi` — no burst capacity.

### Solution: 3 shared tiers

All 33 blocks now reference `defaults.cluster.pod_resources.<tier>`:

| Tier | limits | requests | Used by |
|------|--------|---------|---------|
| `small` | 1 CPU / 512Mi | 100m / 256Mi | sidecars, operators, controllers, multus |
| `medium` | 2 CPU / 2Gi | 500m / 1Gi | cilium agent, grafana, vlogs server, vmagent |
| `large` | 4 CPU / 8Gi | 1 / 4Gi | vmsingle, vmstorage, vmcluster |

**limits > requests** = burst capacity. Pods can temporarily exceed requests
without OOM-kill (fixes the Multus issue).

Override one tier to scale the entire cluster:

```yaml
# globals.yaml
cluster_pod_resources:
  large:
    limits: {cpu: 8, memory: 16Gi}
    requests: {cpu: 2, memory: 8Gi}
```

---

## 5. KubeVirt integration

### Feature gates

`NetworkBindingPlugins` is enabled by default — it enables `managedTap` network
binding mode, required for VM multi-NIC via Multus.

Feature gates are exposed as a configurable list (`kubevirt_feature_gates`).

### Kubelet CPU pinning profile

KubeVirt VMs with `resources.requests.cpu: N` and `cpuManagerPolicy: static`
get dedicated CPU cores. The `cluster_kubelet_kubevirt_profile` flag
auto-configures:

- `cpuManagerPolicy: static`
- `cpuManagerPolicyOptions: {distribute-cpus-across-numa: "true"}`
- `topologyManagerPolicy: restricted`
- `feature-gates: CPUManagerPolicyAlphaOptions=true`

Disabled by default (`false`). Enable only when running KubeVirt with
CPU-pinned guaranteed pods.

### GPU Operator

`enable_gpu_operator: true` deploys NVIDIA GPU Operator with:
- CDI enabled (required for KubeVirt GPU passthrough)
- `sandboxWorkloads.mode: kubevirt` (GPU to VMs)
- DCGM exporter → VictoriaMetrics ServiceMonitor

---

## 6. Architecture auto-detection

`defaults.cluster.architecture` and `architecture_binary` are derived from
`ansible_facts.machine` at runtime (set by the `cluster` role's `tasks/facts.yaml`).

| `ansible_facts.machine` | binary suffix | Used by |
|------------------------|---------------|---------|
| `x86_64` | `amd64` | k3s, cilium, hubble, cmctl, argocd, virtctl, longhornctl, kubepug |
| `aarch64` | `arm64` | (same set) |
| `armv7l` | `arm` | (same set) |
| (other) | `amd64` (fallback) | |

**Heterogeneous clusters**: `cluster_map` is per-host, so each node downloads
the correct binary for its own architecture. No global arch setting needed.

---

## 7. Passwords separation

Following kolla's `passwords.yml` pattern, secrets are in a separate
`passwords.yaml` file with flat variable names:

| Type | Prefix | Example | Generation |
|------|--------|---------|------------|
| Internal passwords | `password_*` | `password_argocd_admin` | `generate-passwords.py` (UUID) |
| External credentials | `credential_*` | `credential_cloudflare_api_token` | `setup-secrets.py` (user input) |

`all.yaml` bridges these to the nested `global_map.credentials.*` structure
expected by role templates — **zero template changes needed**.

```yaml
# all.yaml (bridge)
global_map:
  credentials:
    argocd:
      server:
        admin:
          password: '{{ password_argocd_admin }}'   # ← references passwords.yaml
```

---

## 8. Naming conventions

| Layer | Pattern | Example |
|-------|---------|---------|
| User override (`globals.yaml`) | `<component>_<param>` (snake_case) | `cilium_routing_mode` |
| Repo baseline (`all.yaml` defaults) | `defaults.<component>.<param>` | `defaults.cilium.routing_mode` |
| Role internal | `<role>_vars.kubernetes.<section>.<key>` | `cilium_vars.kubernetes.routing.mode` |
| Helm chart value | camelCase (per upstream) | `routingMode` |

Hyphenated component names use underscores: `enable_external_dns`,
`enable_victoria_metrics`, `enable_argo_cd`, `enable_kube_ovn`,
`enable_gpu_operator`.

---

## 9. Component roles added in this fork

| Role | Type | Purpose |
|------|------|---------|
| `kubevirt` | Manifest | VMs in Kubernetes (operator + CR) |
| `multus` | Manifest | Meta-CNI for multi-network pods |
| `kube-ovn` | Helm | Additional CNI (Non-Primary mode, via Multus) |
| `gpu-operator` | Helm | NVIDIA GPU management + KubeVirt passthrough |

All follow the established patterns:
- `enable_*` switch in `globals.yaml`
- Flat vars in `all.yaml` `defaults` map
- Role defaults reference via `{{ var | default(defaults.<role>.<key>) }}`
- Tags registered in `global_map.tags.role/charts/postinstall`
- `reset.yaml` includes cleanup (not filtered by enable)
