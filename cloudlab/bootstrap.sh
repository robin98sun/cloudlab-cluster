#!/usr/bin/env bash
# Two-layer node bootstrap. Runs as a CloudLab startup service on every boot.
#
#   Layer 1 (bake layer)  packages, k3s binary, prefetched container images.
#                         Skipped when /etc/dcb-image-version matches -- i.e.
#                         when booting from a golden image. This is the slow,
#                         network-dependent part; baking it is what makes the
#                         ~15-minute redeploy possible.
#   Layer 2 (boot layer)  per-instantiation config: clock, dirs, facts,
#                         k3s cluster formation, manifest generation.
#                         Runs every boot; must stay idempotent and fast.
#
# Usage: bootstrap.sh <ctl|fe|db|lg> [--fe-hosts N --db-hosts N --lg-hosts N
#                                     --fe-instances N]   (ctl only)
set -euo pipefail

ROLE="${1:?usage: bootstrap.sh <ctl|fe|db|lg> [opts]}"; shift || true
FE_HOSTS=1; DB_HOSTS=1; LG_HOSTS=0; FE_INSTANCES=3
while [ $# -gt 0 ]; do
    case "$1" in
        --fe-hosts)     FE_HOSTS="$2";     shift 2 ;;
        --db-hosts)     DB_HOSTS="$2";     shift 2 ;;
        --lg-hosts)     LG_HOSTS="$2";     shift 2 ;;
        --fe-instances) FE_INSTANCES="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

IMAGE_LAYER=1          # bump when the bake layer's contents change, then rebake
DB_PORT=9091
# Private testbed on CloudLab's control network; static token keeps cluster
# formation dependency-free. Not a pattern for anything internet-facing.
TOKEN="dcb-testbed-2c9f7d41"
K3S_INSTALLER=/usr/local/share/dcb/k3s-install.sh
PY_IMAGE="docker.io/library/python:3.11-slim"

REPO=/local/repository
STATE=/local/dcb
LOGDIR="$STATE/logs"

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo -H"

$SUDO mkdir -p "$LOGDIR" "$STATE/telemetry"
$SUDO chmod 0777 "$STATE" "$LOGDIR" "$STATE/telemetry"
exec > >(tee -a "$LOGDIR/bootstrap.log") 2>&1
echo "=== bootstrap role=$ROLE image_layer_wanted=$IMAGE_LAYER at $(date -Is) ==="

# ---------------------------------------------------------------- layer 1 ---
HAVE_LAYER="$(cat /etc/dcb-image-version 2>/dev/null || echo none)"
if [ "$HAVE_LAYER" = "$IMAGE_LAYER" ]; then
    echo "bake layer $IMAGE_LAYER present (golden image); skipping downloads"
else
    echo "bake layer: have=$HAVE_LAYER want=$IMAGE_LAYER; installing"
    export DEBIAN_FRONTEND=noninteractive
    for _ in 1 2 3; do $SUDO apt-get update -qq && break || sleep 5; done
    $SUDO apt-get install -y -qq chrony python3 jq curl skopeo \
        iproute2 iputils-ping sysstat >/dev/null

    $SUDO mkdir -p /usr/local/share/dcb /var/lib/rancher/k3s/agent/images
    # Cache the installer and fetch the k3s binary without starting anything.
    # The k3s version is thereby frozen into the golden image; facts.json
    # records it per node and the run manifest picks it up from there.
    $SUDO curl -sfL https://get.k3s.io -o "$K3S_INSTALLER"
    INSTALL_K3S_SKIP_START=true INSTALL_K3S_SKIP_ENABLE=true \
        $SUDO -E sh "$K3S_INSTALLER" >/dev/null

    # Prefetch the pod base image as a k3s auto-import tarball so pod start
    # needs no registry. Best-effort: a failed prefetch means a slower first
    # pod start, not a broken node.
    $SUDO skopeo copy "docker://$PY_IMAGE" \
        "docker-archive:/var/lib/rancher/k3s/agent/images/python-3.11-slim.tar:$PY_IMAGE" \
        >/dev/null 2>&1 \
        || echo "WARN: image prefetch failed; pods will pull from the registry"

    echo "$IMAGE_LAYER" | $SUDO tee /etc/dcb-image-version >/dev/null
fi

# ---------------------------------------------------------------- layer 2 ---
$SUDO systemctl enable --now chrony >/dev/null 2>&1 || \
    $SUDO systemctl enable --now chronyd >/dev/null 2>&1 || true
$SUDO chronyc makestep >/dev/null 2>&1 || true

DATA_DIR=/mnt/data
if mountpoint -q "$DATA_DIR" 2>/dev/null; then
    $SUDO mkdir -p "$DATA_DIR/dcb"; $SUDO chmod 0777 "$DATA_DIR/dcb"
    DATA_BACKING="blockstore"
else
    DATA_DIR="$STATE/data"
    $SUDO mkdir -p "$DATA_DIR"; $SUDO chmod 0777 "$DATA_DIR"
    DATA_BACKING="rootfs"
    [ "$ROLE" = "db" ] && echo "WARNING: no blockstore; data on root filesystem"
fi

python3 - "$ROLE" "$DATA_DIR" "$DATA_BACKING" "$STATE/telemetry" <<'PYFACTS' | $SUDO tee "$STATE/facts.json" >/dev/null
import json, os, socket, subprocess, sys
role, data_dir, data_backing, telem_dir = sys.argv[1:5]
ifaces = {}
out = subprocess.run(["ip", "-o", "-4", "addr", "show"],
                     capture_output=True, text=True).stdout
for line in out.splitlines():
    f = line.split()
    if len(f) >= 4:
        ifaces[f[1]] = f[3].split("/")[0]
def read(path):
    try:
        return open(path).read().strip()
    except OSError:
        return ""
def cmd(*a):
    try:
        return subprocess.run(a, capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""
print(json.dumps({
    "role": role,
    "hostname": socket.gethostname(),
    "short_name": socket.gethostname().split(".")[0],
    "interfaces": ifaces,
    "data_dir": data_dir,
    "data_backing": data_backing,
    "telemetry_dir": telem_dir,
    "cpus": os.cpu_count(),
    "kernel": os.uname().release,
    "product": read("/sys/class/dmi/id/product_name"),
    "image_layer": read("/etc/dcb-image-version"),
    "k3s_version": cmd("/usr/local/bin/k3s", "--version").splitlines()[0]
                   if os.path.exists("/usr/local/bin/k3s") else "",
}, indent=2))
PYFACTS

# k3s cluster formation. All control-plane traffic rides CloudLab's control
# network (default route), keeping the client/backend LANs clean. Measured
# pods use hostNetwork, so flannel never touches the measured path.
# Resolve ctl1 from the CloudLab manifest: hostname -f can be stale during
# early boot, and /etc/hosts maps bare "ctl1" to an experiment LAN that db
# hosts deliberately cannot reach. k3s traffic belongs on the control net.
read -r CTL_NAME CTL_IP <<<"$(geni-get manifest 2>/dev/null | python3 -c '
import sys, xml.etree.ElementTree as ET
def t(e): return e.tag.split("}", 1)[-1]
try:
    root = ET.parse(sys.stdin).getroot()
except Exception:
    sys.exit(0)
for n in root.iter():
    if t(n) == "node" and n.get("client_id") == "ctl1":
        for s in n.iter():
            if t(s) == "host" and s.get("name"):
                print(s.get("name"), s.get("ipv4") or "")
                sys.exit(0)
' || true)"
if [ -n "${CTL_NAME:-}" ] && getent hosts "$CTL_NAME" >/dev/null 2>&1; then
    SERVER_HOST="$CTL_NAME"
elif [ -n "${CTL_IP:-}" ]; then
    SERVER_HOST="$CTL_IP"
else
    SERVER_HOST="ctl1.$(hostname -f | cut -d. -f2-)"
fi
SERVER_URL="https://${SERVER_HOST}:6443"
echo "k3s server endpoint: $SERVER_URL"

case "$ROLE" in
    ctl)
        INSTALL_K3S_SKIP_DOWNLOAD=true INSTALL_K3S_SKIP_START=true \
        INSTALL_K3S_SKIP_ENABLE=true K3S_TOKEN="$TOKEN" \
        INSTALL_K3S_EXEC="server --disable traefik --disable servicelb \
--disable metrics-server --write-kubeconfig-mode 644 --node-label dcb/role=ctl" \
            $SUDO -E sh "$K3S_INSTALLER" >/dev/null
        # Never block bootstrap on service readiness; the wait loop below
        # (and smoke S10) verify convergence instead.
        $SUDO systemctl enable k3s >/dev/null 2>&1 || true
        $SUDO systemctl restart --no-block k3s

        EXPECTED=$((1 + FE_HOSTS + DB_HOSTS + LG_HOSTS))
        echo "waiting for $EXPECTED Ready nodes"
        for _ in $(seq 1 90); do
            READY=$(/usr/local/bin/k3s kubectl get nodes --no-headers 2>/dev/null \
                    | awk '$2 == "Ready"' | wc -l || echo 0)
            [ "$READY" -ge "$EXPECTED" ] && break
            sleep 5
        done
        echo "ready nodes: ${READY:-0}/$EXPECTED (pods reconcile as stragglers join)"

        # The auto-deploy dir applies (and re-applies) whatever lands here.
        python3 "$REPO/cloudlab/gen_manifests.py" \
            --fe-hosts "$FE_HOSTS" --db-hosts "$DB_HOSTS" \
            --fe-instances "$FE_INSTANCES" --db-port "$DB_PORT" \
            --out /tmp/dcb-testbed.yaml
        $SUDO mkdir -p /var/lib/rancher/k3s/server/manifests
        $SUDO cp /tmp/dcb-testbed.yaml /var/lib/rancher/k3s/server/manifests/
        ;;
    fe|db|lg)
        INSTALL_K3S_SKIP_DOWNLOAD=true INSTALL_K3S_SKIP_START=true \
        INSTALL_K3S_SKIP_ENABLE=true K3S_URL="$SERVER_URL" K3S_TOKEN="$TOKEN" \
        INSTALL_K3S_EXEC="agent --node-label dcb/role=${ROLE}-host" \
            $SUDO -E sh "$K3S_INSTALLER" >/dev/null
        # Non-blocking: a systemctl start that waits for join would hang
        # bootstrap forever if the server is unreachable.
        $SUDO systemctl enable k3s-agent >/dev/null 2>&1 || true
        $SUDO systemctl restart --no-block k3s-agent
        echo "k3s agent joining via $SERVER_URL (non-blocking)"
        ;;
    *)
        echo "unknown role: $ROLE" >&2; exit 2 ;;
esac

echo "$IMAGE_LAYER" | $SUDO tee "$STATE/boot.done" >/dev/null
echo "=== bootstrap complete role=$ROLE at $(date -Is) ==="
