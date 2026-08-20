"""Give every node admin kubectl access.

k3s writes the admin kubeconfig only on the server. This copies it to each
agent at /etc/rancher/k3s/k3s.yaml -- the path k3s's bundled kubectl reads by
default -- with the server address rewritten from 127.0.0.1 to ctl1's
control-network IP (the IP, not a hostname, because the server's TLS cert
covers its own names and IPs only).

Testbed trade-off, made deliberately: every node holds admin credentials.
"""

import argparse
import json
import subprocess
import sys

SSH = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
       "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR"]


def ssh(node, cmd, stdin=None):
    target = "%s@%s" % (node["user"], node["control"]) if node.get("user") \
        else node["control"]
    p = subprocess.run(SSH + [target, cmd], input=stdin,
                       capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topology", default="topology.json")
    a = ap.parse_args()
    topo = json.load(open(a.topology))
    nodes = topo["nodes"]
    ctl = next(n for n in nodes if n["role"] == "ctl")

    rc, ip, err = ssh(ctl, "ip route get 1.1.1.1 | awk '{print $7; exit}'")
    if rc != 0 or not ip.strip():
        sys.exit("cannot determine ctl control IP: " + err)
    rc, cfg, err = ssh(ctl, "cat /etc/rancher/k3s/k3s.yaml")
    if rc != 0:
        sys.exit("cannot read kubeconfig on ctl1: " + err)
    cfg = cfg.replace("https://127.0.0.1:6443",
                      "https://%s:6443" % ip.strip())

    for n in nodes:
        if n["role"] == "ctl":
            continue
        rc, _, err = ssh(n, "sudo mkdir -p /etc/rancher/k3s && "
                            "sudo tee /etc/rancher/k3s/k3s.yaml >/dev/null && "
                            "sudo chmod 644 /etc/rancher/k3s/k3s.yaml",
                         stdin=cfg)
        if rc != 0:
            sys.exit("failed on %s: %s" % (n["name"], err))
        rc, out, _ = ssh(n, "kubectl get nodes --no-headers 2>/dev/null | wc -l")
        print("%s: kubeconfig installed, kubectl sees %s nodes"
              % (n["name"], out.strip() or "0"))


if __name__ == "__main__":
    main()
