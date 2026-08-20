"""Build topology.json for the k3s layout.

Physical hosts carry roles ctl|lg|fe|db. On top of them:
  frontends    -- FE pod instances: (id, host, client ip, port)
  destinations -- DB pods: (id, host, backend ip, port)

Prefer `from-manifest` (the CloudLab manifest is authoritative and belongs in
every result bundle); `derive` covers the gap before you download one.
"""

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET

ROLE_OF = re.compile(r"^(ctl|lg|fe|db)\d*$")
LAN_BY_PREFIX = {"10.10.1.": "client", "10.10.2.": "backend"}
LETTERS = "abcdefghij"


def strip_ns(tag):
    return tag.split("}", 1)[-1]


def parse_manifest(path):
    root = ET.parse(path).getroot()
    nodes = []
    for el in root.iter():
        if strip_ns(el.tag) != "node":
            continue
        name = el.get("client_id")
        if not name:
            continue
        m = ROLE_OF.match(name)
        if not m:
            continue
        entry = {"name": name, "role": m.group(1), "control": None,
                 "user": None, "hardware": None, "ifaces": {}}
        for sub in el.iter():
            t = strip_ns(sub.tag)
            if t == "login" and entry["control"] is None:
                entry["control"] = sub.get("hostname")
                entry["user"] = sub.get("username")
            elif t == "ip" and sub.get("type") == "ipv4":
                addr = sub.get("address") or ""
                lan = LAN_BY_PREFIX.get(addr[:8])
                if lan:
                    entry["ifaces"][lan] = addr
            elif t == "node_type" and sub.get("type_name"):
                entry["hardware"] = entry["hardware"] or sub.get("type_name")
        nodes.append(entry)
    if not nodes:
        sys.exit("no ctl/lg/fe/db nodes found in manifest -- wrong file?")
    return nodes


def derive_nodes(user, domain, lg, fe, db):
    def node(name, role, ifaces):
        return {"name": name, "role": role, "control": "%s.%s" % (name, domain),
                "user": user, "hardware": None, "ifaces": ifaces}
    nodes = [node("ctl1", "ctl", {"client": "10.10.1.10"})]
    for i in range(1, lg + 1):
        nodes.append(node("lg%d" % i, "lg", {"client": "10.10.1.%d" % (10 + i)}))
    for j in range(1, fe + 1):
        nodes.append(node("fe%d" % j, "fe",
                          {"client": "10.10.1.%d" % (20 + j),
                           "backend": "10.10.2.%d" % (20 + j)}))
    for k in range(1, db + 1):
        nodes.append(node("db%d" % k, "db",
                          {"backend": "10.10.2.%d" % (30 + k)}))
    return nodes


def build(nodes, fe_instances, fe_base_port, db_port):
    frontends, destinations = [], []
    for n in nodes:
        if n["role"] == "fe":
            for i in range(fe_instances):
                frontends.append({
                    "id": "%s-%s" % (n["name"], LETTERS[i]),
                    "host": n["name"],
                    "ip": n["ifaces"].get("client"),
                    "port": fe_base_port + i,
                })
        elif n["role"] == "db":
            destinations.append({
                "id": n["name"],
                "host": n["name"],
                "ip": n["ifaces"].get("backend"),
                "port": db_port,
            })
    return {"nodes": nodes, "frontends": frontends,
            "destinations": destinations,
            "lans": {"client": "10.10.1.0/24", "backend": "10.10.2.0/24"}}


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("from-manifest")
    m.add_argument("manifest")

    d = sub.add_parser("derive")
    d.add_argument("--user", required=True)
    d.add_argument("--domain", required=True,
                   help="e.g. testbed-smoke.myproject.utah.cloudlab.us")
    d.add_argument("--lg", type=int, default=0)
    d.add_argument("--fe", type=int, default=1)
    d.add_argument("--db", type=int, default=1)

    for q in (m, d):
        q.add_argument("--fe-instances", type=int, default=3)
        q.add_argument("--fe-base-port", type=int, default=8081)
        q.add_argument("--db-port", type=int, default=9091)
        q.add_argument("--out", default="topology.json")

    a = p.parse_args()
    nodes = (parse_manifest(a.manifest) if a.cmd == "from-manifest"
             else derive_nodes(a.user, a.domain, a.lg, a.fe, a.db))
    topo = build(nodes, a.fe_instances, a.fe_base_port, a.db_port)
    with open(a.out, "w") as fh:
        json.dump(topo, fh, indent=2)
    counts = {}
    for n in nodes:
        counts[n["role"]] = counts.get(n["role"], 0) + 1
    print("wrote %s: hosts %s, %d fe instances, %d destinations"
          % (a.out, counts, len(topo["frontends"]), len(topo["destinations"])))


if __name__ == "__main__":
    main()
