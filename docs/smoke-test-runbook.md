# Smoke Test Runbook

## 1. What this is

A CloudLab testbed for distributed admission-control experiments, at its
smallest useful scale: **3 c6620 machines** running a k3s cluster with the
services as pinned, hostNetwork pods.

```
ctl1   k3s server + monitoring + load generation      client LAN
fe1    3 frontend pods (fe1-a/b/c, ports 8081-8083)   client + backend LANs
db1    1 storage pod (db1, port 9091)                 backend LAN
```

The full 5-machine topology (3 db hosts) uses the same profile with
`preset=full`. Placement rules are fixed in `cloudlab/gen_manifests.py`:
frontend and storage pods never share a host, storage replicas never share a
host, frontend pods colocate. Every pod is pinned by hostname — placement is
a recorded decision, not a scheduler outcome.

Because frontend pods colocate, **even the 3-machine tier has three
independent admission points**, so multi-instance accounting (S13) is real
from day one.

What it is not: a measurement. `stub_fe` runs `PassThroughAdmission` (admit
everything); result bundles are stamped `measurement_valid: false`.

## 2. Local check — no CloudLab required

```bash
make local
```

Runs 3 frontend instances + 1 storage stub on this machine; verifies instance
attribution and the accounting identity `offered = accepted + rejected`, per
instance and in aggregate.

## 3. First boot (base image — one-time, ~25 min)

Create a repo-backed profile in the CloudLab portal pointing at this
repository (`profile.py` at the root — CloudLab requires the top-level
location). Instantiate with the default `preset=smoke` on **CloudLab Utah**
(c6620 lives there). The first boot downloads packages, the k3s binary, and
the pod base image — the slow path the golden image later removes.

Purposes are presets of the one profile, selected at instantiation:

| preset | machines | db hosts | intent |
|---|---:|---:|---|
| `smoke` | 3 | 1 | plumbing verification, fast iteration |
| `full` | 5 | 3 | baseline experiments |
| `submission` | 5 | 3 | frozen bindings; bind a portal profile to a release **tag** |
| `custom` | — | — | the individual form fields apply |

A preset **overrides** the individual fields it defines; `disk_image` is
never preset-bound and always comes from the form (pin the golden URN as its
default). For dev-vs-official coexistence, create two portal profile objects
over the same file: one tracking `main`, one pinned to a release tag.

Then:

```bash
make topology          # after downloading manifest.xml from the portal
make smoke
```

## 4. Bake the golden image (one-time, ~20 min)

On any node of the running experiment (all roles share one image):

```bash
ssh <node> sudo bash /local/repository/cloudlab/bake.sh
```

This stops k3s, wipes all cluster identity and per-boot state, and keeps the
bake layer (packages, k3s binary, prefetched image tarballs,
`/etc/testbed-image-version`). Then in the portal: node → **Create Disk Image**.
Pin the resulting URN — **with its version** — as `disk_image` in `profile.py`
and commit.

Rebake only when the dependency set changes (bump `IMAGE_LAYER` in
`bootstrap.sh`, which forces the bake layer to re-run on base images and makes
stale golden images self-identify in S02). Code changes never need a rebake —
code arrives via the repo clone at every boot.

## 5. The ~15-minute loop (every redeploy after that)

```bash
# portal: instantiate profile (golden image pinned)     ~10 min
make topology                                         #  ~1 min
make smoke                                            #  ~3 min
make collect                                          #  ~1 min
```

`make smoke-fast` skips the load-generating checks (S07, S12, S13) for quick
re-checks after a reboot.

## 6. What the checks prove

| ID | Proves |
|---|---|
| S01 | every host answers over the control network |
| S02 | bake layer + boot layer both completed (and whether this was a golden-image boot) |
| S03 | hardware recorded and homogeneous |
| S04 | planned addresses are on the interfaces |
| S05 | driver→fe on client LAN, fe→db on backend LAN |
| S06 | ctl/drivers **cannot** reach the backend LAN — admission is unbypassable |
| S07 | benchmark bytes land on the client NIC, not the control NIC |
| S08 | chrony within tolerance (default 5ms) |
| S09 | data mount present with space; telemetry on a different device |
| S10 | k3s nodes Ready; every pod Running **on its pinned host** (waits up to `--k8s-wait`) |
| S11 | pods healthy; every frontend resolved its destinations |
| S12 | end-to-end request through every frontend instance, attributed to the right instance id |
| S13 | `offered = accepted + rejected` per frontend instance and aggregate ≈ issued |

S10 fails immediately (no waiting) on a *misplaced* pod — placement is pinned,
so disagreement between manifest and topology cannot resolve itself.

S06 is a negative test: if a load generator can reach storage directly, the
frontend is not the only admission point and every admission-control result is
meaningless. S07 exists because with several LANs the common failure is not
contention but misrouting — traffic silently taking the control network while
a shaped experimental link sits idle, with nothing erroring.

## 7. Cluster control from any node

k3s puts the admin kubeconfig only on ctl1. After `make topology`:

```bash
make kubeconfig
```

copies it to every node (server address rewritten to ctl1's control IP), so
`kubectl` works on fe/db hosts too. Deliberate testbed trade-off: every node
then holds admin credentials. Rerun after re-forming the cluster — the
credentials change with cluster identity.

## 8. Networks

Client and backend LANs carry only benchmark traffic. Everything else — k3s
API, kubelet, flannel, monitoring, SSH — rides CloudLab's control network.
Measured pods use `hostNetwork`, so the CNI overlay never touches the measured
path. Telemetry is written locally during runs and shipped by `make collect`
afterwards.

## 9. Seams for the system under test

- `services/admission_stub.py` — `PassThroughAdmission` is the seam where a real
  admission controller plugs in; the bucket accounting is already wired.
- `stub_db.py`'s latency curve is a synthetic concurrency-driven inflation,
  not a storage engine.
- Destination discovery auto-probes; measured runs should pin
  `/local/testbed/destinations.json`.
- Pods run without CPU limits. CPU-pinning regimes and the CFS-throttling
  validity gate arrive with the co-tenancy work (a CPU limit's 100ms CFS
  quota periods inject scheduler artifacts into latency measurements).
- Orchestration runs from your workstation; measured runs move it to ctl1.
- Private research code deploys onto nodes via rsync to a separate path,
  keeping this repository self-contained.
