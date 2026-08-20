# cloudlab-cluster

Reusable CloudLab testbed infrastructure: a parameterized bare-metal profile
that boots a k3s cluster with pinned, hostNetwork service pods, plus the
bootstrap, golden-image, verification, and result-collection tooling around
it.

Built for distributed admission-control experiments (`dcb` in paths and pod
names is an opaque project code name -- the research design and controllers
live in a separate private repository), but the infrastructure is generic:
isolated client/backend experiment LANs, explicit pod placement, two-layer
image-aware bootstrap, a 13-check smoke suite, and checksummed result bundles.

```
profile.py                 CloudLab geni-lib profile (presets: smoke/full/submission)
cloudlab/bootstrap.sh      two-layer node bootstrap (bake layer + boot layer)
cloudlab/bake.sh           prepare a node for golden-image capture
cloudlab/gen_manifests.py  k8s objects with pinned placement
services/                  stub load generator / frontend / storage (plumbing)
orchestrator/              smoke checks, topology builder, result collection
docs/smoke-test-runbook.md how to run all of it
```

Quick start: `make local` runs the whole request path on your machine with no
CloudLab account. Then see the runbook.
