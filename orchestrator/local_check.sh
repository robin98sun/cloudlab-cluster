#!/usr/bin/env bash
# Run the multi-instance request path on one machine: 3 FE instances on
# distinct ports (as they will sit on one fe host) + 1 DB. Verifies instance
# attribution and the offered = accepted + rejected identity per instance
# and in aggregate.
set -euo pipefail

WORK="$(mktemp -d)"
trap 'kill $(jobs -p) 2>/dev/null || true; rm -rf "$WORK"' EXIT
HERE="$(cd "$(dirname "$0")/.." && pwd)"

cat > "$WORK/destinations.json" <<JSON
{"destinations": [{"destination_id": "db1", "endpoint": "http://127.0.0.1:19091"}]}
JSON

python3 "$HERE/services/stub_db.py" --port 19091 --data-dir "$WORK/data" \
    --base-ms 1 --capacity 16 >"$WORK/db.log" 2>&1 &
for i in 0 1 2; do
    port=$((18081 + i))
    id="fe1-$(printf \\$(printf '%03o' $((97 + i))))"
    python3 "$HERE/services/stub_fe.py" --port "$port" --instance-id "$id" \
        --db-port 19091 --destinations-file "$WORK/destinations.json" \
        --telemetry-dir "$WORK/telemetry" >"$WORK/fe-$port.log" 2>&1 &
done

for i in $(seq 1 40); do
    up=1
    for port in 18081 18082 18083; do
        curl -sf --max-time 1 "http://127.0.0.1:$port/health" >/dev/null 2>&1 || up=0
    done
    [ "$up" = 1 ] && break
    sleep 0.25
    [ "$i" = 40 ] && { echo "FAIL: services never came up"; cat "$WORK"/*.log; exit 1; }
done

echo "--- instance attribution"
for port in 18081 18082 18083; do
    curl -s "http://127.0.0.1:$port/kv?key=smoke" \
        | python3 -c "import json,sys; d=json.load(sys.stdin); \
print('  :%s -> frontend=%s dest=%s' % ($port, d['frontend'], d['destination_id']))"
done

echo "--- load across all three instances"
python3 "$HERE/services/stub_lg.py" \
    --target http://127.0.0.1:18081 --target http://127.0.0.1:18082 \
    --target http://127.0.0.1:18083 \
    --rate 150 --duration 4 --out "$WORK/lg.json" >/dev/null
python3 -c "
import json; d = json.load(open('$WORK/lg.json'))
print('  requested %.0f qps, achieved %.1f qps, late_sends %d'
      % (d['requested_qps'], d['achieved_qps'], d['late_sends']))
print('  status', d['status'])
print('  per-target', {k.rsplit(':',1)[1]: v for k, v in d['per_target'].items()})
"

echo "--- identity per instance and aggregate"
python3 - "$WORK/lg.json" <<'PY'
import json, sys, urllib.request
issued = json.load(open(sys.argv[1]))["issued"]
ok, total = True, 0
for port in (18081, 18082, 18083):
    m = json.load(urllib.request.urlopen("http://127.0.0.1:%d/metrics" % port))
    inst = m["instance"]
    for dest, t in m["totals"].items():
        holds = t["offered"] == t["accepted"] + t["rejected"]
        ok &= holds
        total += t["offered"]
        print("  %s/%s: offered=%d accepted=%d rejected=%d completed=%d identity=%s"
              % (inst, dest, t["offered"], t["accepted"], t["rejected"],
                 t["completed"], "OK" if holds else "VIOLATED"))
# +3 for the three attribution probes above
expect = issued + 3
print("  aggregate: %d offered vs %d issued+probes -> %s"
      % (total, expect, "OK" if total == expect else "MISMATCH"))
ok &= (total == expect)
sys.exit(0 if ok else 1)
PY

echo
echo "local check PASSED"
