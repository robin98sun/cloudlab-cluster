"""Assemble a self-contained, checksummed result bundle.

A run is not complete until the bundle exists and its checksums verify. Raw
observations are preserved; anything derived must be reproducible from them.
"""

import argparse
import hashlib
import json
import os
import subprocess
import time

LAYOUT = ["metrics/raw", "metrics/export", "logs", "summaries", "plots"]


def ssh(node, cmd, timeout=60):
    target = node["control"]
    if node.get("user"):
        target = "%s@%s" % (node["user"], target)
    try:
        return _ssh(target, cmd, timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def _ssh(target, cmd, timeout):
    p = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
         "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR", target, cmd],
        capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def scp(node, remote, local, timeout=120):
    target = node["control"]
    if node.get("user"):
        target = "%s@%s" % (node["user"], target)
    os.makedirs(local, exist_ok=True)
    cmd = ["scp", "-q", "-r", "-o", "BatchMode=yes",
           "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
           "-o", "LogLevel=ERROR", "%s:%s" % (target, remote), local]
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        return 124


def git_state(repo):
    def g(*a):
        p = subprocess.run(["git", "-C", repo] + list(a),
                           capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else None
    return {"commit": g("rev-parse", "HEAD"),
            "branch": g("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(g("status", "--porcelain"))}


def checksum_tree(root):
    lines = []
    for dirpath, _, files in os.walk(root):
        for name in sorted(files):
            if name == "checksums.sha256":
                continue
            full = os.path.join(dirpath, name)
            h = hashlib.sha256()
            with open(full, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    h.update(block)
            lines.append("%s  %s" % (h.hexdigest(),
                                     os.path.relpath(full, root)))
    path = os.path.join(root, "checksums.sha256")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return len(lines)


def verify_tree(root):
    bad = []
    with open(os.path.join(root, "checksums.sha256")) as fh:
        for line in fh:
            digest, rel = line.strip().split("  ", 1)
            full = os.path.join(root, rel)
            h = hashlib.sha256()
            with open(full, "rb") as f2:
                for block in iter(lambda: f2.read(1 << 20), b""):
                    h.update(block)
            if h.hexdigest() != digest:
                bad.append(rel)
    return bad


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--topology", default="topology.json")
    p.add_argument("--results-dir", default="results")
    p.add_argument("--label", default="smoke")
    p.add_argument("--run-id", default="")
    p.add_argument("--smoke-results", default="",
                   help="smoke.py --out JSON to fold into the bundle")
    p.add_argument("--note", default="")
    a = p.parse_args()

    with open(a.topology) as fh:
        topo = json.load(fh)

    run_id = a.run_id or "%s-%s-r001" % (
        time.strftime("%Y-%m-%dT%H%M%SZ", time.gmtime()), a.label)
    root = os.path.join(a.results_dir, run_id)
    for sub in LAYOUT:
        os.makedirs(os.path.join(root, sub), exist_ok=True)

    print("bundle: %s" % root)

    with open(os.path.join(root, "topology.json"), "w") as fh:
        json.dump(topo, fh, indent=2)

    hardware, software = {}, {}
    for n in topo["nodes"]:
        rc, out, _ = ssh(n, "cat /local/dcb/facts.json")
        if rc == 0:
            try:
                hardware[n["name"]] = json.loads(out)
            except ValueError:
                pass
        rc, out, _ = ssh(n, "python3 --version 2>&1; uname -r")
        if rc == 0:
            software[n["name"]] = out.strip().splitlines()

        dest = os.path.join(root, "logs", n["name"])
        if scp(n, "/local/dcb/logs", dest) != 0:
            print("  warn: no logs from %s" % n["name"])
        if scp(n, "/local/dcb/telemetry", os.path.join(root, "metrics", "raw",
                                                       n["name"])) != 0:
            print("  warn: no telemetry from %s" % n["name"])
        print("  collected %s" % n["name"])

    for name, obj in (("hardware.json", hardware), ("software.json", software)):
        with open(os.path.join(root, name), "w") as fh:
            json.dump(obj, fh, indent=2)

    # Cluster state as evidence: which pods ran where, per the API server.
    ctls = [n for n in topo["nodes"] if n["role"] == "ctl"]
    if ctls:
        rc, out, _ = ssh(ctls[0],
                         "/usr/local/bin/k3s kubectl get nodes -o wide; echo; "
                         "/usr/local/bin/k3s kubectl get pods -A -o wide")
        if rc == 0:
            with open(os.path.join(root, "summaries", "k8s-state.txt"), "w") as fh:
                fh.write(out)
        else:
            print("  warn: could not capture k8s state")

    if a.smoke_results and os.path.exists(a.smoke_results):
        with open(a.smoke_results) as fh:
            smoke = json.load(fh)
        with open(os.path.join(root, "summaries", "smoke.json"), "w") as fh:
            json.dump(smoke, fh, indent=2)
    else:
        smoke = None

    manifest = {
        "run_id": run_id,
        "label": a.label,
        "note": a.note,
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repository": git_state(os.path.dirname(os.path.abspath(__file__)) + "/.."),
        "nodes": [{"name": n["name"], "role": n["role"],
                   "control": n["control"], "ifaces": n.get("ifaces", {})}
                  for n in topo["nodes"]],
        "counts": {r: sum(1 for n in topo["nodes"] if n["role"] == r)
                   for r in ("ctl", "lg", "fe", "db")},
        "fe_instances": [f["id"] for f in topo.get("frontends", [])],
        "destinations": [d["id"] for d in topo.get("destinations", [])],
        "smoke_overall": (smoke or {}).get("overall"),
        "status": "complete",
        # Stubs, not the real controller. Recorded so no one mistakes a smoke
        # bundle for an experimental result.
        "services": "stub_lg / stub_fe (PassThroughAdmission) / stub_db",
        "measurement_valid": False,
        "invalidation_reason": "stub services; plumbing verification only",
    }
    with open(os.path.join(root, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)

    with open(os.path.join(root, "README.md"), "w") as fh:
        fh.write("# %s\n\n%s\n\nPlumbing verification with stub services. "
                 "Not a measurement.\n" % (run_id, a.note or a.label))

    n = checksum_tree(root)
    bad = verify_tree(root)
    if bad:
        print("CHECKSUM MISMATCH: %s" % bad)
        raise SystemExit(1)
    print("%d files checksummed and verified" % n)
    print("bundle complete: %s" % root)


if __name__ == "__main__":
    main()
