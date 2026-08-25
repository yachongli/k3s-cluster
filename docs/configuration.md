# Configuration Guide

This document explains **how to configure** your k3s-cluster deployment.
For design rationale, see [architecture.md](./architecture.md).

## File layout

```
inventory/cluster/
├── hosts-sample.yaml           # node inventory template (copy to hosts.yaml)
├── hosts.yaml                  # your inventory (gitignored)
└── group_vars/
    └── all/                    # group_vars for 'all' group (auto-loaded)
        ├── main.yaml           # repo baseline + defaults map + secrets bridge + tags
        ├── globals-sample.yaml # override template (copy to globals.yaml)
        ├── globals.yaml        # user overrides (kolla-style, gitignored) ← EDIT THIS
        └── passwords.yaml      # all vault-encrypted secrets (kolla passwords.yml)
```

Ansible auto-loads every file under `group_vars/all/`. The directory name
(`all`) matches the implicit `all` group, so these vars apply to every host.

## What to edit

| File | When to edit | What's inside |
|------|--------------|---------------|
| `globals.yaml` | **Per-cluster tuning** | `enable_*` switches, override vars (commented) |
| `passwords.yaml` | **Secrets setup** | Vault-encrypted passwords + credentials |
| `all.yaml` | **Changing repo baseline** (rare) | `defaults` map, tags, credentials bridge |
| `hosts.yaml` | **Node inventory** | server/agent hostnames (copy from `hosts-sample.yaml`) |
| `roles/*/defaults/main.yaml` | **Advanced tuning** (rare) | Nested role vars, chart versions |

## Quick start

```bash
# 1. Generate internal passwords (UUID-based)
python3 generate-passwords.py

# 2. Fill in external credentials (Cloudflare, iCloud, Slack, NAS, SSH)
python3 setup-secrets.py

# 3. Edit globals.yaml for your network
vim inventory/cluster/group_vars/all/globals.yaml

# 4. Validate
ansible-playbook validation.yaml

# 5. Deploy
ansible-playbook provisioning.yaml
```

## globals.yaml — the one file you edit

### Component enable switches

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
enable_multus: false           # meta-CNI for multi-network
enable_kube_ovn: false          # additional CNI (via Multus)
enable_gpu_operator: false      # NVIDIA GPU (requires GPU hardware)
```

Setting `enable_<component>: false`:
- Skips deployment and postinstall
- Triggers derived defaults (e.g., storage class → `local-path`)
- `reset.yaml` is NOT filtered — always cleans up all components

### Common overrides (uncomment to change)

```yaml
# cluster_api_host: "192.168.4.10"              # LB / K3s API VIP
# cluster_architecture: "aarch64"                # override auto-detection
# cluster_storage_class: "rook-ceph"             # override all storage consumers
# cilium_lb_ip_start: "192.168.4.20"            # LoadBalancer IP pool
# cilium_routing_mode: "tunnel"                  # native | tunnel
# cilium_ipam_mode: "kubernetes"                # cluster-pool | kubernetes
# victoria_metrics_storage_size: "100Gi"         # vmsingle PVC size
# kubevirt_use_emulation: true                   # no /dev/kvm
# cluster_kubelet_kubevirt_profile: true         # CPU pinning for VMs
```

### Derived defaults (auto-adapt, no manual config needed)

| Setting | Auto-adapts to | Override |
|---------|---------------|---------|
| Storage class | `longhorn` if enabled, else `local-path` | `cluster_storage_class` |
| ServiceMonitor | enabled if VictoriaMetrics deployed | `cluster_service_monitor_enabled` |
| Ingress class | `cilium` if enabled, else `traefik` | `cluster_ingress_class` |
| CNI exclusive | `false` if Multus enabled | `cilium_cni_exclusive` |
| Architecture | from `ansible_facts.machine` | `cluster_architecture` |

### Pod resource tiers

```yaml
# cluster_pod_resources:
#   small:                                    # sidecars, operators, multus
#     limits: {cpu: 1, memory: 512Mi}
#     requests: {cpu: 100m, memory: 256Mi}
#   medium:                                   # agents (cilium, grafana, vmagent)
#     limits: {cpu: 2, memory: 2Gi}
#     requests: {cpu: 500m, memory: 1Gi}
#   large:                                    # storage (vmsingle, vmstorage)
#     limits: {cpu: 4, memory: 8Gi}
#     requests: {cpu: 1, memory: 4Gi}
```

All 33 resource blocks across 9 roles reference these tiers. Override one
tier to scale the entire cluster.

## Secrets management

### passwords.yaml

All secrets in flat vault-encrypted variables:

| Variable | Type | How to set |
|----------|------|------------|
| `password_argocd_admin` | Internal | `generate-passwords.py` (UUID) |
| `password_argocd_user` | Internal | `generate-passwords.py` (UUID) |
| `password_grafana_admin` | Internal | `generate-passwords.py` (UUID) |
| `credential_cloudflare_api_token` | External | `setup-secrets.py` |
| `credential_postfix_alias` | External | `setup-secrets.py` |
| `credential_postfix_name` | External | `setup-secrets.py` |
| `credential_postfix_password` | External | `setup-secrets.py` |
| `credential_slack_webhook_url` | External | `setup-secrets.py` |
| `credential_longhorn_backup_password` | External | `setup-secrets.py` |

SSH connection credentials (`ansible_user`, `ansible_password`,
`ansible_become_password`) are NOT stored in the vault. Default deployment
model is SSH key + passwordless sudo. Override in `globals.yaml` only when
password auth is required, or pass `--ask-pass` / `--ask-become-pass` on
the command line.

### Commands

```bash
# Generate internal UUID passwords (ArgoCD, Grafana)
python3 generate-passwords.py
python3 generate-passwords.py --force    # regenerate all

# Fill in external credentials interactively
python3 setup-secrets.py

# List / encrypt / rotate vault passwords
ansible-playbook vault.yaml
```

## Common scenarios

### Disable Longhorn, use local-path

```yaml
enable_longhorn: false
```
Storage class auto-derives to `local-path` for all consumers.

### Multi-CNI: Multus + Cilium + Kube-OVN

```yaml
enable_multus: true
enable_kube_ovn: true
```
Cilium's `cni.exclusive` auto-disables. Kube-OVN runs in Non-Primary mode.

CNI config order: `00-multus.conf` → `05-cilium.conflist` → `10-kube-ovn.conflist`

### KubeVirt with GPU passthrough

```yaml
enable_kubevirt: true
enable_gpu_operator: true
gpu_operator_sandbox_workloads_enabled: true
cluster_kubelet_kubevirt_profile: true    # CPU pinning + NUMA alignment
```

### Tunnel routing (cloud environments)

```yaml
cilium_routing_mode: "tunnel"
cilium_tunnel_protocol: "geneve"
```

### Custom storage for one component only

```yaml
enable_longhorn: true                           # baseline = longhorn
victoria_metrics_storage_class: "rook-ceph"     # only VM uses rook
```

### Skip ArgoCD

```yaml
enable_argo_cd: false
```

## Validation

```bash
# Full validation
ansible-playbook validation.yaml

# Single component
ansible-playbook validation.yaml -t cilium

# Upgrade simulation (helm diff)
ansible-playbook validation.yaml -t victoria-metrics
```

Checks: URL reachability, Helm values rendering, `helm diff`, kubeconfig validity.

## Deployment

```bash
# Full deployment
ansible-playbook provisioning.yaml

# Phase-by-phase
ansible-playbook provisioning.yaml -t cluster       # OS + hardware
ansible-playbook provisioning.yaml -t kubernetes     # K3s + Helm
ansible-playbook provisioning.yaml -t charts         # All chart roles

# Single component
ansible-playbook provisioning.yaml -t kubevirt

# Upgrade
ansible-playbook upgrade.yaml -t cilium

# Reset (cleanup everything, including disabled components)
ansible-playbook reset.yaml
ansible-playbook reset.yaml -t kubevirt    # single component
```

## Adding a new override

1. **`all.yaml`** — add to `defaults` map:
   ```yaml
   defaults:
     cilium:
       new_param: default_value
   ```

2. **Role `defaults/main.yaml`** — reference with fallback:
   ```yaml
   some_key: '{{ cilium_new_param | default(defaults.cilium.new_param) }}'
   ```

3. **`globals.yaml`** — add commented hint (optional):
   ```yaml
   # cilium_new_param: "default_value"
   ```

## Variable reference

The authoritative source is the `defaults:` map in `all.yaml`.
`globals.yaml` mirrors the same keys in commented form as a quick-reference.
