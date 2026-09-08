# K8s Resource Dashboard

Find deployments that over-request CPU/memory and force the scheduler to spin up new
nodes even when real usage is low.

## The core problem this solves

The K8s scheduler places pods based on their **requests**, not their actual usage. A
deployment can request 4Gi and use 200Mi — it still reserves 4Gi of a node's capacity.
Enough of those and nodes look "full" (can't schedule more) while `kubectl top nodes`
shows them nearly idle. That's the gap this dashboard highlights.

The desktop app talks to your cluster by shelling out to the `kubectl` already on
your PATH, using whatever kubeconfig/context you have configured. No SSH, no copying
JSON around, no extra dependencies — just Python's built-in tkinter. Works the same on
Windows, macOS, and Linux.

## Prerequisites

You need three things on your machine. No `pip install` is required for the
desktop app itself — it uses only the Python standard library.

1. **Python 3** with **tkinter** (the GUI toolkit).
   - The official installer from [python.org](https://www.python.org/downloads/)
     already bundles tkinter on all platforms.
   - Verify with: `python -m tkinter` (a tiny test window should pop up).
   - See the macOS/Linux note in **Troubleshooting** if that command errors.
2. **kubectl** on your PATH.
   - Verify with: `kubectl version --client`.
   - Install: `brew install kubectl` (macOS), `choco install kubernetes-cli`
     (Windows), or your distro's package manager.
3. **A working kubeconfig** — at least one cluster you can actually reach.
   - Verify with: `kubectl config get-contexts` (these are the contexts the
     dropdown will list) and `kubectl get nodes` (proves you can connect + auth).
   - The app only sees clusters already configured on *your* machine; it never
     copies or ships a kubeconfig for you.

## Usage

Open a terminal in the project folder and start the desktop app. The only
per-OS difference is the launcher name (`py`/`python` on Windows, `python3` on
macOS/Linux):

**Windows** (PowerShell or Command Prompt):

```powershell
py k8s_dashboard_gui.py
```

**macOS / Linux:**

```bash
python3 k8s_dashboard_gui.py
```

1. Pick a **context** from the dropdown (populated from your kubeconfig).
2. Hit **⟳ Refresh from cluster** — it runs the kubectl commands in the
   background and renders the tabs.

That's it. The app starts empty and only shows data after you refresh (or click
**Load files** on a cached snapshot). Optionally tick "also save JSON to data
folder" to cache a snapshot you can reopen later (offline / air-gapped).

**Tabs:**
- **Nodes** — requested % vs used % per node; double-click a node to see its pods
- **Deployments** — per-pod and total (×replicas) reserved footprint
- **Pods** — filter by namespace/node
- **Biggest offenders** — pods reserving far more memory than they actually use
- **Trends** — record resource use over time and graph it (see below)

### Trends tab (capture over a day)

Point-in-time snapshots miss daily peaks and troughs. The Trends tab samples
deployment resource data on a fixed interval while the app is left running, so you
build up a full day's picture instead of one moment.

1. Pick an **interval** (5 / 15 / 30 min or 1 hour) and click **▶ Start capture**.
   It takes a sample immediately, then again every interval. Leave the app open
   (it can run all day on someone's machine).
2. Choose a **namespace** — the graph draws **one line per deployment** in it.
3. Choose the **metric** (Memory or CPU) and toggle the **usage** / **request**
   lines. Solid = actual usage, dashed = requested (reserved).

Samples are appended to `data/trends-history.jsonl` (gitignored), so history
survives restarts and reloads when you reopen the app. Capture uses the same
context selected at the top and needs metrics-server for the usage lines.

**Reading the colors** — a **dark-red row** is a warning flag. What it means
depends on the tab:
- **Nodes** — the node is **over 85% requested** on CPU *or* memory. The
  scheduler sees it as nearly full and can't place many more pods there (even if
  actual usage is low) — these are the nodes at risk of forcing a new node to
  spin up.
- **Deployments** — the deployment is **missing at least one request or limit**.
  The exact gaps are listed in the **"missing"** column (e.g. `web:cpu-lim`).
- **Pods** (and the per-node pod popup) — the pod has **at least one container
  with no CPU or memory limit**. The Pods tab also marks these with ⚠ in the
  "no lim" column.

The **Biggest offenders** tab has no red highlight — it's already sorted
worst-first by wasted (requested − used) memory, so every row is an offender.

**Custom kubectl command (optional)** — by default the app runs plain `kubectl`
from your PATH, which is all most people need. If you have to invoke kubectl
differently — a full path, a differently-named binary, or a wrapper — set the
`KUBECTL` environment variable before launching:

```powershell
# Windows (PowerShell) — example: a kubectl not on PATH
$env:KUBECTL = "C:\tools\kubectl.exe"; py k8s_dashboard_gui.py
```

```bash
# macOS / Linux — example: a versioned or wrapped binary
KUBECTL="kubectl.custom" python3 k8s_dashboard_gui.py
```

**Offline / web variant** — `app.py` is an optional Streamlit web version that
reads cached JSON from `./data` (`pip install -r requirements.txt; streamlit run
app.py`). The collector scripts `collect.sh` / `collect.ps1` still exist for
machines where you'd rather dump JSON separately.

## Kubernetes commands used

Everything the dashboard shows comes from these read-only `kubectl` calls. Run any
of them yourself to manually verify what the app reports. (Add `--context <name>`
to target a specific cluster, and substitute your own kubectl invocation if you set
the `KUBECTL` override.)

**Discover contexts** — populates the dropdown:

```bash
kubectl config get-contexts -o name      # list every context in your kubeconfig
kubectl config current-context           # which one is active by default
```

**Deployments** — requests/limits from each deployment's pod template (the
Deployments tab):

```bash
kubectl get deploy -A -o json
```

Human-readable equivalent of what the app parses out of that JSON:

```bash
kubectl get deploy -A -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,REPLICAS:.spec.replicas,CPU_REQ:.spec.template.spec.containers[*].resources.requests.cpu,CPU_LIM:.spec.template.spec.containers[*].resources.limits.cpu,MEM_REQ:.spec.template.spec.containers[*].resources.requests.memory,MEM_LIM:.spec.template.spec.containers[*].resources.limits.memory'
```

**Pods** — every pod, which **node** it landed on, and per-container requests/limits
(the Pods, Nodes, and Biggest-offenders tabs):

```bash
kubectl get pods -A -o json
```

Human-readable equivalent (the NODE column is the key one):

```bash
kubectl get pods -A -o wide
kubectl describe node <node>     # "Allocated resources" = requested % of capacity
```

**Nodes** — capacity/allocatable per node (the Nodes tab):

```bash
kubectl get nodes -o json
```

**Actual usage (metrics-server)** — real live CPU/memory. `kubectl top` has no JSON
output, so the app hits the raw metrics API. If metrics-server isn't installed these
fail and the usage columns go blank (everything else still works):

```bash
kubectl get --raw /apis/metrics.k8s.io/v1beta1/pods      # what the app calls
kubectl get --raw /apis/metrics.k8s.io/v1beta1/nodes     # what the app calls

kubectl top pods -A --sum=true                            # human-readable equivalent
kubectl top nodes                                         # human-readable equivalent
```

## Troubleshooting / common snags

- **`ModuleNotFoundError: No module named 'tkinter'`** — your Python was built
  without Tk. This commonly happens on macOS (Homebrew Python) and Linux.
  - macOS: `brew install python-tk` (match your version, e.g. `python-tk@3.12`),
    or reinstall Python from python.org which includes it.
  - Debian/Ubuntu: `sudo apt install python3-tk`. Fedora: `sudo dnf install python3-tkinter`.
- **"kubectl not found on PATH"** — install kubectl, or if it's installed under a
  different name/path, point the app at it with the `KUBECTL` env var (see
  **Custom kubectl command** in Usage).
- **Context dropdown is empty** — you have no kubeconfig. Set one up (or set the
  `KUBECONFIG` env var to point at the right file) and click **Reload contexts**.
- **Refresh fails with "connection refused" / timeout** — the cluster's API server
  isn't reachable from this machine. You likely need to be on the right
  network/VPN or tunnel. A context existing in your config does **not** mean it's
  reachable from where you're sitting.
- **Refresh fails with "Unauthorized" / credentials error** — your token/cert
  expired, or a cloud auth plugin isn't installed. EKS needs the `aws` CLI, GKE
  needs `gke-gcloud-auth-plugin`, AKS may need `kubelogin`. Install the one your
  context uses.
- **403 / "forbidden" on some resources** — the app lists across *all* namespaces
  (`-A`). If your account is scoped to a single namespace you won't have
  cluster-wide read; ask for read (list) access on deployments/pods/nodes.
- **Usage columns are blank / "no metrics-server"** — that cluster has no
  metrics-server installed. Everything else still works; only live usage numbers
  are unavailable.
