"""Storage-tier stand-in.

Plumbing only. It exists so the request path is real before MyRocks/Raft or
TiKV is wired in. Its latency model is a crude load-dependent curve -- enough
to produce a knee-shaped signal for exercising the pipeline, and NOT a
substitute for measuring a real storage engine. Do not report numbers from it.
"""

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

STATE = {"inflight": 0, "served": 0, "errors": 0, "started": time.time()}
LOCK = threading.Lock()
ARGS = None


def service_time():
    """Base service time inflated by concurrency, giving a soft knee."""
    with LOCK:
        inflight = STATE["inflight"]
    base = ARGS.base_ms / 1000.0
    capacity = max(1, ARGS.capacity)
    return base * (1.0 + (max(0, inflight - 1) / float(capacity)) ** 2)


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
            return self._reply(200, {"ok": True, "role": "db",
                                     "node": os.uname().nodename})
        if u.path == "/metrics":
            with LOCK:
                snap = dict(STATE)
            snap["uptime_s"] = time.time() - snap.pop("started")
            return self._reply(200, snap)
        if u.path == "/kv":
            key = (parse_qs(u.query).get("key") or ["?"])[0]
            with LOCK:
                STATE["inflight"] += 1
            try:
                time.sleep(service_time())
                with LOCK:
                    STATE["served"] += 1
                return self._reply(200, {"key": key, "value": "v:" + key,
                                         "node": os.uname().nodename})
            finally:
                with LOCK:
                    STATE["inflight"] -= 1
        return self._reply(404, {"error": "not found"})


def main():
    global ARGS
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=9091)
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--base-ms", type=float, default=2.0)
    p.add_argument("--capacity", type=int, default=32,
                   help="concurrency beyond which latency degrades")
    p.add_argument("--data-dir", default="/mnt/data")
    ARGS = p.parse_args()
    os.makedirs(ARGS.data_dir, exist_ok=True)
    srv = ThreadingHTTPServer((ARGS.bind, ARGS.port), Handler)
    srv.daemon_threads = True
    print("stub_db listening on %s:%d data_dir=%s" %
          (ARGS.bind, ARGS.port, ARGS.data_dir), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
