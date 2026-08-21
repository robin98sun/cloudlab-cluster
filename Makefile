# Testbed workflow (k3s layout). See docs/smoke-test-runbook.md.
SHELL := /bin/bash
TOPOLOGY ?= topology.json
LABEL    ?= smoke
USER_    ?= $(shell whoami)
DOMAIN   ?=
LG ?= 0
FE ?= 1
DB ?= 1
INSTANCES ?= 3

.PHONY: help local topology topology-derive smoke smoke-fast collect run clean

help:
	@echo "local            run 3 FE instances + DB on this machine (no CloudLab)"
	@echo "topology         build $(TOPOLOGY) from a downloaded manifest.xml"
	@echo "topology-derive  build $(TOPOLOGY) from counts + DOMAIN=..."
	@echo "smoke            run all checks against the allocated testbed"
	@echo "smoke-fast       skip the load-generating checks (S07,S12,S13)"
	@echo "collect          assemble a verified result bundle"
	@echo "run              smoke + collect"

local:
	@./orchestrator/local_check.sh

topology:
	@test -f manifest.xml || { echo "download the manifest from the CloudLab portal to ./manifest.xml"; exit 1; }
	python3 orchestrator/topology.py from-manifest manifest.xml \
		--fe-instances $(INSTANCES) --out $(TOPOLOGY)

topology-derive:
	@test -n "$(DOMAIN)" || { echo "set DOMAIN=<exp>.<proj>.<cluster>.cloudlab.us"; exit 1; }
	python3 orchestrator/topology.py derive --user $(USER_) --domain $(DOMAIN) \
		--lg $(LG) --fe $(FE) --db $(DB) --fe-instances $(INSTANCES) \
		--out $(TOPOLOGY)

smoke:
	python3 orchestrator/smoke.py --topology $(TOPOLOGY) --out smoke-results.json

smoke-fast:
	python3 orchestrator/smoke.py --topology $(TOPOLOGY) --skip S07,S12,S13 \
		--out smoke-results.json

collect:
	python3 orchestrator/collect.py --topology $(TOPOLOGY) --label $(LABEL) \
		--smoke-results smoke-results.json

run: smoke collect

clean:
	rm -f smoke-results.json $(TOPOLOGY)

MON_NS ?= monitoring
monitoring:
	@ctl=$$(python3 -c "import json;t=json.load(open('$(TOPOLOGY)'));print(next(n['control'] for n in t['nodes'] if n['role']=='ctl'))"); \
	dbips=$$(python3 -c "import json;t=json.load(open('$(TOPOLOGY)'));print(','.join('%s:9101'%d['ip'] for d in t['destinations']))" 2>/dev/null); \
	echo "deploying monitoring to $$ctl (storaged targets: $$dbips)"; \
	scp -q -o BatchMode=yes monitoring/monitoring.yaml monitoring/dashboard.yaml robin98@$$ctl:/tmp/; \
	ssh -o BatchMode=yes robin98@$$ctl "sudo /usr/local/bin/k3s kubectl apply -f /tmp/monitoring.yaml -f /tmp/dashboard.yaml"; \
	echo "Grafana: http://$$ctl:3000  Prometheus: http://$$ctl:9090"
