"""Load generator stand-in.

Open-loop pacing: requests are scheduled against a fixed timeline, so offered
load does not collapse when the system slows down. Closed-loop generators hide
overload, which is the one thing this testbed exists to observe.

Records requested vs actually issued QPS separately: offered load must never
be inferred from configuration, only from what was actually sent.
"""

import argparse
import json
import os
import random
import socket
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.request import urlopen
from urllib.error import HTTPError, URLError

RESULTS = {"latencies": [], "status": Counter(), "per_target": Counter(),
           "issued": 0, "scheduled": 0, "send_failures": 0, "late_sends": 0}
LOCK = threading.Lock()


def one_request(url, key, timeout, scheduled_at):
    lateness = time.monotonic() - scheduled_at
    t0 = time.monotonic()
    status = "unknown"
    try:
        with urlopen("%s/kv?key=%s" % (url, key), timeout=timeout) as r:
            r.read()
            status = "success" if r.status == 200 else "http_%d" % r.status
    except HTTPError as e:
        status = "rejected" if e.code == 429 else "http_%d" % e.code
    except (URLError, socket.timeout, TimeoutError):
        status = "timeout"
    except OSError:
        status = "send_failure"
    dt = time.monotonic() - t0
    with LOCK:
        RESULTS["latencies"].append(dt)
        RESULTS["status"][status] += 1
        RESULTS["per_target"][url] += 1
        RESULTS["issued"] += 1
        if status == "send_failure":
            RESULTS["send_failures"] += 1
        if lateness > 0.05:
            RESULTS["late_sends"] += 1


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--target", action="append", required=True,
                   help="frontend base URL; repeat for several")
    p.add_argument("--rate", type=float, default=20.0, help="requests/second")
    p.add_argument("--duration", type=float, default=10.0, help="seconds")
    p.add_argument("--keyspace", type=int, default=1000)
    p.add_argument("--hot-key", default=None,
                   help="send every request to one key (fixed-affinity hotspot)")
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--workers", type=int, default=64)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--start-at", type=float, default=0.0,
                   help="epoch seconds; synchronised start barrier")
    p.add_argument("--out", default="/local/dcb/telemetry/lg-results.json")
    args = p.parse_args()

    rng = random.Random(args.seed)
    targets = args.target

    if args.start_at:
        delay = args.start_at - time.time()
        if delay > 0:
            time.sleep(delay)

    total = int(args.rate * args.duration)
    interval = 1.0 / args.rate if args.rate > 0 else 0.0
    t_start = time.monotonic()
    wall_start = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i in range(total):
            due = t_start + i * interval
            now = time.monotonic()
            if due > now:
                time.sleep(due - now)
            key = args.hot_key or "k%d" % rng.randrange(args.keyspace)
            url = targets[i % len(targets)]
            with LOCK:
                RESULTS["scheduled"] += 1
            pool.submit(one_request, url, key, args.timeout, due)

    elapsed = time.monotonic() - t_start
    lat = sorted(RESULTS["latencies"])

    def pct(q):
        return lat[min(len(lat) - 1, int(q * len(lat)))] if lat else 0.0

    summary = {
        "node": os.uname().nodename,
        "wall_start": wall_start,
        "wall_end": time.time(),
        "elapsed_s": elapsed,
        "requested_qps": args.rate,
        "achieved_qps": RESULTS["issued"] / elapsed if elapsed else 0.0,
        "scheduled": RESULTS["scheduled"],
        "issued": RESULTS["issued"],
        "send_failures": RESULTS["send_failures"],
        "late_sends": RESULTS["late_sends"],
        "status": dict(RESULTS["status"]),
        "per_target": dict(RESULTS["per_target"]),
        "latency_s": {"p50": pct(0.50), "p95": pct(0.95),
                      "p99": pct(0.99), "p999": pct(0.999),
                      "max": lat[-1] if lat else 0.0},
        "targets": targets,
        "hot_key": args.hot_key,
        "seed": args.seed,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(summary, fh, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
