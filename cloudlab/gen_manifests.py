"""Generate the k8s objects for the testbed as one List manifest.

Written as JSON (a YAML subset) so generation needs no dependencies and the
output is trivially machine-checkable. Placement is deliberately explicit:
every pod is pinned to a hostname. The experiment wants placement to be a
recorded decision, not a scheduler outcome.

Pods run hostNetwork so the measured path uses the real client/backend
interfaces -- flannel exists only for cluster plumbing on the control net.
FE pods on one host take distinct ports 8081, 8082, ...
"""

import argparse
import json

PY_IMAGE = "docker.io/library/python:3.11-slim"
LETTERS = "abcdefghij"


def pod(name, node, kind, command, volumes, mounts):
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": "dcb",
            "labels": {"app": "dcb", "dcb/kind": kind},
        },
        "spec": {
            "nodeSelector": {"kubernetes.io/hostname": node},
            "hostNetwork": True,
            "dnsPolicy": "Default",
            "restartPolicy": "Always",
            "containers": [{
                "name": kind,
                "image": PY_IMAGE,
                "imagePullPolicy": "IfNotPresent",
                "command": command,
                "volumeMounts": mounts,
            }],
            "volumes": volumes,
        },
    }


def host_path(name, path, create=False):
    v = {"name": name, "hostPath": {"path": path}}
    if create:
        v["hostPath"]["type"] = "DirectoryOrCreate"
    return v


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fe-hosts", type=int, default=1)
    p.add_argument("--db-hosts", type=int, default=1)
    p.add_argument("--fe-instances", type=int, default=3)
    p.add_argument("--fe-base-port", type=int, default=8081)
    p.add_argument("--db-port", type=int, default=9091)
    p.add_argument("--out", default="dcb-testbed.yaml")
    a = p.parse_args()

    common_volumes = [
        host_path("repo", "/local/repository"),
        host_path("dcb", "/local/dcb", create=True),
    ]
    common_mounts = [
        {"name": "repo", "mountPath": "/repo", "readOnly": True},
        {"name": "dcb", "mountPath": "/local/dcb"},
    ]

    items = [{
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": "dcb"},
    }]

    for j in range(1, a.fe_hosts + 1):
        for i in range(a.fe_instances):
            name = "fe%d-%s" % (j, LETTERS[i])
            port = a.fe_base_port + i
            items.append(pod(
                name, "fe%d" % j, "fe",
                ["python3", "/repo/services/stub_fe.py",
                 "--port", str(port),
                 "--instance-id", name,
                 "--db-port", str(a.db_port),
                 "--destinations-file", "/local/dcb/destinations.json",
                 "--telemetry-dir", "/local/dcb/telemetry"],
                common_volumes, common_mounts))

    for k in range(1, a.db_hosts + 1):
        items.append(pod(
            "db%d" % k, "db%d" % k, "db",
            ["python3", "/repo/services/stub_db.py",
             "--port", str(a.db_port),
             "--data-dir", "/data/dcb"],
            common_volumes + [host_path("data", "/mnt/data", create=True)],
            common_mounts + [{"name": "data", "mountPath": "/data"}]))

    doc = {"apiVersion": "v1", "kind": "List", "items": items}
    with open(a.out, "w") as fh:
        json.dump(doc, fh, indent=2)
    print("wrote %s: %d objects (%d fe pods, %d db pods)"
          % (a.out, len(items), a.fe_hosts * a.fe_instances, a.db_hosts))


if __name__ == "__main__":
    main()
