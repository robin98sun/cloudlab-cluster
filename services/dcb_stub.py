"""Admission-control seam plus one-second bucket accounting.

This is deliberately NOT the DCB controller. It is the interface the real
controller will implement, wired to the bucket bookkeeping so the core accounting
invariant -- offered = accepted + rejected, per destination, per bucket -- is
enforced from the first smoke run rather than retrofitted.

PassThroughAdmission admits everything. Replacing it with the real three-state
controller should require no change to stub_fe.py.
"""

import threading
import time


class Bucket:
    """One-second observation bucket for one destination."""

    __slots__ = ("interval_id", "start_timestamp", "destination_id",
                 "operating_mode", "offered_count", "accepted_count",
                 "rejected_count", "completion_count", "error_count",
                 "timeout_count", "latency_sum", "latency_max", "latencies",
                 "inflight_sum", "inflight_samples", "max_inflight",
                 "admission_ceiling")

    def __init__(self, interval_id, destination_id, mode):
        self.interval_id = interval_id
        self.start_timestamp = float(interval_id)
        self.destination_id = destination_id
        self.operating_mode = mode
        self.offered_count = 0
        self.accepted_count = 0
        self.rejected_count = 0
        self.completion_count = 0
        self.error_count = 0
        self.timeout_count = 0
        self.latency_sum = 0.0
        self.latency_max = 0.0
        self.latencies = []
        self.inflight_sum = 0
        self.inflight_samples = 0
        self.max_inflight = 0
        self.admission_ceiling = None

    def as_dict(self):
        lat = sorted(self.latencies)
        def pct(p):
            if not lat:
                return 0.0
            return lat[min(len(lat) - 1, int(p * len(lat)))]
        n = self.completion_count or 1
        return {
            "interval_id": self.interval_id,
            "start_timestamp": self.start_timestamp,
            "duration": 1.0,
            "destination_id": self.destination_id,
            "operating_mode": self.operating_mode,
            "offered_count": self.offered_count,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "completion_count": self.completion_count,
            "avg_latency": self.latency_sum / n,
            "p95_latency": pct(0.95),
            "avg_inflight": (self.inflight_sum / self.inflight_samples
                             if self.inflight_samples else 0.0),
            "max_inflight": self.max_inflight,
            "error_count": self.error_count,
            "timeout_count": self.timeout_count,
            "admission_ceiling": self.admission_ceiling,
        }


class PassThroughAdmission:
    """Admit-all placeholder holding the seam for the real DCB controller."""

    mode = "PassThrough"

    def __init__(self, history_seconds=300):
        self.history_seconds = history_seconds
        self._lock = threading.Lock()
        self._buckets = {}          # destination_id -> {interval_id: Bucket}
        self._inflight = {}         # destination_id -> int

    # -- controller interface ------------------------------------------------

    def admit(self, destination_id, now=None):
        """Return (admitted: bool, reason: str). Counts the offer either way."""
        b = self._bucket(destination_id, now)
        with self._lock:
            b.offered_count += 1
            b.accepted_count += 1
            b.admission_ceiling = None
            self._inflight[destination_id] = self._inflight.get(destination_id, 0) + 1
            inf = self._inflight[destination_id]
            b.inflight_sum += inf
            b.inflight_samples += 1
            b.max_inflight = max(b.max_inflight, inf)
        return True, "passthrough"

    def reject(self, destination_id, reason, now=None):
        """Record a rejection. Kept separate so the identity cannot drift."""
        b = self._bucket(destination_id, now)
        with self._lock:
            b.offered_count += 1
            b.rejected_count += 1
        return False, reason

    def complete(self, destination_id, latency_s, ok=True, timed_out=False,
                 now=None):
        b = self._bucket(destination_id, now)
        with self._lock:
            self._inflight[destination_id] = max(
                0, self._inflight.get(destination_id, 1) - 1)
            b.completion_count += 1
            b.latency_sum += latency_s
            b.latency_max = max(b.latency_max, latency_s)
            b.latencies.append(latency_s)
            if not ok:
                b.error_count += 1
            if timed_out:
                b.timeout_count += 1

    # -- introspection -------------------------------------------------------

    def inflight(self, destination_id):
        return self._inflight.get(destination_id, 0)

    def snapshot(self):
        with self._lock:
            return {
                dest: [b.as_dict() for _, b in sorted(buckets.items())]
                for dest, buckets in self._buckets.items()
            }

    def totals(self):
        """Aggregate counters, used by the offered = accepted + rejected check."""
        out = {}
        with self._lock:
            for dest, buckets in self._buckets.items():
                t = {"offered": 0, "accepted": 0, "rejected": 0,
                     "completed": 0, "errors": 0, "timeouts": 0}
                for b in buckets.values():
                    t["offered"] += b.offered_count
                    t["accepted"] += b.accepted_count
                    t["rejected"] += b.rejected_count
                    t["completed"] += b.completion_count
                    t["errors"] += b.error_count
                    t["timeouts"] += b.timeout_count
                t["inflight"] = self._inflight.get(dest, 0)
                t["identity_holds"] = (t["offered"] == t["accepted"] + t["rejected"])
                out[dest] = t
        return out

    # -- internals -----------------------------------------------------------

    def _bucket(self, destination_id, now=None):
        now = time.time() if now is None else now
        interval_id = int(now)
        with self._lock:
            per_dest = self._buckets.setdefault(destination_id, {})
            b = per_dest.get(interval_id)
            if b is None:
                b = Bucket(interval_id, destination_id, self.mode)
                per_dest[interval_id] = b
                cutoff = interval_id - self.history_seconds
                for stale in [k for k in per_dest if k < cutoff]:
                    del per_dest[stale]
        return b
