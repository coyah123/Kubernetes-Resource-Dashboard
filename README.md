# K8s Resource Dashboard

Find deployments that over-request CPU/memory and force the scheduler to spin up new
nodes even when real usage is low.

## The core problem this solves

The K8s scheduler places pods based on their **requests**, not their actual usage. A
deployment can request 4Gi and use 200Mi — it still reserves 4Gi of a node's capacity.
Enough of those and nodes look "full" (can't schedule more) while `kubectl top nodes`
shows them nearly idle. That's the gap this dashboard highlights.

## Quick start (any OS — Windows, macOS, Linux)

The desktop app talks to your cluster by shelling out to the `kubectl` already on
your PATH, using whatever kubeconfig/context you have configured. No SSH, no
copying JSON around, no extra dependencies — just Python's built-in tkinter.

```bash
python k8s_dashboard_gui.py
```

1. Pick a **context** from the dropdown (populated from your kubeconfig).
2. Hit **⟳ Refresh from cluster** — it runs the kubectl commands in the
   background and renders the tabs.

That's it. Optionally tick "also save JSON to data folder" to cache a snapshot
you can reopen later (offline / air-gapped).

### Non-standard kubectl

If your cluster needs a wrapper (e.g. k3s on a Pi), set `KUBECTL`:

```bash
KUBECTL="sudo k3s kubectl" python k8s_dashboard_gui.py
```

If kubectl isn't on PATH the app says so and falls back to loading JSON files
(see the collector scripts `collect.sh` / `collect.ps1`, or the cached snapshot).

### Tabs
- **Nodes** — requested % vs used % per node; double-click a node to see its pods
- **Deployments** — per-pod and total (×replicas) reserved footprint
- **Pods** — filter by namespace/node
- **Biggest offenders** — pods reserving far more memory than they actually use

### Offline / web variant
`app.py` is an optional Streamlit web version that reads cached JSON from `./data`
(`pip install -r requirements.txt; streamlit run app.py`). The collector scripts
still exist for machines where you'd rather dump JSON separately.

## Raw kubectl commands (no dashboard)

Requests/limits for every deployment:

```bash
kubectl get deploy -A -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,REPLICAS:.spec.replicas,CPU_REQ:.spec.template.spec.containers[*].resources.requests.cpu,CPU_LIM:.spec.template.spec.containers[*].resources.limits.cpu,MEM_REQ:.spec.template.spec.containers[*].resources.requests.memory,MEM_LIM:.spec.template.spec.containers[*].resources.limits.memory'
```

Which node + how packed each node is (the money command):

```bash
kubectl get pods -A -o wide          # NODE column
kubectl describe node <node>         # "Allocated resources" = requested % of capacity
```

Actual usage:

```bash
kubectl top nodes
kubectl top pods -A --sum=true
```
