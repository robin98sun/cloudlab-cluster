"""Frontend / testbed host stand-in.

Deliberately minimal -- it does exactly this and nothing else: resolve the
fixed destination, ask the admission controller, forward or reject, record.
It never picks a different backend -- routing stays out of admission.
"""

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen
from urllib.error import URLError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from admission_stub import PassThroughAdmission  # noqa: E402

ARGS = None
testbed = None
DESTINATIONS = []
DEST_LOCK = threading.Lock()


def discover_destinations():
    """Config file wins; otherwise probe the backend subnet.

    Auto-discovery keeps smoke runnable with zero orchestration. A measured run
    should pin destinations.json so the topology is recorded, not inferred.
    """
    cfg = ARGS.destinations_file
    if cfg and os.path.exists(cfg):
        with open(cfg) as fh:
            return json.load(fh)["destinations"]
    found = []
    for k in range(1, 10):
        host = "10.10.2.%d" % (30 + k)
        try:
            with urlopen("http://%s:%d/health" % (host, ARGS.db_port),
                         timeout=1.0) as r:
                if r.status == 200:
                    found.append({"destination_id": "db%d" % k,
                                  "endpoint": "http://%s:%d" % (host, ARGS.db_port)})
        except (URLError, OSError):
            continue
    return found


def resolve(key):
    """Fixed affinity: the key alone determines the destination."""
    with DEST_LOCK:
        dests = list(DESTINATIONS)
    if not dests:
        return None
    h = hashlib.sha256(key.encode()).digest()
    return dests[int.from_bytes(h[:4], "big") % len(dests)]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)

        if u.path == "/health":
            with DEST_LOCK:
                n = len(DESTINATIONS)
            return self._reply(200, {"ok": n > 0, "role": "fe",
                                     "instance": ARGS.instance_id,
                                     "node": os.uname().nodename,
                                     "destinations": n})
        if u.path == "/destinations":
            with DEST_LOCK:
                return self._reply(200, {"destinations": list(DESTINATIONS)})
        if u.path == "/refresh":
            fresh = discover_destinations()
            with DEST_LOCK:
                DESTINATIONS[:] = fresh
            return self._reply(200, {"destinations": fresh})
        if u.path == "/metrics":
            return self._reply(200, {"instance": ARGS.instance_id,
                                     "node": os.uname().nodename,
                                     "mode": testbed.mode,
                                     "totals": testbed.totals()})
        if u.path == "/buckets":
            return self._reply(200, {"instance": ARGS.instance_id,
                                     "buckets": testbed.snapshot()})
        if u.path == "/kv":
            return self._kv((parse_qs(u.query).get("key") or ["?"])[0])
        return self._reply(404, {"error": "not found"})

    def _kv(self, key):
        dest = resolve(key)
        if dest is None:
            return self._reply(503, {"error": "no destination resolved",
                                     "key": key})
        dest_id = dest["destination_id"]

        admitted, reason = testbed.admit(dest_id)
        if not admitted:
            return self._reply(429, {"error": "rejected", "reason": reason,
                                     "destination_id": dest_id, "key": key})

        t0 = time.monotonic()
        try:
            with urlopen("%s/kv?key=%s" % (dest["endpoint"], key),
                         timeout=ARGS.timeout) as r:
                payload = json.loads(r.read().decode())
            testbed.complete(dest_id, time.monotonic() - t0, ok=True)
            payload["destination_id"] = dest_id
            payload["frontend"] = ARGS.instance_id
            return self._reply(200, payload)
        except Exception as exc:                      # noqa: BLE001
            timed_out = isinstance(exc, (TimeoutError, URLError))
            testbed.complete(dest_id, time.monotonic() - t0, ok=False,
                         timed_out=timed_out)
            return self._reply(502, {"error": "downstream failure",
                                     "detail": str(exc),
                                     "destination_id": dest_id})


def main():
    global ARGS, testbed
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8081)
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--db-port", type=int, default=9091)
    p.add_argument("--timeout", type=float, default=5.0)
    # Three FE pods share one hostname under hostNetwork; the instance
    # id is what tells their observations and budgets apart.
    p.add_argument("--instance-id", default=None)
    p.add_argument("--destinations-file", default="/local/testbed/destinations.json")
    p.add_argument("--telemetry-dir", default="/local/testbed/telemetry")
    ARGS = p.parse_args()
    if ARGS.instance_id is None:
        ARGS.instance_id = os.uname().nodename

    testbed = PassThroughAdmission()
    os.makedirs(ARGS.telemetry_dir, exist_ok=True)

    def rediscover():
        while True:
            fresh = discover_destinations()
            with DEST_LOCK:
                if fresh != DESTINATIONS:
                    DESTINATIONS[:] = fresh
                    print("destinations: %s" % [d["destination_id"] for d in fresh],
                          flush=True)
            time.sleep(10)

    threading.Thread(target=rediscover, daemon=True).start()
    srv = ThreadingHTTPServer((ARGS.bind, ARGS.port), Handler)
    srv.daemon_threads = True
    print("stub_fe %s listening on %s:%d" % (ARGS.instance_id, ARGS.bind, ARGS.port), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
