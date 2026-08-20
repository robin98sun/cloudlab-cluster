"""Distributed admission-control testbed -- CloudLab profile (k3s on bare metal).

Project-neutral: paths, namespace, and labels use the generic name
"testbed", so the same infrastructure serves any system under test.

Physical hosts (one hardware type per comparison series, default c6620):

    ctl1    k3s control plane + monitoring + load generation   client LAN
    fe<j>   frontend hosts: fe_instances FE+testbed pods each      client+backend
            (hostNetwork, distinct ports 8081..)                LANs
    db<k>   storage hosts: 1 replica pod each (+ declared      backend LAN
            noisy-neighbor pods later)
    lg<i>   optional dedicated load-generator hosts; 0 until   client LAN
            stub_lg's late_sends gate says otherwise

Purposes are parameter bindings of this one generator, selected by `preset`:

    preset       machines  db hosts  intent
    smoke        3         1         plumbing verification, fast iteration
    full         5         3         replicated/Raft baseline experiments
    submission   5         3         frozen bindings for reported results;
                                     bind a portal profile to a release TAG
                                     of this repo so it can never drift
    custom       --        --        the individual form fields apply

When preset != custom, the preset's bindings OVERRIDE the individual form
fields they name; fields a preset does not name (notably disk_image) still
come from the form. After baking a golden image, pin its versioned URN as
the disk_image default here and commit; before tagging a submission release,
also pin it inside the submission preset.

Placement rules live in cloudlab/gen_manifests.py: FE and DB never share a
host, DB replicas never share a host, FE pods colocate.

Per-role hardware requirements when substituting hw_type:
    fe hosts : >= 2 experimental interfaces (client + backend)
    db hosts : local disk large enough for the data blockstore
    all      : one homogeneous type within any comparison series -- results
               are comparable within a type, never across; S03 warns on
               mixed allocations

Networks: client 10.10.1.0/24, backend 10.10.2.0/24. Monitoring and all k3s
control traffic ride CloudLab's control network; measured pods use
hostNetwork, so nothing latency-sensitive crosses an overlay.

Address plan: ctl1 10.10.1.10; lg<i> 10.10.1.(10+i); fe<j> 10.10.1.(20+j)
and 10.10.2.(20+j); db<k> 10.10.2.(30+k).
"""

import geni.portal as portal
import geni.rspec.pg as pg

DEFAULT_IMAGE = "urn:publicid:IDN+emulab.net+image+emulab-ops//UBUNTU22-64-STD"

PRESETS = {
    "smoke": dict(num_fe_hosts=1, num_db_hosts=1, num_lg_hosts=0,
                  fe_instances=3, data_size="20GB"),
    "full": dict(num_fe_hosts=1, num_db_hosts=3, num_lg_hosts=0,
                 fe_instances=3, data_size="200GB"),
    # Freeze everything that defines the reported configuration. Pin
    # disk_image here too once the submission-era golden image exists.
    "submission": dict(num_fe_hosts=1, num_db_hosts=3, num_lg_hosts=0,
                       fe_instances=3, hw_type="c6620", data_size="200GB",
                       client_bw=0, backend_bw=0),
}

pc = portal.Context()

pc.defineParameter(
    "preset", "Configuration preset", portal.ParameterType.STRING, "smoke",
    legalValues=[("smoke", "smoke: 3 machines, 1 db host"),
                 ("full", "full: 5 machines, 3 db hosts"),
                 ("submission", "submission: frozen full-scale bindings"),
                 ("custom", "custom: use the individual fields below")],
    longDescription="Anything other than 'custom' overrides the individual "
                    "fields it defines (see the profile source for exact "
                    "bindings). Presets are versioned with the repository, so "
                    "every result bundle can name its configuration by commit.")
pc.defineParameter(
    "num_fe_hosts", "Frontend hosts (custom preset)",
    portal.ParameterType.INTEGER, 1,
    longDescription="Each runs fe_instances FE+testbed pods; one host already "
                    "preserves the multi-upstream property.")
pc.defineParameter(
    "num_db_hosts", "Storage hosts (custom preset)",
    portal.ParameterType.INTEGER, 1,
    longDescription="One replica pod per host. 1 for smoke, 3 for Raft. "
                    "2 is refused.")
pc.defineParameter(
    "num_lg_hosts", "Dedicated load-generator hosts (custom preset)",
    portal.ParameterType.INTEGER, 0,
    longDescription="0 runs load generation on ctl1. Add hosts only when "
                    "stub_lg's late_sends gate shows pacing degradation.")
pc.defineParameter(
    "fe_instances", "FE pods per frontend host (custom preset)",
    portal.ParameterType.INTEGER, 3,
    longDescription="Ports 8081, 8082, ... Three is the minimum for the "
                    "distributed property: several independent "
                    "admission points enforcing one shared budget.")
pc.defineParameter(
    "hw_type", "Hardware type", portal.ParameterType.STRING, "c6620",
    longDescription="c6620 (Utah): 28 cores, 128GB, 2 NVMe, 2 experimental "
                    "interfaces. Substitutions must satisfy the per-role "
                    "requirements in the profile source; the smoke suite "
                    "re-validates any change in minutes.")
pc.defineParameter(
    "disk_image", "Disk image URN", portal.ParameterType.STRING, DEFAULT_IMAGE,
    longDescription="Base Ubuntu for the first boot; the pinned golden-image "
                    "URN (with version) afterwards for the ~15-minute "
                    "redeploy. Not overridden by presets.")
pc.defineParameter(
    "data_size", "Data blockstore per storage host",
    portal.ParameterType.STRING, "20GB",
    longDescription="Mounted at /mnt/data. Empty skips it (data lands on the "
                    "root filesystem -- smoke only).")
pc.defineParameter(
    "client_bw", "Client link bandwidth (Kbps, 0 = native)",
    portal.ParameterType.INTEGER, 0)
pc.defineParameter(
    "backend_bw", "Backend link bandwidth (Kbps, 0 = native)",
    portal.ParameterType.INTEGER, 0)

params = pc.bindParameters()

CONFIG_FIELDS = ("num_fe_hosts", "num_db_hosts", "num_lg_hosts",
                 "fe_instances", "hw_type", "disk_image", "data_size",
                 "client_bw", "backend_bw")
cfg = {f: getattr(params, f) for f in CONFIG_FIELDS}
if params.preset != "custom":
    if params.preset not in PRESETS:
        pc.reportError(portal.ParameterError(
            "unknown preset %r" % params.preset, ["preset"]))
    else:
        cfg.update(PRESETS[params.preset])

if cfg["num_fe_hosts"] < 1:
    pc.reportError(portal.ParameterError(
        "At least one frontend host is required.", ["num_fe_hosts"]))
if cfg["num_db_hosts"] < 1:
    pc.reportError(portal.ParameterError(
        "At least one storage host is required.", ["num_db_hosts"]))
if cfg["num_db_hosts"] == 2:
    pc.reportError(portal.ParameterError(
        "Two storage hosts cannot form a useful Raft majority. Use 1 (smoke) "
        "or 3.", ["num_db_hosts"]))
if cfg["fe_instances"] < 1:
    pc.reportError(portal.ParameterError(
        "At least one FE pod per host.", ["fe_instances"]))
pc.verifyParameters()

request = pc.makeRequestRSpec()

client_lan = request.LAN("client")
backend_lan = request.LAN("backend")
if cfg["client_bw"] > 0:
    client_lan.bandwidth = cfg["client_bw"]
if cfg["backend_bw"] > 0:
    backend_lan.bandwidth = cfg["backend_bw"]


def make_node(name, role, extra_args=""):
    node = request.RawPC(name)
    if cfg["hw_type"]:
        node.hardware_type = cfg["hw_type"]
    node.disk_image = cfg["disk_image"]
    node.addService(pg.Execute(
        shell="bash",
        command="bash /local/repository/cloudlab/bootstrap.sh %s%s"
                % (role, extra_args)))
    return node


def attach(node, lan, addr):
    iface = node.addInterface()
    iface.addAddress(pg.IPv4Address(addr, "255.255.255.0"))
    lan.addInterface(iface)


ctl = make_node("ctl1", "ctl",
                " --fe-hosts %d --db-hosts %d --lg-hosts %d --fe-instances %d"
                % (cfg["num_fe_hosts"], cfg["num_db_hosts"],
                   cfg["num_lg_hosts"], cfg["fe_instances"]))
attach(ctl, client_lan, "10.10.1.10")

for i in range(1, cfg["num_lg_hosts"] + 1):
    n = make_node("lg%d" % i, "lg")
    attach(n, client_lan, "10.10.1.%d" % (10 + i))

for j in range(1, cfg["num_fe_hosts"] + 1):
    n = make_node("fe%d" % j, "fe")
    attach(n, client_lan, "10.10.1.%d" % (20 + j))
    attach(n, backend_lan, "10.10.2.%d" % (20 + j))

for k in range(1, cfg["num_db_hosts"] + 1):
    n = make_node("db%d" % k, "db")
    attach(n, backend_lan, "10.10.2.%d" % (30 + k))
    if cfg["data_size"]:
        bs = n.Blockstore("db%d-data" % k, "/mnt/data")
        bs.size = cfg["data_size"]

pc.printRequestRSpec(request)
