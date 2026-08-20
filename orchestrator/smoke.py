"""Smoke checks for the testbed testbed (k3s layout).

Runs from your workstation over the CloudLab control network. Physical checks
(S01-S09) verify hosts and wires; cluster checks (S10-S13) verify that the
pods landed where placement says and that the request path and its accounting
behave. A failure names the wire, placement rule, or invariant that broke.
"""

import argparse
import json
import subprocess
import sys
import time

PASS, FAIL, SKIP, WARN = "PASS", "FAIL", "SKIP", "WARN"
REGISTRY = []
K3S = "/usr/local/bin/k3s"


def check(cid, title):
    def deco(fn):
        REGISTRY.append((cid, title, fn))
        return fn
    return deco


class Ctx:
    def __init__(self, topo, args):
        self.topo = topo
        self.args = args
        self.nodes = topo["nodes"]
        self.frontends = topo.get("frontends", [])
        self.destinations = topo.get("destinations", [])

    def by_role(self, role):
        return [n for n in self.nodes if n["role"] == role]

    def host(self, name):
        for n in self.nodes:
            if n["name"] == name:
                return n
        raise KeyError(name)

    def drivers(self):
        """Where load generation runs: dedicated lg hosts, else ctl1."""
        return self.by_role("lg") or self.by_role("ctl")

    def ssh(self, node, cmd, timeout=60):
        target = node["control"]
        if node.get("user"):
            target = "%s@%s" % (node["user"], target)
        try:
            p = subprocess.run(
                ["ssh", "-o", "BatchMode=yes",
                 "-o", "StrictHostKeyChecking=accept-new",
                 "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR",
                 target, cmd],
                capture_output=True, text=True, timeout=timeout)
            return p.returncode, p.stdout.strip(), p.stderr.strip()
        except subprocess.TimeoutExpired:
            return 124, "", "ssh timeout after %ss" % timeout

    def ssh_parallel(self, jobs, timeout=120):
        """Run (node, cmd) pairs concurrently -- a start barrier is only a
        barrier if the generators are actually launched at the same time."""
        import concurrent.futures as cf
        out = [None] * len(jobs)
        with cf.ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
            futs = {pool.submit(self.ssh, n, c, timeout): i
                    for i, (n, c) in enumerate(jobs)}
            for f in cf.as_completed(futs):
                out[futs[f]] = f.result()
        return out

    def facts(self, node):
        rc, out, _ = self.ssh(node, "cat /local/testbed/facts.json")
        if rc != 0:
            return None
        try:
            return json.loads(out)
        except ValueError:
            return None

    def fe_metrics(self, f):
        """Metrics for one FE instance, queried on its host."""
        rc, out, _ = self.ssh(self.host(f["host"]),
                              "curl -sf --max-time 5 http://127.0.0.1:%d/metrics"
                              % f["port"])
        if rc != 0:
            return None
        try:
            return json.loads(out)
        except ValueError:
            return None


# --------------------------------------------------------------------------
# S01-S03  provisioning
# --------------------------------------------------------------------------

@check("S01", "every host reachable over the control network")
def s01(ctx):
    bad = []
    for n in ctx.nodes:
        rc, out, err = ctx.ssh(n, "echo ok", timeout=20)
        if rc != 0 or out != "ok":
            bad.append("%s (%s)" % (n["name"], err or "rc=%d" % rc))
    if bad:
        return FAIL, "unreachable: " + ", ".join(bad)
    return PASS, "%d/%d hosts reachable" % (len(ctx.nodes), len(ctx.nodes))


@check("S02", "bootstrap layers complete (bake layer + boot layer)")
def s02(ctx):
    bad, details = [], []
    for n in ctx.nodes:
        rc, out, _ = ctx.ssh(
            n, "echo \"$(cat /etc/testbed-image-version 2>/dev/null || echo -):"
               "$(cat /local/testbed/boot.done 2>/dev/null || echo -)\"")
        image, boot = (out.split(":") + ["-"])[:2] if rc == 0 else ("-", "-")
        if boot == "-":
            rc2, tail, _ = ctx.ssh(
                n, "tail -n 3 /local/testbed/logs/bootstrap.log 2>/dev/null")
            bad.append("%s: boot layer incomplete (%s)"
                       % (n["name"], tail.replace("\n", " | ") or "no log"))
        else:
            details.append("%s=img:%s/boot:%s" % (n["name"], image, boot))
    if bad:
        return FAIL, " ;; ".join(bad)
    golden = all(d.split("img:")[1][0] != "-" for d in details)
    note = "" if golden else " (no bake marker: base-image boot, slower)"
    return PASS, ", ".join(details) + note


@check("S03", "allocated hardware recorded and homogeneous")
def s03(ctx):
    seen, missing = {}, []
    for n in ctx.nodes:
        f = ctx.facts(n)
        if not f:
            missing.append(n["name"])
            continue
        seen[n["name"]] = f.get("product") or "unknown"
    if missing:
        return FAIL, "no facts.json on: " + ", ".join(missing)
    detail = ", ".join("%s=%s" % kv for kv in sorted(seen.items()))
    if len(set(seen.values())) > 1:
        return WARN, "mixed hardware (invalid for a comparison series): " + detail
    return PASS, detail


# --------------------------------------------------------------------------
# S04-S07  the wires
# --------------------------------------------------------------------------

@check("S04", "experimental interfaces present with the planned addresses")
def s04(ctx):
    bad = []
    for n in ctx.nodes:
        want = n.get("ifaces") or {}
        if not want:
            continue
        f = ctx.facts(n)
        if not f:
            bad.append("%s: no facts" % n["name"])
            continue
        have = set(f.get("interfaces", {}).values())
        for lan, addr in want.items():
            if addr not in have:
                bad.append("%s missing %s addr %s (has %s)"
                           % (n["name"], lan, addr, sorted(have)))
    if bad:
        return FAIL, "; ".join(bad)
    return PASS, "all planned addresses present"


@check("S05", "client and backend LANs carry the paths they should")
def s05(ctx):
    fes, dbs = ctx.by_role("fe"), ctx.by_role("db")
    if not (fes and dbs):
        return SKIP, "needs fe and db hosts"
    bad = []
    for drv in ctx.drivers():
        for fe in fes:
            ip = fe["ifaces"].get("client")
            rc, _, _ = ctx.ssh(drv, "ping -c1 -W2 %s >/dev/null" % ip)
            if rc != 0:
                bad.append("%s -/-> %s on client" % (drv["name"], fe["name"]))
    for fe in fes:
        for db in dbs:
            ip = db["ifaces"].get("backend")
            rc, _, _ = ctx.ssh(fe, "ping -c1 -W2 %s >/dev/null" % ip)
            if rc != 0:
                bad.append("%s -/-> %s on backend" % (fe["name"], db["name"]))
    if bad:
        return FAIL, "; ".join(bad)
    return PASS, "driver->fe on client, fe->db on backend"


@check("S06", "drivers cannot bypass the frontends onto the backend LAN")
def s06(ctx):
    dbs = ctx.by_role("db")
    if not dbs:
        return SKIP, "no db hosts"
    leaks = []
    for drv in ctx.drivers() + ctx.by_role("ctl"):
        for db in dbs:
            ip = db["ifaces"].get("backend")
            rc, _, _ = ctx.ssh(drv, "ping -c1 -W2 %s >/dev/null" % ip)
            if rc == 0:
                leaks.append("%s -> %s (%s)" % (drv["name"], db["name"], ip))
    if leaks:
        return FAIL, ("backend reachable outside the fe tier, so admission "
                      "can be bypassed: " + "; ".join(sorted(set(leaks))))
    return PASS, "backend LAN unreachable from ctl and drivers"


@check("S07", "benchmark traffic lands on the client NIC, not the control NIC")
def s07(ctx):
    if not (ctx.frontends and ctx.drivers()):
        return SKIP, "needs a driver and an fe instance"
    drv, f = ctx.drivers()[0], ctx.frontends[0]
    fe_host = ctx.host(f["host"])

    probe = (
        "CI=$(ip -o -4 addr show | awk -v ip=%s '$4 ~ \"^\"ip\"/\" {print $2}'); "
        "CT=$(ip route show default | awk '{print $5; exit}'); "
        "echo $CI $(cat /sys/class/net/$CI/statistics/rx_bytes) "
        "$CT $(cat /sys/class/net/$CT/statistics/rx_bytes)" % f["ip"])

    rc, before, err = ctx.ssh(fe_host, probe)
    if rc != 0 or len(before.split()) != 4:
        return FAIL, "could not read interface counters: %s" % (err or before)
    ci, c0, ct, t0 = before.split()

    ctx.ssh(drv, "python3 /local/repository/services/stub_lg.py "
                 "--target http://%s:%d --rate 200 --duration 3 "
                 "--out /local/testbed/telemetry/s07.json >/dev/null 2>&1"
                 % (f["ip"], f["port"]), timeout=90)

    rc, after, err = ctx.ssh(fe_host, probe)
    if rc != 0 or len(after.split()) != 4:
        return FAIL, "could not re-read interface counters: %s" % (err or after)
    _, c1, _, t1 = after.split()

    d_client, d_ctrl = int(c1) - int(c0), int(t1) - int(t0)
    detail = "%s +%dB vs control %s +%dB" % (ci, d_client, ct, d_ctrl)
    if d_client < 10000:
        return FAIL, ("client NIC saw almost no traffic (%s) -- requests are "
                      "taking another path" % detail)
    if d_client <= d_ctrl:
        # The control NIC also carries k3s chatter; it must still lose.
        return FAIL, "traffic is not preferring the client NIC: " + detail
    return PASS, detail


# --------------------------------------------------------------------------
# S08-S09  host preconditions
# --------------------------------------------------------------------------

@check("S08", "clocks synchronised within tolerance")
def s08(ctx):
    tol = ctx.args.clock_tolerance
    bad, ok = [], []
    for n in ctx.nodes:
        rc, out, _ = ctx.ssh(n, "chronyc tracking 2>/dev/null")
        if rc != 0 or not out:
            bad.append("%s: chrony not responding" % n["name"])
            continue
        offset, leap = None, None
        for line in out.splitlines():
            if line.startswith("System time"):
                for tok in line.split():
                    try:
                        offset = float(tok)
                        break
                    except ValueError:
                        continue
            elif line.startswith("Leap status"):
                leap = line.split(":", 1)[1].strip()
        if offset is None:
            bad.append("%s: could not parse offset" % n["name"])
        elif abs(offset) > tol:
            bad.append("%s: offset %.6fs > %.6fs" % (n["name"], offset, tol))
        elif leap and leap != "Normal":
            bad.append("%s: leap status %s" % (n["name"], leap))
        else:
            ok.append("%s=%.6fs" % (n["name"], offset))
    if bad:
        return FAIL, "; ".join(bad)
    return PASS, "within %.3fs: %s" % (tol, ", ".join(ok))


@check("S09", "storage present and telemetry is not on the data device")
def s09(ctx):
    dbs = ctx.by_role("db")
    if not dbs:
        return SKIP, "no storage hosts"
    issues, notes = [], []
    for db in dbs:
        f = ctx.facts(db)
        if not f:
            issues.append("%s: no facts" % db["name"])
            continue
        data, telem = f["data_dir"], f["telemetry_dir"]
        rc, out, _ = ctx.ssh(db, "df -B1 --output=source,avail %s | tail -1; "
                                 "df --output=source %s | tail -1"
                                 % (data, telem))
        parts = out.split()
        if rc != 0 or len(parts) < 3:
            issues.append("%s: df failed on %s" % (db["name"], data))
            continue
        data_dev, avail, telem_dev = parts[0], int(parts[1]), parts[2]
        gb = avail / 1e9
        if f["data_backing"] != "blockstore":
            notes.append("%s: data on rootfs (set data_size before measuring)"
                         % db["name"])
        if gb < ctx.args.min_data_gb:
            issues.append("%s: only %.1fGB free at %s" % (db["name"], gb, data))
        if data_dev == telem_dev:
            notes.append("%s: telemetry shares %s with the data directory"
                         % (db["name"], data_dev))
        else:
            notes.append("%s: %.1fGB free, telemetry on %s"
                         % (db["name"], gb, telem_dev))
    if issues:
        return FAIL, "; ".join(issues + notes)
    if any("shares" in x or "rootfs" in x for x in notes):
        return WARN, "; ".join(notes)
    return PASS, "; ".join(notes)


# --------------------------------------------------------------------------
# S10-S13  cluster, placement, request path, accounting
# --------------------------------------------------------------------------

@check("S10", "k3s nodes Ready and every pod Running on its intended host")
def s10(ctx):
    ctls = ctx.by_role("ctl")
    if not ctls:
        return SKIP, "no ctl host"
    ctl = ctls[0]
    expected = {f["id"]: f["host"] for f in ctx.frontends}
    expected.update({d["id"]: d["host"] for d in ctx.destinations})
    want_nodes = len(ctx.nodes)

    deadline = time.time() + ctx.args.k8s_wait
    last = "no status yet"
    while time.time() < deadline:
        rc, out, err = ctx.ssh(ctl, "%s kubectl get nodes --no-headers" % K3S)
        if rc != 0:
            last = "kubectl unavailable: %s" % (err or out)
            time.sleep(10)
            continue
        ready = [ln.split()[0] for ln in out.splitlines()
                 if len(ln.split()) > 1 and ln.split()[1] == "Ready"]
        rc, out, err = ctx.ssh(
            ctl, "%s kubectl get pods -n testbed -o json" % K3S)
        if rc != 0:
            last = "%d/%d nodes Ready; pods unreadable" % (len(ready), want_nodes)
            time.sleep(10)
            continue
        pods = {p["metadata"]["name"]:
                (p["status"].get("phase"), p["spec"].get("nodeName"))
                for p in json.loads(out).get("items", [])}
        misplaced = [
            "%s on %s (wanted %s)" % (name, pods[name][1], host)
            for name, host in expected.items()
            if name in pods and pods[name][1] not in (None, host)]
        if misplaced:
            # Placement is pinned by hostname; a misplaced pod means the
            # manifest and topology disagree. Waiting will not fix it.
            return FAIL, "; ".join(misplaced)
        missing = [n for n in expected if n not in pods]
        not_running = ["%s=%s" % (n, pods[n][0]) for n in expected
                       if n in pods and pods[n][0] != "Running"]
        if len(ready) >= want_nodes and not missing and not_running == []:
            placed = ", ".join("%s@%s" % (n, pods[n][1]) for n in sorted(expected))
            return PASS, "%d nodes Ready; %s" % (len(ready), placed)
        last = ("%d/%d nodes Ready; missing=%s; not-running=%s"
                % (len(ready), want_nodes, missing or "-", not_running or "-"))
        time.sleep(10)
    return FAIL, "timed out after %ds: %s" % (ctx.args.k8s_wait, last)


@check("S11", "pod services healthy and destinations resolved")
def s11(ctx):
    bad, ok = [], []
    for d in ctx.destinations:
        rc, out, _ = ctx.ssh(ctx.host(d["host"]),
                             "curl -sf --max-time 5 http://127.0.0.1:%d/health"
                             % d["port"])
        (bad if rc != 0 else ok).append(
            d["id"] if rc == 0 else "%s db pod not answering" % d["id"])
    deadline = time.time() + 45      # discovery probes every 10s
    pending = list(ctx.frontends)
    while pending and time.time() < deadline:
        still = []
        for f in pending:
            rc, out, _ = ctx.ssh(ctx.host(f["host"]),
                                 "curl -sf --max-time 5 http://127.0.0.1:%d/health"
                                 % f["port"])
            try:
                h = json.loads(out) if rc == 0 else {}
            except ValueError:
                h = {}
            if h.get("destinations"):
                ok.append("%s(%d dest)" % (f["id"], h["destinations"]))
            else:
                still.append(f)
        pending = still
        if pending:
            time.sleep(5)
    for f in pending:
        bad.append("%s resolved 0 destinations" % f["id"])
    if bad:
        return FAIL, "; ".join(bad)
    return PASS, ", ".join(ok)


@check("S12", "end-to-end path through every FE instance with attribution")
def s12(ctx):
    if not (ctx.frontends and ctx.drivers()):
        return SKIP, "needs a driver and fe instances"
    drv = ctx.drivers()[0]
    results = []
    for f in ctx.frontends:
        rc, out, err = ctx.ssh(
            drv, "curl -sf --max-time 10 'http://%s:%d/kv?key=smoke-%s'"
                 % (f["ip"], f["port"], f["id"]))
        if rc != 0:
            return FAIL, "no response through %s: %s" % (f["id"], err or "rc")
        try:
            body = json.loads(out)
        except ValueError:
            return FAIL, "%s: unparseable response: %s" % (f["id"], out[:120])
        if body.get("frontend") != f["id"]:
            return FAIL, ("%s: attribution says %r -- port/instance mapping "
                          "is wrong" % (f["id"], body.get("frontend")))
        if "destination_id" not in body:
            return FAIL, "%s: response missing destination_id" % f["id"]
        results.append("%s->%s" % (f["id"], body["destination_id"]))
    return PASS, ", ".join(results)


@check("S13", "offered = accepted + rejected at every FE instance")
def s13(ctx):
    """The core accounting invariant, per instance and in aggregate. With several FE
    instances this is where multi-upstream accounting first becomes real."""
    if not (ctx.frontends and ctx.drivers()):
        return SKIP, "needs a driver and fe instances"
    drivers = ctx.drivers()

    before = {}
    for f in ctx.frontends:
        m = ctx.fe_metrics(f)
        before[f["id"]] = (m or {}).get("totals", {})

    targets = " ".join("--target http://%s:%d" % (f["ip"], f["port"])
                       for f in ctx.frontends)
    start_at = time.time() + 5 + 2 * len(drivers)
    cmd = ("python3 /local/repository/services/stub_lg.py %s --rate 100 "
           "--duration 5 --start-at %f --out /local/testbed/telemetry/s13.json"
           % (targets, start_at))
    procs = ctx.ssh_parallel([(d, cmd) for d in drivers], timeout=180)

    issued = 0
    for rc, out, err in procs:
        if rc != 0:
            return FAIL, "load generator failed: %s" % (err or out)[:200]
        try:
            issued += json.loads(out)["issued"]
        except ValueError:
            return FAIL, "load generator output unparseable"

    violations, per_instance = [], []
    counted = 0
    for f in ctx.frontends:
        m = ctx.fe_metrics(f)
        if m is None:
            return FAIL, "%s metrics unreadable after load" % f["id"]
        inst_off = 0
        for dest, t in m["totals"].items():
            b = before[f["id"]].get(dest, {})
            d_off = t["offered"] - b.get("offered", 0)
            d_acc = t["accepted"] - b.get("accepted", 0)
            d_rej = t["rejected"] - b.get("rejected", 0)
            counted += d_off
            inst_off += d_off
            if not t["identity_holds"]:
                violations.append("%s/%s cumulative: %d != %d + %d"
                                  % (f["id"], dest, t["offered"],
                                     t["accepted"], t["rejected"]))
            if d_off != d_acc + d_rej:
                violations.append("%s/%s delta: %d != %d + %d"
                                  % (f["id"], dest, d_off, d_acc, d_rej))
        per_instance.append("%s+%d" % (f["id"], inst_off))
    if violations:
        return FAIL, "; ".join(violations)
    if counted == 0:
        return FAIL, "no offers recorded despite %d issued" % issued
    lost = issued - counted
    detail = "%d issued, %d offered (%s)" % (issued, counted,
                                             ", ".join(per_instance))
    if abs(lost) > max(5, 0.02 * issued):
        return WARN, detail + " -- %d unaccounted, check send failures" % lost
    return PASS, detail


# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--topology", default="topology.json")
    p.add_argument("--only", default="", help="comma-separated check ids")
    p.add_argument("--skip", default="", help="comma-separated check ids")
    p.add_argument("--clock-tolerance", type=float, default=0.005)
    p.add_argument("--min-data-gb", type=float, default=5.0)
    p.add_argument("--k8s-wait", type=int, default=240,
                   help="seconds to wait for nodes Ready and pods Running")
    p.add_argument("--out", default="", help="write results JSON here")
    args = p.parse_args()

    with open(args.topology) as fh:
        topo = json.load(fh)
    ctx = Ctx(topo, args)

    only = {c.strip() for c in args.only.split(",") if c.strip()}
    skip = {c.strip() for c in args.skip.split(",") if c.strip()}

    print("hosts: %s" % ", ".join(
        "%s(%s)" % (n["name"], n["role"]) for n in ctx.nodes))
    print("fe instances: %s" % ", ".join(
        "%s:%d" % (f["id"], f["port"]) for f in ctx.frontends))
    print()

    results, worst = [], PASS
    for cid, title, fn in REGISTRY:
        if (only and cid not in only) or cid in skip:
            continue
        t0 = time.time()
        try:
            status, detail = fn(ctx)
        except Exception as exc:                       # noqa: BLE001
            status, detail = FAIL, "check raised: %r" % exc
        dt = time.time() - t0
        results.append({"id": cid, "title": title, "status": status,
                        "detail": detail, "seconds": round(dt, 1)})
        print("%-4s %-6s %-58s %5.1fs" % (cid, status, title, dt))
        if detail:
            print("        %s" % detail)
        if status == FAIL:
            worst = FAIL
        elif status == WARN and worst != FAIL:
            worst = WARN

    print()
    tally = {}
    for r in results:
        tally[r["status"]] = tally.get(r["status"], 0) + 1
    print("summary: " + ", ".join("%s=%d" % kv for kv in sorted(tally.items())))

    if args.out:
        with open(args.out, "w") as fh:
            json.dump({"generated_at": time.time(), "topology": topo,
                       "results": results, "overall": worst}, fh, indent=2)
        print("wrote %s" % args.out)

    sys.exit(1 if worst == FAIL else 0)


if __name__ == "__main__":
    main()
