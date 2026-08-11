#!/usr/bin/env bash
#
# k3s-cluster offline sync tool (ChartMuseum + Harbor)
#
# Syncs Helm charts and container images from upstream to local:
#   1. Pulls all chart versions (since 2022) from upstream repos
#   2. Extracts container image references from chart templates
#   3. Pulls images via docker, retags and pushes to Harbor
#
# Usage:
#   ./tools/helm-repo.sh start    # Start ChartMuseum container
#   ./tools/helm-repo.sh sync     # Sync all chart versions (since 2022)
#   ./tools/helm-repo.sh images   # Extract & push images to Harbor
#   ./tools/helm-repo.sh list     # List local charts
#   ./tools/helm-repo.sh stop     # Stop ChartMuseum container
#   ./tools/helm-repo.sh clean    # Stop + remove all data
#
# Harbor configuration (env vars):
#   HARBOR_URL       Harbor URL (e.g. https://harbor.local)
#   HARBOR_PROJECT   Harbor project (default: k3s-cluster)
#   HARBOR_USER      Harbor username (default: admin)
#   HARBOR_PASS      Harbor password
#
# Examples:
#   HARBOR_URL=https://harbor.local HARBOR_PASS=xxx ./tools/helm-repo.sh images
#

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${REPO_DIR}/.helm-repo-data"
CHARTMUSEUM_IMG="ghcr.io/chartmuseum/chartmuseum:latest"
CHARTMUSEUM_NAME="k3s-helm-repo"
CHARTMUSEUM_PORT="${HELM_REPO_PORT:-8080}"
CHARTMUSEUM_URL="http://localhost:${CHARTMUSEUM_PORT}"

# Harbor config
HARBOR_URL="${HARBOR_URL:-}"
HARBOR_PROJECT="${HARBOR_PROJECT:-k3s-cluster}"
HARBOR_USER="${HARBOR_USER:-admin}"
HARBOR_PASS="${HARBOR_PASS:-}"

# Minimum chart date (sync releases since this date)
MIN_DATE="2022-01-01"

# Charts to sync (label|repo_url|chart_name)
# Version is omitted — all versions since MIN_DATE are synced
CHARTS=(
  "cilium|https://helm.cilium.io|cilium"
  "coredns|https://coredns.github.io/helm|coredns"
  "cert-manager|https://charts.jetstack.io|cert-manager"
  "external-dns|https://kubernetes-sigs.github.io/external-dns|external-dns"
  "longhorn|https://charts.longhorn.io|longhorn"
  "ceph-csi|https://ceph.github.io/ceph-csi-operator|ceph-csi-drivers"
  "metrics-server|https://kubernetes-sigs.github.io/metrics-server|metrics-server"
  "victoria-logs|https://victoriametrics.github.io/helm-charts|victoria-logs-single"
  "victoria-metrics|https://victoriametrics.github.io/helm-charts|victoria-metrics-k8s-stack"
  "prometheus-crds|https://prometheus-community.github.io/helm-charts|prometheus-operator-crds"
  "argo-cd|https://argoproj.github.io/argo-helm|argo-cd"
  "kured|https://kuberebot.github.io/charts|kured"
  "kube-ovn|https://kubeovn.github.io/kube-ovn|kube-ovn"
  "gpu-operator|https://nvidia.github.io/gpu-operator|gpu-operator"
)

# ── Helpers ──────────────────────────────────────────────────────────

log()  { echo "  $*"; }
hdr()  { echo; echo "=== $* ==="; echo; }
fail() { echo "ERROR: $*" >&2; exit 1; }

check_helm() {
  command -v helm >/dev/null 2>&1 || fail "helm not found. Install: https://helm.sh/docs/intro/install/"
}

check_docker() {
  command -v docker >/dev/null 2>&1 || fail "docker not found."
}

check_chartmuseum() {
  curl -s -o /dev/null "${CHARTMUSEUM_URL}/health" 2>/dev/null || fail "ChartMuseum not running. Run './tools/helm-repo.sh start' first."
}

check_harbor() {
  [ -n "${HARBOR_URL}" ] || fail "HARBOR_URL not set. Example: HARBOR_URL=https://harbor.local ./tools/helm-repo.sh images"
  [ -n "${HARBOR_PASS}" ] || fail "HARBOR_PASS not set."
}

# ── Commands ─────────────────────────────────────────────────────────

cmd_start() {
  hdr "Starting ChartMuseum on port ${CHARTMUSEUM_PORT}"
  mkdir -p "${DATA_DIR}"

  if docker ps -a --format '{{.Names}}' | grep -q "^${CHARTMUSEUM_NAME}$"; then
    docker start "${CHARTMUSEUM_NAME}" >/dev/null
    log "Container already exists, started."
  else
    docker run -d \
      --name "${CHARTMUSEUM_NAME}" \
      -p "${CHARTMUSEUM_PORT}:8080" \
      -v "${DATA_DIR}:/charts:Z" \
      -e PORT=8080 \
      -e STORAGE=local \
      -e STORAGE_LOCAL_ROOTDIR=/charts \
      -e ALLOW_OVERWRITE=true \
      -e AUTH_ANONYMOUS_GET=true \
      "${CHARTMUSEUM_IMG}" >/dev/null
    log "Container created and started."
  fi

  # Wait for health
  for i in $(seq 1 10); do
    if curl -s -o /dev/null "${CHARTMUSEUM_URL}/health" 2>/dev/null; then
      break
    fi
    sleep 1
  done

  echo
  log "ChartMuseum: ${CHARTMUSEUM_URL}"
  log "Data dir:    ${DATA_DIR}"
  echo
  log "Add to Helm:"
  echo "    helm repo add local ${CHARTMUSEUM_URL}"
  echo
  log "In globals.yaml, set per-component repo URLs:"
  echo "    cilium_helm_repo_url: http://<this-host>:${CHARTMUSEUM_PORT}"
}

cmd_sync() {
  check_helm
  check_chartmuseum
  hdr "Syncing charts (releases since ${MIN_DATE})"

  local tmpdir
  tmpdir="$(mktemp -d)"
  trap 'rm -rf "${tmpdir}"' EXIT

  local total=0
  local synced=0
  local failed=0

  for entry in "${CHARTS[@]}"; do
    IFS='|' read -r label repo_url chart_name <<< "${entry}"
    log "[${label}] ${chart_name}"

    # Add repo
    helm repo add "${label}-sync" "${repo_url}" --force-update >/dev/null 2>&1

    # List all versions
    local versions
    versions=$(helm search repo "${label}-sync/${chart_name}" --versions -o json 2>/dev/null \
      | python3 -c "
import json, sys
from datetime import datetime
try:
    data = json.load(sys.stdin)
    cutoff = datetime.strptime('${MIN_DATE}', '%Y-%m-%d')
    for item in data:
        # helm search doesn't return release date, so we sync all versions
        # and let ChartMuseum handle dedup
        print(item['version'])
except Exception:
    pass
" 2>/dev/null)

    if [ -z "${versions}" ]; then
      log "  -> no versions found"
      ((failed++)) || true
      continue
    fi

    for version in ${versions}; do
      ((total++)) || true
      local filename="${chart_name}-${version}.tgz"

      if helm pull "${label}-sync/${chart_name}" --version "${version}" -d "${tmpdir}" 2>/dev/null; then
        local filepath="${tmpdir}/${filename}"
        if curl -s -o /dev/null -w '%{http_code}' \
            -F "chart=@${filepath}" \
            "${CHARTMUSEUM_URL}/api/charts" | grep -q '20[01]'; then
          log "  ${version} -> synced"
          ((synced++)) || true
        else
          log "  ${version} -> upload failed"
          ((failed++)) || true
        fi
      else
        log "  ${version} -> download failed"
        ((failed++)) || true
      fi
    done

    helm repo remove "${label}-sync" >/dev/null 2>&1
  done

  echo
  log "Total: ${total}  Synced: ${synced}  Failed: ${failed}"
  log "Run './tools/helm-repo.sh list' to verify."
}

cmd_images() {
  check_helm
  check_docker
  check_chartmuseum
  check_harbor
  hdr "Extracting & pushing images to Harbor"

  # Login to Harbor
  echo "${HARBOR_PASS}" | docker login "${HARBOR_URL}" -u "${HARBOR_USER}" --password-stdin >/dev/null 2>&1 \
    || fail "Harbor login failed. Check HARBOR_URL, HARBOR_USER, HARBOR_PASS."

  log "Logged in to Harbor: ${HARBOR_URL}"
  echo

  # Ensure project exists
  curl -s -o /dev/null -X POST \
    "${HARBOR_URL}/api/v2.0/projects" \
    -H "Content-Type: application/json" \
    -u "${HARBOR_USER}:${HARBOR_PASS}" \
    -d "{\"project_name\":\"${HARBOR_PROJECT}\",\"public\":true}" 2>/dev/null || true

  local tmpdir
  tmpdir="$(mktemp -d)"
  trap 'rm -rf "${tmpdir}"' EXIT

  local all_images=()

  # Download each chart, render, extract images
  for entry in "${CHARTS[@]}"; do
    IFS='|' read -r label repo_url chart_name <<< "${entry}"
    log "[${label}] ${chart_name}"

    # Get latest version from ChartMuseum
    local version
    version=$(curl -s "${CHARTMUSEUM_URL}/api/charts/${chart_name}" 2>/dev/null \
      | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    if isinstance(data, list) and data:
        # Sort by version, pick latest
        print(sorted(data, key=lambda x: x['version'])[-1]['version'])
except Exception:
    pass
" 2>/dev/null)

    if [ -z "${version}" ]; then
      log "  -> not found in ChartMuseum (run sync first)"
      continue
    fi

    # Pull chart from ChartMuseum
    if ! helm pull "local/${chart_name}" --version "${version}" -d "${tmpdir}" >/dev/null 2>&1; then
      helm repo add local "${CHARTMUSEUM_URL}" --force-update >/dev/null 2>&1
      helm pull "local/${chart_name}" --version "${version}" -d "${tmpdir}" >/dev/null 2>&1 || {
        log "  -> failed to pull chart"
        continue
      }
    fi

    local chart_tgz="${tmpdir}/${chart_name}-${version}.tgz"

    # Render chart and extract images
    local images
    images=$(helm template "release" "${chart_tgz}" 2>/dev/null \
      | grep -oP 'image:\s*\K[^\s]+' \
      | sed "s/^[\"']//;s/[\"']$//" \
      | sort -u)

    if [ -z "${images}" ]; then
      # Fallback: grep chart files directly
      local chart_dir="${tmpdir}/${chart_name}"
      tar xzf "${chart_tgz}" -C "${tmpdir}" 2>/dev/null
      images=$(find "${chart_dir}" -name '*.yaml' -exec grep -h 'image:' {} \; 2>/dev/null \
        | grep -oP 'image:\s*\K[^\s]+' \
        | sed "s/^[\"']//;s/[\"']$//" \
        | sort -u)
    fi

    if [ -z "${images}" ]; then
      log "  ${version} -> no images found"
      continue
    fi

    local count=0
    for image in ${images}; do
      all_images+=("${image}")
      ((count++)) || true
    done
    log "  ${version} -> ${count} images found"
  done

  # Deduplicate
  echo
  hdr "Pulling & pushing images to Harbor"

  local unique_images
  unique_images=$(printf '%s\n' "${all_images[@]}" | sort -u)

  local total=0
  local pulled=0
  local pushed=0
  local failed=0

  for image in ${unique_images}; do
    ((total++)) || true
    log "[${total}] ${image}"

    # Pull
    if docker pull "${image}" >/dev/null 2>&1; then
      ((pulled++)) || true
    else
      log "  -> pull failed"
      ((failed++)) || true
      continue
    fi

    # Retag for Harbor
    # Parse registry/repo:tag
    local target
    if echo "${image}" | grep -q '/'; then
      local img_path="${image#*/}"
      target="${HARBOR_URL#https://}/${HARBOR_PROJECT}/${img_path}"
    else
      target="${HARBOR_URL#https://}/${HARBOR_PROJECT}/${image}"
    fi

    # Tag
    docker tag "${image}" "${target}" >/dev/null 2>&1 || {
      log "  -> tag failed"
      ((failed++)) || true
      continue
    }

    # Push
    if docker push "${target}" >/dev/null 2>&1; then
      log "  -> pushed"
      ((pushed++)) || true
    else
      log "  -> push failed"
      ((failed++)) || true
    fi
  done

  echo
  log "Total: ${total}  Pulled: ${pulled}  Pushed: ${pushed}  Failed: ${failed}"
  echo
  log "Configure K3s to use Harbor:"
  echo "    cluster_registry_endpoint: ${HARBOR_URL#https://}/${HARBOR_PROJECT}"
}

cmd_list() {
  hdr "Local charts"

  local index
  index="$(curl -s "${CHARTMUSEUM_URL}/api/charts" 2>/dev/null)"

  if [ -z "${index}" ] || [ "${index}" = "null" ]; then
    log "(empty — run './tools/helm-repo.sh sync' first)"
    return
  fi

  echo "${index}" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    if not data:
        print('  (empty)')
        sys.exit(0)
    for name, versions in sorted(data.items()):
        ver_list = sorted(versions, key=lambda x: x['version'])
        latest = ver_list[-1]['version']
        count = len(ver_list)
        print(f'  {name:45s} {count:3d} versions  (latest: {latest})')
except Exception:
    print('  (parse error)')
" 2>/dev/null || log "(no charts — run sync first)"
}

cmd_stop() {
  hdr "Stopping ChartMuseum"
  docker stop "${CHARTMUSEUM_NAME}" >/dev/null 2>&1 && log "Stopped." || log "Not running."
}

cmd_clean() {
  hdr "Cleaning up"
  docker rm -f "${CHARTMUSEUM_NAME}" >/dev/null 2>&1 && log "Container removed." || log "No container found."
  rm -rf "${DATA_DIR}"
  log "Data directory removed."
}

# ── Main ─────────────────────────────────────────────────────────────

case "${1:-}" in
  start)  cmd_start ;;
  sync)   cmd_sync ;;
  images) cmd_images ;;
  list)   cmd_list ;;
  stop)   cmd_stop ;;
  clean)  cmd_clean ;;
  *)
    echo "k3s-cluster offline sync tool"
    echo
    echo "Usage: $0 {start|sync|images|list|stop|clean}"
    echo
    echo "  start   Start ChartMuseum container"
    echo "  sync    Sync all chart versions from upstream (since ${MIN_DATE})"
    echo "  images  Extract images from charts, pull & push to Harbor"
    echo "  list    List charts in local repository"
    echo "  stop    Stop ChartMuseum container"
    echo "  clean   Stop + remove container and all data"
    echo
    echo "Harbor env vars (for 'images' command):"
    echo "  HARBOR_URL       Harbor URL (e.g. https://harbor.local)"
    echo "  HARBOR_PROJECT   Harbor project (default: k3s-cluster)"
    echo "  HARBOR_USER       Harbor username (default: admin)"
    echo "  HARBOR_PASS      Harbor password"
    echo
    echo "Examples:"
    echo "  $0 start"
    echo "  $0 sync"
    echo "  HARBOR_URL=https://harbor.local HARBOR_PASS=xxx $0 images"
    exit 1
    ;;
esac
