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

You need three things on your machine (plus one optional extra). No `pip install`
is required for the desktop app itself — it uses only the Python standard library.

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
4. **openssl** on your PATH — *optional*, only for **TLS certificate inspection**
   in a Secret's **Get secret** tab (chain summary, expiry, SAN, fingerprint).
   - Everything else works without it; if it's absent, the cert inspector simply
     shows install guidance instead of failing.
   - The app shells out to the `openssl` **command-line tool** (like it does with
     kubectl) — it does **not** use any Python crypto library.
   - Verify with: `openssl version`.
   - Install: `brew install openssl` (macOS), `winget install ShiningLight.OpenSSL`
     or `choco install openssl` (Windows — Git for Windows also bundles one under
     `C:\Program Files\Git\usr\bin`), or your distro's package manager (Linux).

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
- **🏠 Overview** — cluster summary: node/deploy/pod counts, capacity (allocatable
  vs requested vs used) for CPU and memory, and the biggest memory offenders
- **Nodes** — requested % vs used % per node, plus pod-IP capacity (**ip used /
  ip alloc / ip left**, see **Pod-IP capacity** below); open a node for its
  details, events, and the pods running on it. A **View** switcher keeps the wide
  table readable — **Health** (ready / schedulable / taints), **Compute** (CPU &
  memory), **Networking** (pod-IP capacity), and **Info** (k8s version, instance
  type, zone, OS, container runtime, age) — with the **⚠ why** flag in every view.
  **Tick ☑ rows to bulk Cordon / Uncordon** nodes; red = NotReady or kubelet
  pressure (Memory/Disk/PID), amber = cordoned or over 85% requested
- **Images** — one row per container image across **all pods**, including **init**
  and **ephemeral/debug** containers (not just app containers), each with its
  resolved **sha256 digest** (what actually got pulled). Filter by namespace, type
  (init / app / ephemeral), or free text (matches image, container, pod, or
  workload); double-click to open the owning pod. Sourced from pods — the ground
  truth of what's running
- **Deployments** — filter by namespace; per-pod and total (×replicas) reserved
  footprint; open a deployment for its YAML, events, and the pods it manages;
  **tick ☑ rows to bulk rollout-restart or delete**
- **Pods** — filter; open a pod for describe / YAML / events / **live logs** /
  embedded **shell**; **tick ☑ rows to bulk delete** (see below). Pods hit by a
  **Multi-Attach volume error** are flagged red with the reason in **⚠ why**, and
  a **Reconcile Multi-Attach…** button diagnoses and clears the standoff (see
  **Multi-Attach detection & reconcile** below)
- **Pods by namespace** — pick a namespace, browse its pods, open any one, or
  **tick ☑ to bulk delete**
- **Workloads / Networking / Config / Storage / Security / RBAC** — browse (almost)
  every resource type live, grouped like the official k8s Dashboard's sidebar; open
  any item for its details, and run a few **safe, confirmed actions** (scale /
  rollout restart / delete). **Security / RBAC** covers ServiceAccounts, Roles,
  ClusterRoles, RoleBindings and ClusterRoleBindings (with a ⚠ flag on wildcard
  rules); ClusterRoleBindings can be filtered by their subjects' namespace.
  **Storage** additionally lists **VolumeAttachments** (which PV is attached to
  which node) — rows carrying an **attachError** turn red — and offers
  **Reconcile Multi-Attach…** on a selected **PVC** (see below).
- **Custom Resources** — discovers **every CRD installed in the cluster** at refresh
  (cert-manager, Argo, Gateway API, NGINX VirtualServers/TransportServers, …) and
  lets you browse instances of any of them, using each CRD's own
  `additionalPrinterColumns` so you see its author-defined state. A **⟳ Reload
  CRDs** button re-discovers after installing/removing an operator.
- **Resource Management** — pods reserving far more memory than they actually use
- **Trends** — record resource use over time and graph it (see below)

### Inspecting & acting on resources

**Per-tab refresh** — every tab has its own **⟳ Refresh** that re-fetches *only
that tab's resource*, scoped to the current filter, instead of re-pulling the whole
cluster. E.g. on **Pods by namespace**, refreshing while viewing `kube-system`
runs just `kubectl get pods -n kube-system` and redraws that list. The
**Deployments** and **Pods** refreshes are likewise scoped; the grouped browser
tabs have always refreshed per-type. **Overview** is the exception — it summarizes
every kind, so its refresh does the full "Refresh from cluster".

> Note: the **Nodes** refresh updates node capacity/usage only. The per-node
> "pods" count, "req %" columns, and **"ip used" / "ip left" / "ip used%"**
> columns are all derived from the current pod model, so refresh **Pods** (or
> do a full cluster refresh) to bring those in sync.

Rows across the app share one interaction model:

- **Double-click** a row → its detail opens **in-place** in the same tab, with a
  **← Back** button to return to the list.
- **Single-click the leading ⧉ column** → open the same detail in a **separate
  window** instead.
- On the Pods and Deployments tabs, a leading **☑ column** lets you check several
  rows; the buttons below the table then act on **all checked rows** (or the
  single selected row if none are checked), each behind a confirmation:
  - **Pods** → **Delete** (managed pods get recreated by their controller), plus
    **Reconcile Multi-Attach…** on a single stuck pod
  - **Deployments** → **Rollout restart** and **Delete**
  - **Nodes** → **Cordon** (mark unschedulable — running pods stay put) and
    **Uncordon**

A resource's detail view has tabs for **Describe**, **YAML**, and **Events**.
Pods additionally get **Logs (live)** — a real-time `kubectl logs -f` stream with
pause / restart / clear / auto-scroll — and two embedded terminals, **Shell: sh**
and **Shell: bash**, via `kubectl exec -i`. Controllers (Deployments, StatefulSets,
DaemonSets, ReplicaSets, Jobs) and Nodes also get a **Pods** tab listing what they
run, itself clickable.

**Live edit** — every detail view has an **✎ Edit ▾** dropdown offering two ways
to edit `<kind>/<name>`:

- **Edit in app (built-in YAML editor)** — opens the resource's live YAML in a
  dialog; **✔ Apply** pipes it to `kubectl apply -f -` with server-side
  validation. No external terminal or `$EDITOR` needed, so it works the same on
  every platform.
- **Edit in external terminal (kubectl edit)** — launches `kubectl edit` in a
  **new terminal window**, handing off to kubectl's own flow using your
  `$KUBE_EDITOR`/`$EDITOR` (Notepad on Windows). Save applies, quit-without-save
  cancels.

Click **⟳ Refresh** on a tab afterward to see the result.

**Update credentials without restarting** — the top **Credentials → Update
credentials…** menu opens a dialog where you paste the same credential lines
you'd run in a shell (`export KEY=VALUE`, `set KEY=VALUE`, `$env:KEY="VALUE"`, or
bare `KEY=VALUE`). **Apply** writes them into the running app's environment, so
the next kubectl call uses the refreshed token/`KUBECONFIG`/`KUBECTL`/cloud creds
— no need to quit and relaunch. **✔ Apply & verify** also runs `kubectl get ns`
to confirm the new credentials work. (Env-var credentials only; interactive
logins like `gcloud auth login` still run in your own shell — paste the resulting
exports here afterward.)

### Debugging tab

A dedicated **Debugging** tab (next to **Trends**) for in-cluster network
troubleshooting without leaving the app:

- **Saved debug images** — paste an image tag (e.g. an ACR path) and **＋ Save to
  list**; tags persist to `debug_images.json` in the data folder, which is
  **gitignored** so a private registry path never lands in git. Deploy from the
  **Image** dropdown of saved tags.
- **Deploy a debug pod** — pick an image, name the pod, choose a namespace, and
  **🚀 Deploy** (`kubectl run … --command -- sh -c 'sleep infinity'` so it stays
  Running). List/refresh pods and **🗑 Delete** them from the same panel.
- **Embedded exec** — select a pod and **⇆ Exec into selected** to get a
  line-oriented shell right in the app (same pipe-exec as the pod detail view;
  `ls`/`cat`/`curl` work, full-screen TUIs don't).
- **Network targets (right panel)** — every **Service** (with its
  `name.namespace.svc.cluster.local` hostname) plus any NGINX **VirtualServers**
  and **TransportServers** (with `spec.host`), so you can see hostnames at a
  glance. Click a target to auto-build a connectivity command, then **▶ Run in
  pod** to execute it inside the connected debug pod — testing connectivity while
  keeping the whole target list in view.
- **Pick the call** — a **Tool** dropdown chooses how the command is formed:
  `curl`, `wget` (for images without curl), `nc` (port reachability), `ping`, or
  `nslookup` (DNS). Services build an HTTP call on their port; ingress hosts
  (VirtualServer/TransportServer) build a TLS call on the `spec.host`. The command
  field is editable, so you can tweak it before running.

**Secrets** get an extra **Get secret** tab: each `data` key is listed with its
value **hidden by default** behind a **Reveal values** toggle, a **Decoder**
dropdown (**Base64** first), and a per-key **Copy**. Non-text values render as
`<binary: N bytes>` + hex. If a value is a **PEM certificate**, a **🔎 Inspect
cert** button opens `openssl`-powered views — **Chain summary** (per-cert
subject/issuer/validity with days-left / expired flags and a chain-linkage check),
Full text, Dates, Subject/Issuer, SAN, SHA-256 fingerprint, Serial. This shells out
to the `openssl` binary on your PATH (see Prerequisites); if it's absent the window
shows install guidance instead of failing.

**kubectl command buffer** — a bar pinned to the bottom of the window shows the
kubectl command behind whatever you're doing, so the GUI teaches the CLI rather than
hiding it. Highlight any row and it shows the `kubectl get … -o yaml` you'd run to
fetch that object (tagged *selected*); anything the app actually runs shows tagged
*last run*. A **Copy** button puts it on your clipboard. Commands include
`--context` and honor the `KUBECTL` override, so they're copy-paste runnable.

> The embedded shells are line-oriented (no TTY): `ls`, `cat`, `env` work fine, but
> full-screen programs (`vim`, `top`) and shell prompts won't render. Use `ls -C`
> for columns. Bash is unavailable in minimal images (e.g. Alpine) — use **sh**.

**Safe actions** on the grouped tabs — every mutating action asks for confirmation
first, then reloads the list:
- **Scale…** (Deployments / StatefulSets / ReplicaSets) — prompts for a replica count
- **Rollout restart** (Deployments / StatefulSets / DaemonSets)
- **Delete…** — with a warning prompt

There is no create/edit-and-apply or deploy wizard — this stays a read-mostly tool
with a few common, guarded actions.

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

**Reading the colors** — a **dark-red row** flags something actually wrong at a
glance (missing resource limits alone is *not* reddened — it's common and rarely
urgent):
- **Nodes** — the node is **over 85% requested** on CPU *or* memory (scheduler
  sees it as nearly full), **over 85% of its pod-IP capacity used**, or
  **NotReady**. The **⚠ why** column spells out which. A node can look idle on
  CPU/memory and still be unable to take another pod if it's out of IPs —
  that's exactly what the IP columns catch.
- **Pods** and **Pods by namespace** — the pod is unhealthy (not
  Running/Succeeded, a container reason like `CrashLoopBackOff`/`ImagePullBackOff`,
  or restarts). The **⚠ why** column gives the exact reason for every red row.
  This includes a **Multi-Attach volume error** (a pod Event, normally invisible
  in the pod object) — see **Multi-Attach detection & reconcile** below.
- **Storage → VolumeAttachments** — the attachment reports an **attachError**
  (or detachError): the CSI/attach-detach controller couldn't (de)attach the
  volume, the cluster-side face of a Multi-Attach standoff.

Missing requests/limits are still surfaced, just not in red: the **Deployments**
tab lists the exact gaps in its **"missing"** column (e.g. `web:cpu-lim`), the
**Pods** tab marks them with ⚠ in the "no lim" column, and the **Overview** shows
counts. The **Resource Management** tab has no red highlight — it's already sorted
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

### Air-gapped dependency bundle (`Airgapped_Prep.py`)

The **desktop app needs no dependencies** — only Python-with-tkinter. This helper
is for the optional Streamlit web app: run it on a machine **with** internet to
download every wheel in `requirements.txt` (and its transitive deps) for Windows,
macOS, and Linux, each into its own folder, so you can install with **no network**
on an air-gapped box.

```bash
python Airgapped_Prep.py                 # -> ./airgapped_bundle/{windows,macos,linux}/
python Airgapped_Prep.py --include-arm   # also Linux aarch64 + more macOS arm64
```

The OS you run it on doesn't matter — `pip download --platform` cross-fetches wheels
for all targets. Each OS folder gets the `.whl` files, a copy of `requirements.txt`,
and an **`INSTALL.txt`** with the exact offline command (there's also a combined
`INSTALL_COMMANDS.txt` at the top). On the offline machine, copy the matching OS
folder over and run its one command:

```bash
python -m pip install --no-index --find-links . -r requirements.txt
```

Options: `--python-versions 3.11 3.12 3.13` (which interpreter versions to fetch
for), `--os windows macos linux` (subset), `--requirements PATH`, `--output DIR`.

## Python imports (all standard library)

The desktop app has **zero third-party Python dependencies** — every import below
ships with CPython. There is no `pip install` step and no packages to vendor. (The
only *external* things it uses are the `kubectl` and optional `openssl` **command-
line binaries**, not Python libraries — see Prerequisites.)

| Import | Stdlib? | Used for |
|---|---|---|
| `tkinter` (`tk`, `ttk`, `filedialog`, `messagebox`, `simpledialog`) | ✅ | The entire GUI — windows, notebook tabs, tables, dialogs, themed widgets |
| `subprocess` | ✅ | Running the external `kubectl` and `openssl` binaries and capturing their output |
| `shutil` | ✅ | `shutil.which()` — locating `kubectl` / `openssl` on the user's PATH |
| `shlex` | ✅ | Splitting a `KUBECTL` override (e.g. `"sudo k3s kubectl"`) into args, respecting quotes; and `shlex.quote()` to safely quote the argv when launching `kubectl edit` in an external terminal on macOS/Linux (must be imported in **both** `kubectl_collect.py` and `k8s_dashboard_gui.py` — the GUI's macOS/Linux terminal-launch path calls it, so a missing import there surfaces as a `NameError` only on those platforms) |
| `os` | ✅ | Reading env vars (`KUBECTL`, `KUBECONFIG`, and `KUBE_EDITOR`/`EDITOR` for live edit) |
| `sys` | ✅ | Platform detection (`win32` → hide the console window) and `argv` |
| `threading` | ✅ | Running kubectl/openssl on background threads so the UI never blocks |
| `queue` | ✅ | Thread-safe handoff of results from worker threads back to the UI thread |
| `json` | ✅ | Parsing `kubectl ... -o json` and reading/writing cached snapshots |
| `base64` | ✅ | Decoding Secret `data` values (the Secrets decoder) |
| `binascii` | ✅ | Catching invalid-base64 errors (`binascii.Error`) when decoding secrets |
| `re` | ✅ | Matching PEM certificate blocks and parsing CRD printer-column JSONPaths |
| `csv` | ✅ | Exporting any table to a CSV file |
| `datetime` (`datetime`, `timezone`) | ✅ | Resource age and TLS-cert expiry math (days remaining / expired) |
| `time` | ✅ | Timestamps ("updated HH:MM:SS"), Trends sample times |
| `pathlib` (`Path`) | ✅ | Filesystem paths (data folder, trends history) |
| `__future__` (`annotations`) | ✅ | Deferred type-annotation evaluation |
| `kubectl_collect` | local | The project's own kubectl wrapper module |
| `trends` | local | The project's own Trends capture + line-chart module |

> The optional Streamlit web variant (`app.py`) is the *only* part that needs
> third-party packages (`streamlit`, `pandas` in `requirements.txt`). The desktop
> app does not import them.

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

**Nodes** — capacity/allocatable per node, plus schedulable state, taints, kubelet
conditions, and `nodeInfo` (version / OS / runtime) for the Nodes tab's views:

```bash
kubectl get nodes -o json
```

The Nodes tab's **Cordon / Uncordon** buttons are the only node mutations, each
behind a confirmation:

```bash
kubectl cordon <node>      # mark unschedulable (spec.unschedulable=true)
kubectl uncordon <node>    # mark schedulable again
```

**Images** — the Images tab reads the same `kubectl get pods -A -o json` above and
walks each pod's `initContainers`, `containers`, and `ephemeralContainers`, pairing
declared images with the resolved digest from the matching `*Statuses[].imageID`:

```bash
kubectl get pods -A -o jsonpath='{range .items[*]}{range .spec.initContainers[*]}{.image}{"\n"}{end}{range .spec.containers[*]}{.image}{"\n"}{end}{end}' | sort -u
```

### Pod-IP capacity (Nodes tab: "ip used" / "ip alloc" / "ip left" / "ip used%")

Kubernetes has no API field that literally says "IPs remaining on this node."
There's no `GET`-able free-address-pool endpoint — the CNI plugin owns that
bookkeeping internally and the API server never sees it. So instead of trying
to talk to the CNI (which would mean different code per CNI, and no code at
all for whichever one *your* cluster runs), the app derives an equivalent
number from two `kubectl` calls it's already making for other tabs. No extra
requests are issued for this.

**Step 1 — get the ceiling, from `kubectl get nodes -o json`:**

Every node object carries `status.allocatable`, the same block the CPU/memory
allocatable columns already come from:

```json
"status": {
  "allocatable": { "cpu": "4", "memory": "15897452Ki", "pods": "110" }
}
```

`build_node_row()` reads `allocatable.pods` straight off that and stores it as
`pods_alloc` — this becomes the **`ip alloc`** column. That field is kubelet's
**max-pods** setting for the node: the scheduler will refuse to place a pod
there once it's hit, full stop, regardless of how much spare CPU/memory the
node has. How that number gets set varies by environment:

- **AWS EKS with the VPC CNI** computes it *for* you, from real IP capacity: a
  bootstrap script (`max-pods-calculator.sh`) looks at the EC2 instance
  type's ENI count and IPs-per-ENI and sets `--max-pods` to exactly the number
  of pod IPs that instance can hand out. Here, `ip alloc` **is** the literal
  IP ceiling — not a proxy, the actual number.
- **Everything else** (GKE, AKS, kubenet, Calico, Cilium, Flannel, on-prem,
  k3s/minikube, …) sets `--max-pods` to a fixed default (often 110) that's
  usually *unrelated* to actual subnet size — a typical `/24` pod-CIDR block
  has 254 usable addresses, well above 110, so IPs aren't really the binding
  constraint there. But `--max-pods` is still the real, enforced cap on how
  many more pods (and therefore pod IPs) that node can accept, so the column
  is still answering the right practical question — it just isn't always
  "how many raw addresses are free in the subnet."

**Step 2 — get current usage, from `kubectl get pods -A -o json`:**

Every pod carries `spec.nodeName` (which node it landed on) and
`spec.hostNetwork` (`build_pod_row()` records both). In the Nodes tab's
`refill()`, the app loops over every pod currently held in memory and buckets
them by `nodeName`, incrementing a per-node counter for each pod **except**
ones with `hostNetwork: true`. A hostNetwork pod (common for CNI daemonsets,
`kube-proxy`, some monitoring agents) doesn't get its own pod IP at all — it
binds directly to the node's own host IP/ports — so counting it would
overstate usage. That per-node counter is the **`ip used`** column.

**Step 3 — combine them:**

The node list and the pod list come from two independent `kubectl` calls;
they're joined in memory purely by matching `pod.node == node.name` (there's
no single query that returns both together). Once joined:

```
ip alloc  = node's status.allocatable.pods
ip used   = count of pods on that node where hostNetwork != true
ip left   = ip alloc − ip used
ip used%  = ip used / ip alloc × 100   (drives the ⚠ over-85% warning,
                                          same threshold as the CPU/mem checks)
```

Worked example from this cluster: node `norbbuntu` has `allocatable.pods =
110`. 25 pods are currently scheduled on it, none of them hostNetwork, so
`ip used = 25`, `ip left = 85`, `ip used% ≈ 23%` — nowhere near the warning
threshold.

**Why this is accurate:** it's always derived from the same two live API
objects (`Node.status.allocatable.pods`, `Pod.spec.nodeName` /
`Pod.spec.hostNetwork`) that the scheduler itself uses to decide placement —
so "ip left" reflects the same ceiling `kubectl` and the scheduler would hit.
It's also universal: it works identically on EKS, GKE, AKS, k3s, kubeadm,
minikube — anything you can point `kubectl` at — with zero cloud-provider API
calls or CNI-specific code.

**Why it's not perfectly accurate everywhere:** on non-VPC-CNI clusters,
`allocatable.pods` is a policy cap chosen by whoever configured kubelet, not a
live count of free addresses in an IP range — a node could theoretically have
"0 IPs left" per this column while its actual subnet still has hundreds of
unused addresses (max-pods hit first), or vice versa in a misconfigured
cluster. The app deliberately does **not** read `spec.podCIDR` and compute
subnet math (network address, broadcast, usable host count) as an
alternative, because many common CNIs — AWS VPC CNI chief among them — don't
populate `podCIDR` on the node at all, so that approach would silently break
on exactly the clusters where IP exhaustion is most likely to matter. Also
worth knowing: a completed/terminated pod that still exists as an object
(e.g. an unswept `Job` pod in `Succeeded`/`Failed` phase) is still counted
here if present in the current pod list, even though its actual IP has
usually already been released back to the CNI — a stale/uncleaned pod list
can make usage look slightly higher than it really is.

Human-readable equivalent, run per node:

```bash
kubectl get node <node> -o jsonpath='{.status.allocatable.pods}'
kubectl get pods -A --field-selector spec.nodeName=<node> -o json \
  | jq '[.items[] | select(.spec.hostNetwork != true)] | length'
```

### Multi-Attach detection & reconcile (Pods tab + Storage tab)

A **`Multi-Attach error for volume …`** happens when a **ReadWriteOnce** (RWO)
PVC is still attached to a pod on one node while a new pod tries to mount it on
another node. RWO means "mountable by one node at a time," so the second mount
can't proceed until the first releases. The classic trigger is a node
drain/crash/upgrade: the old pod gets stuck `Terminating`, its **VolumeAttachment**
never releases, and the replacement pod hangs in `ContainerCreating`/`Pending`
indefinitely. (ReadWriteMany volumes can't hit this — they're legitimately
mounted on many nodes at once, so the tool ignores them here.)

**Why the app has to work for this.** The failure surfaces as a **pod Event**
(`FailedAttachVolume` / `FailedMount` with a "Multi-Attach" message), *not* as a
container `waiting.reason`. A plain `kubectl get pods` just shows
`ContainerCreating` — the real cause is hidden. So the dashboard reads Events to
make it visible, and correlates several objects to point at the actual fix.

**1. Detect (Pods tab).** On each Pods refresh, alongside `kubectl get pods` the
app also fetches Events in the same scope and scans them for the attach/mount
failures. Any match tags the affected pod with a note (e.g. *"Multi-Attach (PVC
held elsewhere)"*) in the **⚠ why** column and forces the row **red**, even
though the pod's phase is only `Pending`. This is best-effort: if listing Events
is denied or errors, the pod table still renders normally.

```bash
kubectl get events -A -o json      # scanned for reason FailedAttachVolume / FailedMount
```

**2. Analyze.** When you select a stuck pod (Pods tab) or its PVC (Storage tab)
and click **Reconcile Multi-Attach…**, the app builds the dependency graph that
identifies *what is holding the volume*:

```bash
kubectl get pod <pod> -n <ns> -o json          # the stuck pod → its RWO PVCs + target node
kubectl get pvc -n <ns> -o json                # each PVC → its PV + access modes (RWO only)
kubectl get volumeattachments -o json          # which PV is attached to which node (the holder)
kubectl get pods -n <ns> -o json               # the pod on the holder node still mounting that PVC
```

The chain is: **stuck pod → RWO PVC → PV → VolumeAttachment on a *different*
node → the stale pod on that node**. The stale pod (usually the old one stuck
Terminating) is the real thing to remove to free the volume; the VolumeAttachment
is the fallback target if that pod is already gone but the attachment lingers.

**3. Reconcile.** A dialog shows exactly what's holding each volume — the PVC/PV,
the holder node(s), the VolumeAttachment name(s), and the stale pod(s) — then
offers two **destructive, individually-confirmed** fixes:

```bash
kubectl delete pod <stale-pod> -n <ns> --force --grace-period=0   # free the volume (usual fix)
kubectl delete volumeattachment <name>                            # last resort: orphaned attachment
```

> ⚠ **Safety.** Force-deleting a pod (or deleting a VolumeAttachment) assumes the
> old node is genuinely gone. Doing it while the volume is *still mounted* on a
> live node can corrupt data. The dialog never acts on its own — each button
> requires a separate confirmation and names precisely what it will remove. This
> is the one place the "read-mostly" tool takes a genuinely disruptive action, so
> it's deliberately behind an explicit diagnosis screen.

**Two entry points, one engine.** The Pods tab starts from the *symptom* (the red
pod); the Storage tab starts from the *resource* (the PVC, or you can eyeball the
red VolumeAttachment rows first). Both converge on the same analysis and dialog,
so the diagnosis and the safety gates are identical either way.

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
- **`macOS 26 (2601) or later required, have instead 16 (1601)` followed by
  `zsh: abort`** — this is a bug in Apple's ancient system Tcl/Tk (the one
  bundled with `/usr/bin/python3`), not this app. Its internal OS-version
  check can't parse macOS's newer year-based version numbers (macOS 26 reads
  back as "16") and aborts before any of the app's code runs. Same class of
  bug as the old `macOS 11 (1107) or later required, have instead 11 (1106)`
  error from the 10.x → 11 jump. Fix: don't launch with the stock system
  Python — use one with a modern bundled Tk:
  - `brew install python-tk@3.12 python@3.12`, then run with that interpreter,
    e.g. `/opt/homebrew/bin/python3.12 k8s_dashboard_gui.py` (Apple Silicon) or
    `/usr/local/bin/python3.12 ...` (Intel) — or install Python from
    [python.org](https://www.python.org/downloads/), which bundles a current
    Tcl/Tk.
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
