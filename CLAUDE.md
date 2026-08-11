# Project Instructions

Ansible framework for deploying and managing production-ready K3s clusters on bare-metal and virtual machines. Built around specialized Ansible roles that handle every aspect of the cluster lifecycle.

- `/inventory` — Ansible inventory and host configuration
- `/roles` — Ansible roles
- `/provisioning.yaml` — Main playbook for cluster deployment
- `/upgrade.yaml` — Cluster upgrade playbook
- `/validation.yaml` — Post-deployment validation playbook
- `/reset.yaml` — Cluster teardown playbook

Each role has `defaults/main.yaml` for variables, `tasks/` for Ansible tasks, and `templates/values.j2` for Helm values — except `cluster`, `helm`, and `k3s` which have no Helm values template.

- `argo-cd` — GitOps CD, has redis-ha subchart
- `cert-manager` — TLS certificates via ACME/Let's Encrypt
- `cilium` — CNI, network policies, Gateway API, Hubble
- `cluster` — Ubuntu OS, firewall, users (no Helm chart)
- `coredns` — Cluster DNS
- `external-dns` — Automatic DNS records
- `helm` — Helm and plugins (no Helm chart)
- `k3s` — K3s server/agent, load balancer (no Helm chart)
- `kubevirt` — Virtual machines via KubeVirt operator (no Helm chart)
- `kured` — Automated node reboot daemon
- `longhorn` — Distributed block storage
- `metrics-server` — Resource metrics for HPA/VPA
- `victoria-logs` — Log aggregation, has vector subchart
- `victoria-metrics` — Metrics/alerting stack, has operator/grafana/kube-state-metrics/prometheus-node-exporter subcharts

## Tag System (provisioning.yaml)

When adding a new component role, you MUST update these places in `provisioning.yaml`:

1. **Play-level `tags:`** — Add the component name to the `Charts Provisioning` play's tag list (hardcoded, not Jinja2)
2. **`when:` condition in loop** — The `Deploy chart roles` and `Perform post-install tasks` tasks use `ansible_run_tags` filtering so `--tags <component>` only runs that component
3. **`global_map.tags.charts`** in `inventory/cluster/group_vars/all/main.yaml` — Add the component name to the charts list
4. **`global_map.tags.postinstall`** in `inventory/cluster/group_vars/all/main.yaml` — Add if the role has a postinstall task

The tag validation in `Playbook Validation` play accepts both play-level tags (`charts`, `cluster`, `kubernetes`) and component-level tags (`cilium`, `multus`, etc.).

Example: `ansible-playbook provisioning.yaml -t multus` runs only the multus role.

## Proxy System

- `cluster_deploy_proxy` — Temporary proxy for Ansible tasks (helm, git, get_url). Not written to hosts.
- `cluster_apt_proxy` (default `false`) — Set to `true` to also route apt through the deploy proxy.
- `cluster_http_proxy` / `cluster_https_proxy` — Persistent proxy written to systemd drop-ins and `/etc/profile.d/`.
- `_proxy_environment` and `_apt_environment` are defined in `inventory/cluster/group_vars/all/main.yaml`.

## Version Overrides

All software versions are overridable via flat variables in `globals.yaml` (e.g., `cilium_chart_version`, `k3s_version`). See `globals-sample.yaml` for the full list. Role defaults use `{{ <var> | default("value") }}` pattern.

## OpenStack / Cloud Deployment Notes

Deploying this project on OpenStack (or any cloud with L2/L3 spoofing protection) has three gotchas:

1. **`cilium_non_masquerade_cidrs` MUST only contain Pod + Service CIDRs**
   Default is `["10.42.0.0/16", "10.43.0.0/16"]` — do NOT add node network (e.g. `10.0.0.0/8`). If you do, Pod → node-IP traffic is not SNATed, leaks Pod IP to the physical NIC, and OpenStack drops the packet as spoofed.

2. **`kube_ovn_pod_cidr` MUST NOT overlap the node network**
   Default `10.18.0.0/16`. If your nodes are on `10.16.x.x`, do not use `10.16.0.0/16` (the old default). This is unrelated to OpenStack — even bare-metal breaks with overlap.

3. **No `allowed_address_pairs` needed on the VM ports**
   With Cilium BPF masquerade + VXLAN tunnel, all traffic leaving the physical NIC has node IP as source. OpenStack port security can stay enabled (default).

**Service IP behavior**: Only TCP/UDP work through Service IPs. `ping <service_ip>` will fail — this is expected. Cilium's eBPF Service translation happens at the socket-layer `connect()` syscall, which ICMP doesn't use.

## Collaborator

- **Name:** Floren Munteanu
- **Work:** Engineering

### Personal Preferences

I’m a site reliability engineer specialized in:

- Advanced GitHub actions based on JS code
- Helm charts
- IaC for Kubernetes clusters
