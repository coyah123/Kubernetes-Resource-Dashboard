"""
KR8M — Kubernetes Resource Manager — native desktop window, standard library ONLY.

Why this version: no third-party packages, no web server, no open network port —
just tkinter (ships with Python). It talks to your cluster by shelling out to the
`kubectl` already on your PATH, using whatever kubeconfig/context you have set up.
Works the same on Windows, macOS, and Linux.

Run:
    python k8s_dashboard_gui.py [path-to-data-folder]

- Pick a context and hit "Refresh from cluster" to pull live data via kubectl.
- Or load previously-collected JSON files from a folder (offline / air-gapped).

Override the kubectl binary for non-standard setups:
    KUBECTL="sudo k3s kubectl" python k8s_dashboard_gui.py
"""
from __future__ import annotations

import csv
import json
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import kubectl_collect
import trends

# How often each interval choice fires, in milliseconds.
_INTERVALS = {"5 min": 5 * 60_000, "15 min": 15 * 60_000,
              "30 min": 30 * 60_000, "1 hour": 60 * 60_000}

# ---------------------------------------------------------------------------
# Quantity parsing (CPU -> millicores, memory -> bytes)
# ---------------------------------------------------------------------------
_CPU_SUFFIX = {"n": 1e-6, "u": 1e-3, "m": 1.0}
_MEM_SUFFIX = {
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5,
    "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "k": 1e3,
}


def cpu_to_milli(q):
    if not q:
        return 0.0
    q = str(q).strip()
    for suf, mult in _CPU_SUFFIX.items():
        if q.endswith(suf):
            return float(q[:-len(suf)]) * mult
    return float(q) * 1000.0


def mem_to_bytes(q):
    if not q:
        return 0.0
    q = str(q).strip()
    for suf, mult in _MEM_SUFFIX.items():
        if q.endswith(suf):
            return float(q[:-len(suf)]) * mult
    return float(q)


def fmt_cpu(m):
    if m is None:
        return "—"
    if m >= 1000:
        return f"{m/1000:.2f} cores"
    return f"{m:.0f}m"


def fmt_mem(b):
    if b is None:
        return "—"
    for unit, size in (("Gi", 1024**3), ("Mi", 1024**2), ("Ki", 1024)):
        if b >= size:
            return f"{b/size:.1f}{unit}"
    return f"{b:.0f}B"


def pct(n, d):
    return f"{n/d*100:.0f}%" if d else "—"


def age_from(ts):
    """Kubernetes creationTimestamp (ISO8601 Z) -> compact age like '3d4h', '12m'."""
    if not ts:
        return "—"
    try:
        t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return "—"
    secs = int((datetime.now(timezone.utc) - t).total_seconds())
    if secs < 0:
        secs = 0
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d{h}h" if h else f"{d}d"
    if h:
        return f"{h}h{m}m" if m else f"{h}h"
    if m:
        return f"{m}m"
    return f"{secs}s"


# ---------------------------------------------------------------------------
# Load + shape the data
# ---------------------------------------------------------------------------
def _load(folder: Path, name: str) -> dict:
    p = folder / name
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return {}


def build_model(folder: Path) -> dict:
    """Build the model from JSON files on disk (the offline / cached path)."""
    return build_model_from_raw({
        "deployments": _load(folder, "deployments.json"),
        "replicasets": _load(folder, "replicasets.json"),
        "pods": _load(folder, "pods.json"),
        "nodes": _load(folder, "nodes.json"),
        "pod_metrics": _load(folder, "pod-metrics.json"),
        "node_metrics": _load(folder, "node-metrics.json"),
    })


def build_pod_row(p: dict, usage=(None, None)) -> dict:
    """One pod's row dict (the shape every pod table consumes). `usage` is the
    (cpu_millicores, mem_bytes) from metrics-server, or (None, None) if absent."""
    ns, name = p["metadata"]["namespace"], p["metadata"]["name"]
    spec = p.get("spec", {})
    status = p.get("status", {})
    node = spec.get("nodeName", "<unscheduled>")
    phase = status.get("phase", "?")
    cstatuses = status.get("containerStatuses", [])
    n_ready = sum(1 for cs in cstatuses if cs.get("ready"))
    n_total = len(spec.get("containers", []))
    restarts = sum(cs.get("restartCount", 0) for cs in cstatuses)
    # Human-readable reason a pod is unhealthy (empty string == healthy).
    reasons = []
    for cs in cstatuses:
        cstate = cs.get("state", {})
        w, t = cstate.get("waiting"), cstate.get("terminated")
        if w and w.get("reason"):
            reasons.append(w["reason"])
        elif t and t.get("reason") and t.get("exitCode"):
            reasons.append(t["reason"])
    note_parts = []
    if phase not in ("Running", "Succeeded"):
        note_parts.append(phase)
    for r in dict.fromkeys(reasons):
        if r not in note_parts:
            note_parts.append(r)
    if restarts:
        note_parts.append(f"{restarts} restart" + ("s" if restarts != 1 else ""))
    owner = next((f'{o["kind"]}/{o["name"]}' for o in p["metadata"].get("ownerReferences", [])), "-")
    cpu_req = cpu_lim = mem_req = mem_lim = 0.0
    missing = 0
    for c in spec.get("containers", []):
        res = c.get("resources", {})
        req, lim = res.get("requests", {}), res.get("limits", {})
        cpu_req += cpu_to_milli(req.get("cpu"))
        mem_req += mem_to_bytes(req.get("memory"))
        cpu_lim += cpu_to_milli(lim.get("cpu"))
        mem_lim += mem_to_bytes(lim.get("memory"))
        if not lim.get("cpu") or not lim.get("memory"):
            missing += 1
    return {
        "namespace": ns, "pod": name, "node": node, "phase": phase, "owner": owner,
        "ready": f"{n_ready}/{n_total}", "restarts": restarts,
        "age": age_from(p["metadata"].get("creationTimestamp")),
        "pod_ip": status.get("podIP", "—"), "status_note": ", ".join(note_parts),
        "cpu_req_m": cpu_req, "cpu_lim_m": cpu_lim, "mem_req_b": mem_req, "mem_lim_b": mem_lim,
        "cpu_used_m": usage[0], "mem_used_b": usage[1], "missing_limits": missing,
    }


def build_node_row(n: dict, usage=(None, None)) -> dict:
    alloc = n["status"].get("allocatable", {})
    ready = next((c["status"] for c in n["status"].get("conditions", [])
                  if c["type"] == "Ready"), "?")
    return {
        "node": n["metadata"]["name"], "ready": ready,
        "cpu_alloc_m": cpu_to_milli(alloc.get("cpu")),
        "mem_alloc_b": mem_to_bytes(alloc.get("memory")),
        "cpu_used_m": usage[0], "mem_used_b": usage[1],
    }


def build_dep_row(d: dict, used=None) -> dict:
    ns, name = d["metadata"]["namespace"], d["metadata"]["name"]
    replicas = d["spec"].get("replicas", 1)
    ready = d.get("status", {}).get("readyReplicas", 0)
    cpu_req = cpu_lim = mem_req = mem_lim = 0.0
    missing = []
    for c in d["spec"]["template"]["spec"].get("containers", []):
        res = c.get("resources", {})
        req, lim = res.get("requests", {}), res.get("limits", {})
        cpu_req += cpu_to_milli(req.get("cpu"))
        mem_req += mem_to_bytes(req.get("memory"))
        cpu_lim += cpu_to_milli(lim.get("cpu"))
        mem_lim += mem_to_bytes(lim.get("memory"))
        if not req.get("cpu"): missing.append(f'{c["name"]}:cpu-req')
        if not req.get("memory"): missing.append(f'{c["name"]}:mem-req')
        if not lim.get("cpu"): missing.append(f'{c["name"]}:cpu-lim')
        if not lim.get("memory"): missing.append(f'{c["name"]}:mem-lim')
    return {
        "namespace": ns, "deployment": name, "replicas": replicas, "ready": ready,
        "cpu_req_m": cpu_req, "cpu_lim_m": cpu_lim, "mem_req_b": mem_req, "mem_lim_b": mem_lim,
        "cpu_req_total_m": cpu_req * replicas, "mem_req_total_b": mem_req * replicas,
        "cpu_used_m": used[0] if used else None,
        "mem_used_b": used[1] if used else None,
        "missing": ", ".join(missing),
    }


def usage_map_from_metrics(pod_metrics: dict) -> dict:
    """(namespace, pod) -> (cpu_millicores, mem_bytes) from a pod metrics list."""
    out = {}
    for it in pod_metrics.get("items", []):
        ns, name = it["metadata"]["namespace"], it["metadata"]["name"]
        cpu = sum(cpu_to_milli(c.get("usage", {}).get("cpu")) for c in it.get("containers", []))
        mem = sum(mem_to_bytes(c.get("usage", {}).get("memory")) for c in it.get("containers", []))
        out[(ns, name)] = (cpu, mem)
    return out


def build_model_from_raw(raw: dict) -> dict:
    """Build the model from the five parsed kubectl JSON objects (live or cached)."""
    deployments = raw.get("deployments") or {}
    replicasets = raw.get("replicasets") or {}
    pods = raw.get("pods") or {}
    nodes = raw.get("nodes") or {}
    pod_metrics = raw.get("pod_metrics") or {}
    node_metrics = raw.get("node_metrics") or {}

    # ReplicaSet -> owning Deployment, so we can roll pod usage up to a deployment.
    # (Pods are owned by a ReplicaSet named "<deployment>-<hash>", which is in turn
    # owned by the Deployment.)
    rs_to_dep = {}
    for rs in replicasets.get("items", []):
        ns = rs["metadata"]["namespace"]
        dep = next((o["name"] for o in rs["metadata"].get("ownerReferences", [])
                    if o.get("kind") == "Deployment"), None)
        if dep:
            rs_to_dep[(ns, rs["metadata"]["name"])] = dep

    pod_usage = usage_map_from_metrics(pod_metrics)
    node_usage = {}
    for it in node_metrics.get("items", []):
        node_usage[it["metadata"]["name"]] = (
            cpu_to_milli(it.get("usage", {}).get("cpu")),
            mem_to_bytes(it.get("usage", {}).get("memory")),
        )

    node_rows = [build_node_row(n, node_usage.get(n["metadata"]["name"], (None, None)))
                 for n in nodes.get("items", [])]

    pod_rows = []
    dep_usage = {}  # (ns, deployment) -> [cpu_m, mem_b] summed actual usage of its pods
    for p in pods.get("items", []):
        ns, name = p["metadata"]["namespace"], p["metadata"]["name"]
        row = build_pod_row(p, pod_usage.get((ns, name), (None, None)))
        pod_rows.append(row)
        # Attribute this pod's usage to a Deployment (via its ReplicaSet owner).
        refs = p["metadata"].get("ownerReferences", [])
        dep_name = next((rs_to_dep.get((ns, o["name"])) for o in refs
                         if o.get("kind") == "ReplicaSet" and (ns, o["name"]) in rs_to_dep), None)
        if dep_name and row["cpu_used_m"] is not None:
            acc = dep_usage.setdefault((ns, dep_name), [0.0, 0.0])
            acc[0] += row["cpu_used_m"]
            acc[1] += row["mem_used_b"]

    dep_rows = [build_dep_row(d, dep_usage.get(
        (d["metadata"]["namespace"], d["metadata"]["name"])))
        for d in deployments.get("items", [])]

    return {"nodes": node_rows, "pods": pod_rows, "deployments": dep_rows}


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class Dashboard(tk.Tk):
    def __init__(self, folder: Path):
        super().__init__()
        self.title("☸ KR8M — Kubernetes Resource Manager")
        self.geometry("1200x760")
        self.folder = folder
        self.model = {"nodes": [], "pods": [], "deployments": []}
        self._result_q: queue.Queue = queue.Queue()
        self._busy = False
        self._analysis_frames: list = []

        # Trends / history capture state
        self._history_path = folder / trends.HISTORY_FILENAME
        self._history = trends.load_samples(self._history_path)
        self._cap_running = False
        self._cap_after_id = None
        self._cap_q: queue.Queue = queue.Queue()
        self._cap_busy = False

        # --- Row 1: live cluster (kubectl on your PATH) -----------------------
        live = ttk.Frame(self, padding=(6, 6, 6, 2))
        live.pack(fill="x")
        ttk.Label(live, text="Cluster context:").pack(side="left")
        self.context_var = tk.StringVar()
        self.context_box = ttk.Combobox(live, textvariable=self.context_var,
                                         width=42, state="readonly")
        self.context_box.pack(side="left", padx=4)
        self.refresh_btn = ttk.Button(live, text="⟳ Refresh from cluster",
                                      command=self.refresh_from_cluster)
        self.refresh_btn.pack(side="left", padx=4)
        ttk.Button(live, text="Reload contexts",
                   command=self.load_contexts).pack(side="left")
        self.cache_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(live, text="also save JSON to data folder",
                        variable=self.cache_var).pack(side="left", padx=8)

        # --- Row 2: offline (previously-collected JSON files) -----------------
        off = ttk.Frame(self, padding=(6, 2, 6, 2))
        off.pack(fill="x")
        ttk.Label(off, text="Data folder:").pack(side="left")
        self.path_var = tk.StringVar(value=str(folder))
        ttk.Entry(off, textvariable=self.path_var, width=60).pack(side="left", padx=4)
        ttk.Button(off, text="Browse…", command=self.browse).pack(side="left")
        ttk.Button(off, text="Load files", command=self.reload).pack(side="left", padx=4)

        # --- Row 3: status ----------------------------------------------------
        bar = ttk.Frame(self, padding=(6, 2, 6, 4))
        bar.pack(fill="x")
        self.status = ttk.Label(bar, text="")
        self.status.pack(side="left")

        # --- Body: collapsible sidebar (hamburger) + notebook content -------
        # The notebook keeps driving tab content, but its top tab strip is
        # hidden — navigation lives in the left sidebar instead.
        style = ttk.Style(self)
        style.layout("TNotebook.Tab", [])  # hide the built-in tab headers

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=6, pady=6)

        self._sidebar_open = True
        self.sidebar = ttk.Frame(body, padding=(4, 4))
        self.sidebar.pack(side="left", fill="y")

        # Hamburger toggle sits at the top of the sidebar.
        top = ttk.Frame(self.sidebar)
        top.pack(fill="x")
        self.hamburger_btn = ttk.Button(top, text="☰", width=3,
                                        command=self._toggle_sidebar)
        self.hamburger_btn.pack(side="left")
        self._sidebar_title = ttk.Label(top, text="KR8M", font=("", 10, "bold"))
        self._sidebar_title.pack(side="left", padx=6)

        # Container that holds one button per notebook tab.
        self._nav = ttk.Frame(self.sidebar)
        self._nav.pack(fill="both", expand=True, pady=(6, 0))
        self._nav_buttons = {}  # tab-id -> ttk.Button

        self.nb = ttk.Notebook(body)
        self.nb.pack(side="left", fill="both", expand=True)
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # Persistent Trends tab (never torn down by data reloads).
        self._build_trends_tab()

        self.load_contexts()
        # Always start empty — never auto-load a cached snapshot. The user picks a
        # context and refreshes live, or explicitly clicks "Load files" for a snapshot.
        cached = " (cached snapshot available — “Load files”)" if (folder / "pods.json").exists() else ""
        if kubectl_collect.kubectl_path():
            self.status.config(text="Pick a context and hit “Refresh from cluster”." + cached)
        else:
            self.status.config(text="⚠ kubectl not found on PATH — load JSON files, "
                                     "or install kubectl / set the KUBECTL env var." + cached)
        # Show the (empty) data tabs up front so all tabs are visible before a refresh.
        self._rebuild_analysis_tabs()
        # Open on Nodes, not the (first-created) Trends tab.
        if self._analysis_frames:
            self.nb.select(self._analysis_frames[0])

    # -- sidebar navigation ------------------------------------------------
    def _rebuild_sidebar(self):
        """Sync the sidebar buttons to the notebook's current tabs."""
        for btn in self._nav_buttons.values():
            btn.destroy()
        self._nav_buttons = {}
        current = self.nb.select()
        for tab_id in self.nb.tabs():
            text = self.nb.tab(tab_id, "text")
            btn = ttk.Button(self._nav, text=text,
                             command=lambda t=tab_id: self.nb.select(t))
            btn.pack(fill="x", pady=1)
            self._nav_buttons[tab_id] = btn
        if current:
            self._highlight_active(current)

    def _highlight_active(self, tab_id):
        for tid, btn in self._nav_buttons.items():
            btn.state(["pressed"] if str(tid) == str(tab_id) else ["!pressed"])

    def _on_tab_changed(self, _event=None):
        sel = self.nb.select()
        if sel:
            self._highlight_active(sel)

    def _toggle_sidebar(self):
        self._sidebar_open = not self._sidebar_open
        if self._sidebar_open:
            self._nav.pack(fill="both", expand=True, pady=(6, 0))
            self._sidebar_title.pack(side="left", padx=6)
        else:
            self._nav.pack_forget()
            self._sidebar_title.pack_forget()

    # -- live cluster ------------------------------------------------------
    def load_contexts(self):
        contexts, current = kubectl_collect.list_contexts()
        self.context_box["values"] = contexts
        if current and current in contexts:
            self.context_var.set(current)
        elif contexts:
            self.context_var.set(contexts[0])
        if not contexts:
            self.refresh_btn.state(["disabled"])
            if kubectl_collect.kubectl_path() is None:
                self.context_box.set("(kubectl not on PATH)")
        else:
            self.refresh_btn.state(["!disabled"])

    def refresh_from_cluster(self):
        if self._busy:
            return
        self._busy = True
        ctx = self.context_var.get().strip()
        self.refresh_btn.state(["disabled"])
        self.status.config(text=f"⏳ Running kubectl against “{ctx or 'current context'}”…")

        def work():
            try:
                raw = kubectl_collect.collect(ctx)
                self._result_q.put(("ok", raw))
            except Exception as e:  # noqa: BLE001 — surfaced to the user
                self._result_q.put(("err", e))

        threading.Thread(target=work, daemon=True).start()
        self.after(100, self._poll_collect)

    def _poll_collect(self):
        try:
            kind, payload = self._result_q.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_collect)
            return

        self._busy = False
        self.refresh_btn.state(["!disabled"])
        if kind == "err":
            self.status.config(text="⚠ kubectl failed")
            messagebox.showerror("kubectl error", str(payload))
            return

        raw = payload
        if self.cache_var.get():
            self._save_raw(raw)
        self.model = build_model_from_raw(raw)
        self._render(source=f"context “{self.context_var.get()}”")

    def _save_raw(self, raw: dict):
        folder = Path(self.path_var.get())
        folder.mkdir(parents=True, exist_ok=True)
        names = {
            "deployments": "deployments.json", "replicasets": "replicasets.json",
            "pods": "pods.json", "nodes": "nodes.json",
            "pod_metrics": "pod-metrics.json", "node_metrics": "node-metrics.json",
        }
        for key, fname in names.items():
            (folder / fname).write_text(json.dumps(raw.get(key) or {}), encoding="utf-8")

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.path_var.get() or ".")
        if d:
            self.path_var.set(d)
            self.reload()

    def reload(self):
        folder = Path(self.path_var.get())
        if not (folder / "pods.json").exists():
            self.status.config(text="⚠ no pods.json in that folder — "
                                     "refresh from the cluster instead.")
            return
        self.model = build_model(folder)
        self._render(source=f"files in {folder}")

    def _render(self, source: str = ""):
        has_metrics = any(p["cpu_used_m"] is not None for p in self.model["pods"])
        self.status.config(
            text=f"{len(self.model['nodes'])} nodes · {len(self.model['deployments'])} deploys · "
                 f"{len(self.model['pods'])} pods"
                 + (f"  ·  {source}" if source else "")
                 + ("" if has_metrics else "  ·  no metrics-server (usage blank)")
        )
        self._rebuild_analysis_tabs()

    def _rebuild_analysis_tabs(self):
        """(Re)build the four data tabs; leave the persistent Trends tab intact."""
        for f in self._analysis_frames:
            self.nb.forget(f)
            f.destroy()
        self._analysis_frames = []
        self._build_overview_tab()
        self._build_nodes_tab()
        self._build_deploys_tab()
        self._build_pods_tab()
        self._build_pods_by_ns_tab()
        self._build_offenders_tab()
        for group in RESOURCE_GROUPS:
            self._build_group_tab(group)
        # Keep Trends as the last tab.
        self.nb.insert("end", self._trends_frame)
        self._trends_refresh_namespaces()
        # Rebuild the sidebar to match the (re)built set of tabs.
        self._rebuild_sidebar()
        # Always land on Overview after a (re)build, not the persistent Trends tab.
        if self._analysis_frames:
            self.nb.select(self._analysis_frames[0])

    # -- helpers -----------------------------------------------------------
    def _make_tree(self, parent, columns, widths=None):
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)

        # Toolbar above every table with a visible export button.
        tools = ttk.Frame(wrap)
        tools.pack(side="top", fill="x", pady=(0, 2))

        body = ttk.Frame(wrap)
        body.pack(side="top", fill="both", expand=True)

        tree = ttk.Treeview(body, columns=columns, show="headings")
        vs = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vs.set)
        for i, c in enumerate(columns):
            tree.heading(c, text=c, command=lambda col=c, t=tree: self._sort(t, col, False))
            tree.column(c, width=(widths[i] if widths else 120), anchor="w")
        tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        tree.tag_configure("warn", background="#5a1e1e")

        ttk.Button(tools, text="⬇ Export CSV",
                   command=lambda t=tree: self._export_tree_csv(t)).pack(side="right")

        # Right-click anywhere in the table also exports it to CSV.
        menu = tk.Menu(tree, tearoff=0)
        menu.add_command(label="Export to CSV…",
                         command=lambda t=tree: self._export_tree_csv(t))

        def popup(event, t=tree, m=menu):
            m.tk_popup(event.x_root, event.y_root)

        tree.bind("<Button-3>", popup)      # right-click (Win/Linux)
        tree.bind("<Button-2>", popup)      # middle/right on some macs
        return tree

    def _export_tree_csv(self, tree):
        """Write a Treeview's current columns and rows to a CSV file the user picks.

        Exports what's on screen: current sort order and any selection is ignored
        (all rows are written). Uses only the stdlib csv module.
        """
        columns = list(tree["columns"])
        if not columns:
            return
        rows = tree.get_children("")
        if not rows:
            messagebox.showinfo("Export to CSV", "This table is empty — nothing to export.")
            return

        path = filedialog.asksaveasfilename(
            title="Export table to CSV",
            defaultextension=".csv",
            initialfile=f"k8s-export-{time.strftime('%Y%m%d-%H%M%S')}.csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return

        headers = [tree.heading(c, "text") or c for c in columns]
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.writer(fh)
                writer.writerow(headers)
                for iid in rows:
                    writer.writerow([tree.set(iid, c) for c in columns])
        except OSError as e:
            messagebox.showerror("Export failed", str(e))
            return
        self.status.config(text=f"✓ exported {len(rows)} rows → {path}")

    def _sort(self, tree, col, desc):
        data = [(tree.set(k, col), k) for k in tree.get_children("")]

        def key(t):
            v = t[0]
            try:
                return float(v.replace("%", "").replace("m", "").replace("Gi", "")
                             .replace("Mi", "").replace("Ki", "").replace(" cores", ""))
            except ValueError:
                return v.lower()
        data.sort(key=key, reverse=desc)
        for i, (_, k) in enumerate(data):
            tree.move(k, "", i)
        tree.heading(col, command=lambda: self._sort(tree, col, not desc))

    def _run_bulk(self, items, action_fn, verb, status_lbl, tree=None,
                  remove_on_success=False):
        """
        Apply action_fn(ktype, name, namespace) to each (iid, ktype, name, ns) in
        items on a background thread, then report on the UI thread. On success,
        optionally drop the affected rows from `tree` (the in-memory model is a
        snapshot; a full cluster refresh reconciles the rest).
        """
        status_lbl.config(text=f"⏳ {verb} {len(items)}…")
        results = {"ok": [], "err": []}

        def work():
            for iid, kt, name, ns in items:
                try:
                    action_fn(kt, name, ns)
                    results["ok"].append(iid)
                except Exception as e:  # noqa: BLE001
                    results["err"].append((name, e))
            self.after(0, finish)

        def finish():
            if remove_on_success and tree is not None:
                for iid in results["ok"]:
                    if tree.exists(iid):
                        tree.delete(iid)
            msg = f"{verb}: {len(results['ok'])} ok"
            if results["err"]:
                msg += f", {len(results['err'])} failed"
                messagebox.showerror(
                    "Some actions failed",
                    "\n".join(f"{n}: {e}" for n, e in results["err"]))
            msg += "  ·  refresh from cluster to fully update"
            status_lbl.config(text=msg)

        threading.Thread(target=work, daemon=True).start()

    def _scoped_refresh(self, fetch_fn, apply_fn, status_lbl):
        """
        Re-fetch ONE tab's own resource live (fetch_fn, off-thread), then hand the
        result to apply_fn on the UI thread. Unlike "Refresh from cluster", this
        touches only the kind/scope the tab shows.
        """
        status_lbl.config(text="⏳ refreshing…")

        def work():
            try:
                data = fetch_fn()
            except Exception as e:  # noqa: BLE001
                self.after(0, lambda err=e: status_lbl.config(text=f"⚠ {err}"))
                return
            self.after(0, lambda: done(data))

        def done(data):
            # Clear the "refreshing…" state first. apply_fn may override this
            # with its own message (e.g. a row count); tabs that don't will keep
            # this success line instead of a stuck spinner.
            status_lbl.config(text=f"✓ updated {time.strftime('%H:%M:%S')}")
            try:
                apply_fn(data)
            except Exception as e:  # noqa: BLE001
                status_lbl.config(text=f"⚠ {e}")

        threading.Thread(target=work, daemon=True).start()

    def _node_usage_map(self, context):
        nu = {}
        for it in kubectl_collect.node_metrics(context).get("items", []):
            nu[it["metadata"]["name"]] = (
                cpu_to_milli(it.get("usage", {}).get("cpu")),
                mem_to_bytes(it.get("usage", {}).get("memory")))
        return nu

    # -- tabs --------------------------------------------------------------
    def _build_overview_tab(self):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="🏠 Overview")
        self._analysis_frames.append(frame)

        # Overview summarizes every kind, so its refresh is the full cluster pull.
        topbar = ttk.Frame(frame, padding=4)
        topbar.pack(fill="x")
        ttk.Button(topbar, text="⟳ Refresh from cluster",
                   command=self.refresh_from_cluster).pack(side="left")
        ttk.Label(topbar, text="(whole-cluster summary — refreshes everything)"
                  ).pack(side="left", padx=8)

        pods = self.model["pods"]
        nodes = self.model["nodes"]
        deps = self.model["deployments"]
        has_metrics = any(p["cpu_used_m"] is not None for p in pods)

        if not pods and not nodes:
            ttk.Label(frame, padding=20, font=("", 11), text=(
                "No data yet.\n\nPick a context above and hit “⟳ Refresh from cluster”, "
                "or “Load files” for a cached snapshot.")).pack(anchor="w")
            return

        # cluster-wide roll-ups
        cpu_alloc = sum(n["cpu_alloc_m"] for n in nodes)
        mem_alloc = sum(n["mem_alloc_b"] for n in nodes)
        cpu_req = sum(p["cpu_req_m"] for p in pods)
        mem_req = sum(p["mem_req_b"] for p in pods)
        cpu_used = sum(n["cpu_used_m"] for n in nodes if n["cpu_used_m"] is not None)
        mem_used = sum(n["mem_used_b"] for n in nodes if n["mem_used_b"] is not None)
        not_running = sum(1 for p in pods if p["phase"] not in ("Running", "Succeeded"))
        restarting = sum(1 for p in pods if p["restarts"] > 0)
        pods_no_lim = sum(1 for p in pods if p["missing_limits"])
        deps_bad = sum(1 for d in deps if d["missing"])

        # --- metric cards ---
        cards = ttk.Frame(frame, padding=(8, 10, 8, 4))
        cards.pack(fill="x")

        def card(parent, value, label, warn=False):
            box = ttk.Frame(parent, relief="solid", borderwidth=1, padding=(12, 8))
            box.pack(side="left", padx=5)
            ttk.Label(box, text=str(value), font=("", 18, "bold"),
                      foreground=("#c0392b" if warn else "")).pack()
            ttk.Label(box, text=label).pack()
            return box

        card(cards, len(nodes), "Nodes")
        card(cards, len(deps), "Deployments")
        card(cards, len(pods), "Pods")
        card(cards, not_running, "Not running", warn=not_running > 0)
        card(cards, restarting, "With restarts", warn=restarting > 0)
        card(cards, pods_no_lim, "Pods w/o limits")
        card(cards, deps_bad, "Deploys w/o req/lim")

        # --- cluster capacity ---
        cap = ttk.LabelFrame(frame, text="Cluster capacity", padding=8)
        cap.pack(fill="x", padx=8, pady=8)
        cap_cols = ("resource", "allocatable", "requested", "used")
        ctree = self._make_tree(cap, cap_cols, [140, 160, 200, 200])
        ctree.insert("", "end", values=(
            "CPU", fmt_cpu(cpu_alloc),
            f"{fmt_cpu(cpu_req)}  ({pct(cpu_req, cpu_alloc)})",
            f"{fmt_cpu(cpu_used)}  ({pct(cpu_used, cpu_alloc)})" if has_metrics else "—",
        ))
        ctree.insert("", "end", values=(
            "Memory", fmt_mem(mem_alloc),
            f"{fmt_mem(mem_req)}  ({pct(mem_req, mem_alloc)})",
            f"{fmt_mem(mem_used)}  ({pct(mem_used, mem_alloc)})" if has_metrics else "—",
        ))
        ttk.Label(cap, padding=(0, 4), text=(
            "requested = what the scheduler reserves (why nodes fill up).  "
            "used = real utilization" + ("" if has_metrics else " — no metrics-server."))
        ).pack(anchor="w")

        # --- biggest offenders preview ---
        off = ttk.LabelFrame(frame, text="Biggest memory offenders (requested − used)",
                             padding=8)
        off.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        cols = ("namespace", "pod", "mem req", "mem used", "mem WASTED")
        otree = self._make_tree(off, cols, [140, 300, 110, 110, 120])
        rows = [p for p in pods if p["mem_used_b"] is not None]
        rows.sort(key=lambda p: p["mem_req_b"] - (p["mem_used_b"] or 0), reverse=True)
        for p in rows[:8]:
            waste = p["mem_req_b"] - (p["mem_used_b"] or 0)
            otree.insert("", "end", values=(
                p["namespace"], p["pod"], fmt_mem(p["mem_req_b"]),
                fmt_mem(p["mem_used_b"]), fmt_mem(waste),
            ))
        if not rows:
            ttk.Label(off, text="No metrics-server data — usage-based ranking unavailable."
                      ).pack(anchor="w")

    def _build_nodes_tab(self):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="Nodes")
        self._analysis_frames.append(frame)
        list_frame = ttk.Frame(frame)
        list_frame.pack(fill="both", expand=True)

        ctx = lambda: self.context_var.get().strip()
        bar = ttk.Frame(list_frame, padding=4)
        bar.pack(fill="x")
        status = ttk.Label(bar, text="")

        def do_refresh():
            def fetch():
                nodes = kubectl_collect.list_items("nodes", context=ctx())
                nu = self._node_usage_map(ctx())
                return [build_node_row(n, nu.get(n["metadata"]["name"], (None, None)))
                        for n in nodes]

            def apply(rows):
                self.model["nodes"] = rows
                refill()
            self._scoped_refresh(fetch, apply, status)

        ttk.Button(bar, text="⟳ Refresh nodes", command=do_refresh).pack(side="left")
        status.pack(side="left", padx=8)

        cols = ("open", "node", "ready", "pods", "cpu alloc", "cpu req%", "cpu used%",
                "mem alloc", "mem req%", "mem used%", "⚠ why")
        tree = self._make_tree(list_frame, cols,
                               [40, 150, 70, 55, 90, 80, 80, 90, 80, 80, 200])
        tree.heading("open", text="⧉")
        tree.column("open", anchor="center", stretch=False)

        def refill():
            for r in tree.get_children(""):
                tree.delete(r)
            # aggregate pod requests per node (from the current pod model)
            agg = {}
            for p in self.model["pods"]:
                a = agg.setdefault(p["node"], {"pods": 0, "cpu": 0.0, "mem": 0.0})
                a["pods"] += 1
                a["cpu"] += p["cpu_req_m"]
                a["mem"] += p["mem_req_b"]
            for n in self.model["nodes"]:
                a = agg.get(n["node"], {"pods": 0, "cpu": 0.0, "mem": 0.0})
                req_cpu_p = a["cpu"] / n["cpu_alloc_m"] * 100 if n["cpu_alloc_m"] else 0
                req_mem_p = a["mem"] / n["mem_alloc_b"] * 100 if n["mem_alloc_b"] else 0
                why = []
                if req_cpu_p > 85:
                    why.append(f"CPU {req_cpu_p:.0f}% requested")
                if req_mem_p > 85:
                    why.append(f"mem {req_mem_p:.0f}% requested")
                if n["ready"] != "True":
                    why.append(f"NotReady ({n['ready']})")
                tree.insert("", "end", tags=("warn" if why else "",), values=(
                    "⧉", n["node"], n["ready"], a["pods"],
                    fmt_cpu(n["cpu_alloc_m"]), f"{req_cpu_p:.0f}%",
                    pct(n["cpu_used_m"], n["cpu_alloc_m"]) if n["cpu_used_m"] is not None else "—",
                    fmt_mem(n["mem_alloc_b"]), f"{req_mem_p:.0f}%",
                    pct(n["mem_used_b"], n["mem_alloc_b"]) if n["mem_used_b"] is not None else "—",
                    "; ".join(why),
                ))
        refill()

        ttk.Label(list_frame, text="Double-click a node for its details, events, and the "
                  "pods running on it — click ⧉ to open in a new window. Red = over 85% "
                  "requested (scheduler sees it as nearly full) or NotReady; see the "
                  "“⚠ why” column.", padding=4).pack(anchor="w")

        wire_row_actions(
            tree,
            lambda row: ResourceDetailWindow(self, "nodes", tree.set(row, "node"), "", ctx()),
            lambda sel: open_inline_detail(frame, list_frame, "nodes",
                                           tree.set(sel, "node"), "", ctx()))

    def _build_deploys_tab(self):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="Deployments")
        self._analysis_frames.append(frame)
        list_frame = ttk.Frame(frame)
        list_frame.pack(fill="both", expand=True)

        bar = ttk.Frame(list_frame, padding=4)
        bar.pack(fill="x")
        ttk.Label(bar, text="Namespace:").pack(side="left")
        namespaces = ["(all)"] + sorted({d["namespace"] for d in self.model["deployments"]})
        ns_var = tk.StringVar(value="(all)")
        ns_box = ttk.Combobox(bar, textvariable=ns_var, width=26, state="readonly",
                              values=namespaces)
        ns_box.pack(side="left", padx=4)
        ctx = lambda: self.context_var.get().strip()

        def do_refresh():
            ns = ns_var.get()
            allns = ns == "(all)"

            def fetch():
                items = kubectl_collect.list_items(
                    "deployments", namespace="" if allns else ns,
                    all_namespaces=allns, context=ctx())
                return (ns, [build_dep_row(d) for d in items])

            def apply(data):
                scope, rows = data
                if scope == "(all)":
                    self.model["deployments"] = rows
                else:
                    self.model["deployments"] = [
                        d for d in self.model["deployments"] if d["namespace"] != scope] + rows
                refill()
            self._scoped_refresh(fetch, apply, count_lbl)

        ttk.Button(bar, text="⟳ Refresh", command=do_refresh).pack(side="left", padx=6)
        count_lbl = ttk.Label(bar, text="")
        count_lbl.pack(side="left", padx=8)

        cols = ("sel", "open", "namespace", "deployment", "replicas", "cpu req/pod",
                "cpu lim/pod", "mem req/pod", "mem lim/pod", "cpu req TOTAL",
                "mem req TOTAL", "missing")
        tree = self._make_tree(list_frame, cols,
                               [34, 40, 110, 200, 80, 90, 90, 90, 90, 95, 95, 160])
        tree.heading("sel", text="☑")
        tree.heading("open", text="⧉")
        tree.column("sel", anchor="center", stretch=False)
        tree.column("open", anchor="center", stretch=False)

        def refill(*_):
            ms.reset()
            for r in tree.get_children(""):
                tree.delete(r)
            ns = ns_var.get()
            rows = [d for d in self.model["deployments"]
                    if ns == "(all)" or d["namespace"] == ns]
            rows.sort(key=lambda x: x["mem_req_total_b"], reverse=True)
            for d in rows:
                tree.insert("", "end", values=(
                    "☐", "⧉", d["namespace"], d["deployment"],
                    f'{d["replicas"]} ({d["ready"]} ready)',
                    fmt_cpu(d["cpu_req_m"]), fmt_cpu(d["cpu_lim_m"]) if d["cpu_lim_m"] else "none",
                    fmt_mem(d["mem_req_b"]), fmt_mem(d["mem_lim_b"]) if d["mem_lim_b"] else "none",
                    fmt_cpu(d["cpu_req_total_m"]), fmt_mem(d["mem_req_total_b"]), d["missing"],
                ))
            count_lbl.config(text=f"{len(rows)} deployments")

        ctx = lambda: self.context_var.get().strip()
        ms = MultiSelect(
            tree,
            lambda row: ResourceDetailWindow(self, "deployments", tree.set(row, "deployment"),
                                             tree.set(row, "namespace"), ctx()),
            lambda sel: open_inline_detail(frame, list_frame, "deployments",
                                           tree.set(sel, "deployment"),
                                           tree.set(sel, "namespace"), ctx()))
        ns_box.bind("<<ComboboxSelected>>", refill)
        refill()

        # -- bulk actions --
        act = ttk.Frame(list_frame, padding=(4, 0, 4, 4))
        act.pack(fill="x")
        act_status = ttk.Label(act, text="")

        def targets():
            return [(i, "deployments", tree.set(i, "deployment"), tree.set(i, "namespace"))
                    for i in ms.targets()]

        def do_restart():
            items = targets()
            if not items:
                messagebox.showinfo("Nothing selected", "Check rows, or select one.")
                return
            if not messagebox.askyesno(
                    "Rollout restart", f"Rolling-restart {len(items)} deployment(s)?"):
                return
            self._run_bulk(items, lambda kt, n, ns: kubectl_collect.rollout_restart(
                kt, n, namespace=ns, context=ctx()), "Restarted", act_status)

        def do_delete():
            items = targets()
            if not items:
                messagebox.showinfo("Nothing selected", "Check rows, or select one.")
                return
            names = ", ".join(n for _, _, n, _ in items[:5]) + ("…" if len(items) > 5 else "")
            if not messagebox.askyesno(
                    "Delete", f"Delete {len(items)} deployment(s)?\n\n{names}\n\n"
                    "This cannot be undone.", icon="warning"):
                return
            self._run_bulk(items, lambda kt, n, ns: kubectl_collect.delete(
                kt, n, namespace=ns, context=ctx()), "Deleted", act_status,
                tree=tree, remove_on_success=True)

        ttk.Button(act, text="Rollout restart", command=do_restart).pack(side="left")
        ttk.Button(act, text="Delete…", command=do_delete).pack(side="left", padx=4)
        act_status.pack(side="left", padx=8)

        ttk.Label(list_frame, text="Tick ☑ to select multiple (or just click a row), then "
                  "use the buttons. Double-click a deployment for its YAML, events, and pods; "
                  "click ⧉ for a new window.", padding=4).pack(anchor="w")

    def _build_pods_tab(self):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="Pods")
        self._analysis_frames.append(frame)
        list_frame = ttk.Frame(frame)
        list_frame.pack(fill="both", expand=True)
        bar = ttk.Frame(list_frame, padding=4)
        bar.pack(fill="x")
        ttk.Label(bar, text="Filter:").pack(side="left")
        filt = tk.StringVar()
        ttk.Entry(bar, textvariable=filt, width=40).pack(side="left", padx=4)
        ctx = lambda: self.context_var.get().strip()
        refresh_status = ttk.Label(bar, text="")

        def do_refresh():
            def fetch():
                items = kubectl_collect.list_items("pods", all_namespaces=True, context=ctx())
                usage = usage_map_from_metrics(kubectl_collect.pod_metrics(ctx()))
                return [build_pod_row(
                    it, usage.get((it["metadata"]["namespace"], it["metadata"]["name"]),
                                  (None, None))) for it in items]

            def apply(rows):
                self.model["pods"] = rows
                refill()
            self._scoped_refresh(fetch, apply, refresh_status)

        ttk.Button(bar, text="⟳ Refresh pods", command=do_refresh).pack(side="left", padx=6)
        refresh_status.pack(side="left", padx=6)
        cols = ("sel", "open", "namespace", "pod", "node", "phase", "cpu req", "cpu used",
                "mem req", "mem used", "no lim", "⚠ why")
        tree = self._make_tree(list_frame, cols,
                               [34, 40, 110, 240, 150, 70, 80, 80, 80, 80, 55, 200])
        tree.heading("sel", text="☑")
        tree.heading("open", text="⧉")
        tree.column("sel", anchor="center", stretch=False)
        tree.column("open", anchor="center", stretch=False)

        def refill(*_):
            ms.reset()
            for r in tree.get_children(""):
                tree.delete(r)
            q = filt.get().lower()
            rows = sorted(self.model["pods"], key=lambda p: p["mem_req_b"], reverse=True)
            for p in rows:
                hay = f'{p["namespace"]} {p["pod"]} {p["node"]}'.lower()
                if q and q not in hay:
                    continue
                tree.insert("", "end", tags=("warn" if p["status_note"] else "",), values=(
                    "☐", "⧉", p["namespace"], p["pod"], p["node"], p["phase"],
                    fmt_cpu(p["cpu_req_m"]), fmt_cpu(p["cpu_used_m"]),
                    fmt_mem(p["mem_req_b"]), fmt_mem(p["mem_used_b"]),
                    "⚠" if p["missing_limits"] else "", p["status_note"],
                ))
        ctx = lambda: self.context_var.get().strip()
        ms = MultiSelect(
            tree,
            lambda row: ResourceDetailWindow(self, "pod", tree.set(row, "pod"),
                                             tree.set(row, "namespace"), ctx()),
            lambda sel: open_inline_detail(frame, list_frame, "pod",
                                           tree.set(sel, "pod"),
                                           tree.set(sel, "namespace"), ctx()))
        filt.trace_add("write", refill)
        refill()

        act = ttk.Frame(list_frame, padding=(4, 0, 4, 4))
        act.pack(fill="x")
        act_status = ttk.Label(act, text="")

        def do_delete():
            items = [(i, "pod", tree.set(i, "pod"), tree.set(i, "namespace"))
                     for i in ms.targets()]
            if not items:
                messagebox.showinfo("Nothing selected", "Check rows, or select one.")
                return
            names = ", ".join(n for _, _, n, _ in items[:5]) + ("…" if len(items) > 5 else "")
            if not messagebox.askyesno(
                    "Delete", f"Delete {len(items)} pod(s)?\n\n{names}\n\n"
                    "Managed pods will be recreated by their controller.",
                    icon="warning"):
                return
            self._run_bulk(items, lambda kt, n, ns: kubectl_collect.delete(
                kt, n, namespace=ns, context=ctx()), "Deleted", act_status,
                tree=tree, remove_on_success=True)

        ttk.Button(act, text="Delete…", command=do_delete).pack(side="left")
        act_status.pack(side="left", padx=8)

        ttk.Label(list_frame, text="Tick ☑ to select multiple (or just click a row), then "
                  "Delete. Double-click a pod for details / logs / shell; click ⧉ for a "
                  "new window. Red row = unhealthy; the “⚠ why” column says why.",
                  padding=4).pack(anchor="w")

    def _build_pods_by_ns_tab(self):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="Pods by namespace")
        self._analysis_frames.append(frame)
        self._pods_ns_frame = frame
        self._pods_ns_detail = None
        # The list lives in its own sub-frame so we can hide it and swap in an
        # in-place detail view, then bring it back.
        self._pods_ns_list = ttk.Frame(frame)
        self._pods_ns_list.pack(fill="both", expand=True)
        self._build_pods_ns_list(self._pods_ns_list)

    def _build_pods_ns_list(self, parent):
        bar = ttk.Frame(parent, padding=4)
        bar.pack(fill="x")
        ttk.Label(bar, text="Namespace:").pack(side="left")
        namespaces = sorted({p["namespace"] for p in self.model["pods"]})
        ns_var = tk.StringVar(value=namespaces[0] if namespaces else "")
        ns_box = ttk.Combobox(bar, textvariable=ns_var, width=28, state="readonly",
                              values=namespaces)
        ns_box.pack(side="left", padx=4)
        ctx = lambda: self.context_var.get().strip()

        def do_refresh():
            ns = ns_var.get()
            if not ns:
                return

            def fetch():
                items = kubectl_collect.list_items("pods", namespace=ns, context=ctx())
                return (ns, [build_pod_row(it) for it in items])

            def apply(data):
                scope, rows = data
                # Replace only this namespace's pods in the model, then redraw.
                self.model["pods"] = [
                    p for p in self.model["pods"] if p["namespace"] != scope] + rows
                refill()
            self._scoped_refresh(fetch, apply, count_lbl)

        ttk.Button(bar, text="⟳ Refresh namespace",
                   command=do_refresh).pack(side="left", padx=6)
        count_lbl = ttk.Label(bar, text="")
        count_lbl.pack(side="left", padx=8)

        # Leading "sel" (☐/☑) + "open" (⧉) columns drive multi-select and the
        # single-click new-window shortcut.
        cols = ("sel", "open", "pod", "ready", "phase", "restarts", "age",
                "node", "pod ip", "⚠ why")
        tree = self._make_tree(parent, cols,
                               [34, 44, 260, 60, 90, 70, 70, 150, 120, 200])
        tree.heading("sel", text="☑")
        tree.heading("open", text="⧉")
        tree.column("sel", anchor="center", stretch=False)
        tree.column("open", anchor="center", stretch=False)

        def refill(*_):
            ms.reset()
            for r in tree.get_children(""):
                tree.delete(r)
            ns = ns_var.get()
            rows = sorted((p for p in self.model["pods"] if p["namespace"] == ns),
                          key=lambda p: p["pod"])
            for p in rows:
                tree.insert("", "end", tags=("warn" if p["status_note"] else "",), values=(
                    "☐", "⧉", p["pod"], p["ready"], p["phase"], p["restarts"],
                    p["age"], p["node"], p["pod_ip"], p["status_note"],
                ))
            count_lbl.config(text=f"{len(rows)} pods")

        ms = MultiSelect(
            tree,
            lambda row: self._open_pod_window(tree.set(row, "pod"), ns_var.get()),
            lambda sel: self._open_pod_inplace(tree.set(sel, "pod"), ns_var.get()))
        ns_box.bind("<<ComboboxSelected>>", refill)
        refill()

        act = ttk.Frame(parent, padding=(4, 0, 4, 4))
        act.pack(fill="x")
        act_status = ttk.Label(act, text="")
        ctx = lambda: self.context_var.get().strip()

        def do_delete():
            ns = ns_var.get()
            items = [(i, "pod", tree.set(i, "pod"), ns) for i in ms.targets()]
            if not items:
                messagebox.showinfo("Nothing selected", "Check rows, or select one.")
                return
            names = ", ".join(n for _, _, n, _ in items[:5]) + ("…" if len(items) > 5 else "")
            if not messagebox.askyesno(
                    "Delete", f"Delete {len(items)} pod(s) in {ns}?\n\n{names}\n\n"
                    "Managed pods will be recreated by their controller.",
                    icon="warning"):
                return
            self._run_bulk(items, lambda kt, n, nns: kubectl_collect.delete(
                kt, n, namespace=nns, context=ctx()), "Deleted", act_status,
                tree=tree, remove_on_success=True)

        ttk.Button(act, text="Delete…", command=do_delete).pack(side="left")
        act_status.pack(side="left", padx=8)

        ttk.Label(parent, padding=4, text=(
            "Tick ☑ to select multiple (or just click a row), then Delete. "
            "Double-click a pod to open it here (describe / logs / shell); click ⧉ for a "
            "separate window. Red row = unhealthy; the “⚠ why” column says why.")
            ).pack(anchor="w")

    def _open_pod_window(self, pod, namespace):
        ResourceDetailWindow(self, "pod", pod, namespace,
                             self.context_var.get().strip())

    def _open_pod_inplace(self, pod, namespace):
        if self._pods_ns_detail is not None:
            self._close_pod_inplace()
        self._pods_ns_list.pack_forget()
        self._pods_ns_detail = ResourceDetailView(
            self._pods_ns_frame, "pod", pod, namespace,
            self.context_var.get().strip(), on_back=self._close_pod_inplace)
        self._pods_ns_detail.pack(fill="both", expand=True)

    def _close_pod_inplace(self):
        if self._pods_ns_detail is not None:
            self._pods_ns_detail.teardown()
            self._pods_ns_detail.destroy()
            self._pods_ns_detail = None
        self._pods_ns_list.pack(fill="both", expand=True)

    def _build_group_tab(self, group):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text=group)
        self._analysis_frames.append(frame)
        browser = ResourceBrowser(
            frame, lambda: self.context_var.get().strip(), RESOURCE_GROUPS[group])
        browser.pack(fill="both", expand=True)

    def _build_offenders_tab(self):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="Resource Management")
        self._analysis_frames.append(frame)
        ctx = lambda: self.context_var.get().strip()

        bar = ttk.Frame(frame, padding=4)
        bar.pack(fill="x")
        status = ttk.Label(bar, text="")

        def do_refresh():
            def fetch():
                items = kubectl_collect.list_items("pods", all_namespaces=True, context=ctx())
                usage = usage_map_from_metrics(kubectl_collect.pod_metrics(ctx()))
                return [build_pod_row(
                    it, usage.get((it["metadata"]["namespace"], it["metadata"]["name"]),
                                  (None, None))) for it in items]

            def apply(rows):
                self.model["pods"] = rows
                refill()
            self._scoped_refresh(fetch, apply, status)

        ttk.Button(bar, text="⟳ Refresh pods", command=do_refresh).pack(side="left")

        ttk.Label(bar, text="Namespace:").pack(side="left", padx=(12, 2))
        ns_filter = tk.StringVar(value="All namespaces")
        ns_box = ttk.Combobox(bar, textvariable=ns_filter, width=24, state="readonly",
                              values=["All namespaces"])
        ns_box.pack(side="left")
        ns_box.bind("<<ComboboxSelected>>", lambda _e: refill())

        status.pack(side="left", padx=8)

        ttk.Label(frame, padding=6, text=(
            "Pods reserving far more memory than they use — the over-provisioning that "
            "forces new nodes. Scheduling uses requests, not usage.")).pack(anchor="w")
        cols = ("namespace", "pod", "node", "mem req", "mem used", "mem WASTED",
                "cpu req", "cpu used")
        tree = self._make_tree(frame, cols, [110, 240, 150, 90, 90, 100, 80, 80])
        empty = ttk.Label(frame, padding=6, text="No metrics-server data available.")

        def refill():
            for r in tree.get_children(""):
                tree.delete(r)
            metered = [p for p in self.model["pods"] if p["mem_used_b"] is not None]

            # Keep the namespace picker in sync with the current data, preserving
            # the user's selection when it still exists.
            namespaces = sorted({p["namespace"] for p in metered})
            ns_box["values"] = ["All namespaces"] + namespaces
            if ns_filter.get() not in ns_box["values"]:
                ns_filter.set("All namespaces")

            selected = ns_filter.get()
            rows = ([p for p in metered if p["namespace"] == selected]
                    if selected != "All namespaces" else metered)
            rows.sort(key=lambda p: p["mem_req_b"] - (p["mem_used_b"] or 0), reverse=True)
            for p in rows[:40]:
                waste = p["mem_req_b"] - (p["mem_used_b"] or 0)
                tree.insert("", "end", values=(
                    p["namespace"], p["pod"], p["node"], fmt_mem(p["mem_req_b"]),
                    fmt_mem(p["mem_used_b"]), fmt_mem(waste),
                    fmt_cpu(p["cpu_req_m"]), fmt_cpu(p["cpu_used_m"]),
                ))
            empty.pack_forget()
            if not rows:
                empty.pack(anchor="w")
        refill()

    # -- Trends tab (time series) ------------------------------------------
    def _build_trends_tab(self):
        frame = ttk.Frame(self.nb)
        self._trends_frame = frame
        self.nb.add(frame, text="Trends")

        ttk.Label(frame, padding=(6, 6, 6, 0), text=(
            "Leave the app running to record deployment resource use over time, then "
            "graph it. Solid line = actual usage, dashed = requested (reserved). "
            "One line per deployment in the selected namespace.")).pack(anchor="w")

        # controls
        ctl = ttk.Frame(frame, padding=6)
        ctl.pack(fill="x")
        ttk.Label(ctl, text="Interval:").pack(side="left")
        self.trends_interval = tk.StringVar(value="15 min")
        ttk.Combobox(ctl, textvariable=self.trends_interval, width=8, state="readonly",
                     values=list(_INTERVALS)).pack(side="left", padx=(2, 10))
        self.trends_cap_btn = ttk.Button(ctl, text="▶ Start capture",
                                         command=self._trends_toggle_capture)
        self.trends_cap_btn.pack(side="left")

        ttk.Separator(ctl, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Label(ctl, text="Namespace:").pack(side="left")
        self.trends_ns = tk.StringVar()
        self.trends_ns_box = ttk.Combobox(ctl, textvariable=self.trends_ns, width=22,
                                          state="readonly")
        self.trends_ns_box.pack(side="left", padx=2)
        self.trends_ns_box.bind("<<ComboboxSelected>>", lambda e: self._trends_redraw())

        ttk.Label(ctl, text="Metric:").pack(side="left", padx=(10, 0))
        self.trends_metric = tk.StringVar(value="Memory")
        mb = ttk.Combobox(ctl, textvariable=self.trends_metric, width=8, state="readonly",
                          values=["Memory", "CPU"])
        mb.pack(side="left", padx=2)
        mb.bind("<<ComboboxSelected>>", lambda e: self._trends_redraw())

        self.trends_show_used = tk.BooleanVar(value=True)
        self.trends_show_req = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctl, text="usage", variable=self.trends_show_used,
                        command=self._trends_redraw).pack(side="left", padx=(10, 2))
        ttk.Checkbutton(ctl, text="request", variable=self.trends_show_req,
                        command=self._trends_redraw).pack(side="left")

        self.trends_status = ttk.Label(frame, padding=(6, 0), text="")
        self.trends_status.pack(anchor="w")

        self.trends_chart = trends.LineChart(frame, height=420)
        self.trends_chart.pack(fill="both", expand=True, padx=6, pady=6)

        self._trends_refresh_namespaces()
        self._trends_update_status()
        self._trends_redraw()

    def _trends_toggle_capture(self):
        if self._cap_running:
            self._cap_running = False
            if self._cap_after_id is not None:
                self.after_cancel(self._cap_after_id)
                self._cap_after_id = None
            self.trends_cap_btn.config(text="▶ Start capture")
            self._trends_update_status()
            return
        if kubectl_collect.kubectl_path() is None:
            messagebox.showerror("kubectl not found",
                                 "Capture needs kubectl on your PATH (or the KUBECTL env var).")
            return
        self._cap_running = True
        self.trends_cap_btn.config(text="■ Stop capture")
        self._capture_tick()  # take one sample immediately, then on the interval

    def _capture_tick(self):
        if not self._cap_running or self._cap_busy:
            return
        self._cap_busy = True
        ctx = self.context_var.get().strip()
        self._trends_update_status(extra="  ·  ⏳ capturing…")

        def work():
            try:
                raw = kubectl_collect.collect(ctx)
                self._cap_q.put(("ok", raw))
            except Exception as e:  # noqa: BLE001
                self._cap_q.put(("err", e))

        threading.Thread(target=work, daemon=True).start()
        self.after(100, self._poll_capture)

    def _poll_capture(self):
        try:
            kind, payload = self._cap_q.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_capture)
            return

        self._cap_busy = False
        if kind == "ok":
            model = build_model_from_raw(payload)
            recs = trends.samples_from_model(model)
            trends.append_samples(self._history_path, recs)
            self._history.extend(recs)
            self._trends_refresh_namespaces()
            self._trends_redraw()
            self._trends_update_status()
        else:
            self._trends_update_status(extra=f"  ·  ⚠ {payload}")

        # schedule the next tick if still running
        if self._cap_running:
            ms = _INTERVALS[self.trends_interval.get()]
            self._cap_after_id = self.after(ms, self._capture_tick)

    def _trends_update_status(self, extra: str = ""):
        n = len(self._history)
        times = sorted({r["ts"] for r in self._history})
        span = ""
        if times:
            span = (f" spanning {time.strftime('%H:%M', time.localtime(times[0]))}"
                    f"–{time.strftime('%H:%M', time.localtime(times[-1]))}")
        state = "capturing" if self._cap_running else "stopped"
        self.trends_status.config(
            text=f"{len(times)} samples · {n} records{span} · {state}{extra}")

    def _trends_refresh_namespaces(self):
        namespaces = trends.namespaces_in(self._history)
        self.trends_ns_box["values"] = namespaces
        if namespaces and self.trends_ns.get() not in namespaces:
            self.trends_ns.set(namespaces[0])

    def _trends_redraw(self):
        if not hasattr(self, "trends_chart"):
            return
        ns = self.trends_ns.get()
        metric = self.trends_metric.get()
        used_key = "mem_used_b" if metric == "Memory" else "cpu_used_m"
        req_key = "mem_req_b" if metric == "Memory" else "cpu_req_m"
        if metric == "Memory":
            scale = 1024 ** 2  # -> MiB
            y_fmt = lambda v: f"{v/1024:.1f}Gi" if v >= 1024 else f"{v:.0f}Mi"
        else:
            scale = 1.0  # millicores
            y_fmt = lambda v: f"{v/1000:.2f}c" if v >= 1000 else f"{v:.0f}m"

        recs = [r for r in self._history if r["ns"] == ns]
        deps = sorted({r["dep"] for r in recs})
        series = []
        for i, dep in enumerate(deps):
            color = trends.color_for(i)
            drecs = sorted((r for r in recs if r["dep"] == dep), key=lambda r: r["ts"])
            if self.trends_show_used.get():
                pts = [(r["ts"], r[used_key] / scale) for r in drecs if r.get(used_key) is not None]
                if pts:
                    series.append({"label": dep, "color": color, "dash": False, "points": pts})
            if self.trends_show_req.get():
                pts = [(r["ts"], r[req_key] / scale) for r in drecs if r.get(req_key) is not None]
                if pts:
                    series.append({"label": dep, "color": color, "dash": True, "points": pts})

        msg = ("No data yet — pick a context above and hit “Start capture”."
               if not self._history else f"No records for namespace “{ns}”.")
        self.trends_chart.set_series(series, y_fmt=y_fmt, empty_msg=msg)


# kubectl types whose detail view gets a live "Pods" tab (what it runs/deploys).
_HAS_PODS = {"deployments", "deployment", "statefulsets", "statefulset",
             "daemonsets", "daemonset", "replicasets", "replicaset",
             "jobs", "job", "nodes", "node"}
_DIRECT_OWNER = {"statefulsets": "StatefulSet", "statefulset": "StatefulSet",
                 "daemonsets": "DaemonSet", "daemonset": "DaemonSet",
                 "replicasets": "ReplicaSet", "replicaset": "ReplicaSet",
                 "jobs": "Job", "job": "Job"}


def pods_for(ktype, name, namespace, context):
    """Live-query the pods a controller owns, or the pods running on a node."""
    if ktype in ("nodes", "node"):
        pods = kubectl_collect.list_items("pods", all_namespaces=True, context=context)
        return [p for p in pods if p.get("spec", {}).get("nodeName") == name]
    if ktype in ("deployments", "deployment"):
        # Deployment -> its ReplicaSets -> their Pods.
        rss = kubectl_collect.list_items("replicasets", namespace=namespace, context=context)
        rs_names = {r["metadata"]["name"] for r in rss
                    if any(o.get("kind") == "Deployment" and o.get("name") == name
                           for o in r["metadata"].get("ownerReferences", []))}
        pods = kubectl_collect.list_items("pods", namespace=namespace, context=context)
        return [p for p in pods
                if any(o.get("kind") == "ReplicaSet" and o.get("name") in rs_names
                       for o in p["metadata"].get("ownerReferences", []))]
    owner = _DIRECT_OWNER.get(ktype)
    if owner:
        pods = kubectl_collect.list_items("pods", namespace=namespace, context=context)
        return [p for p in pods
                if any(o.get("kind") == owner and o.get("name") == name
                       for o in p["metadata"].get("ownerReferences", []))]
    return []


class ResourceDetailView(ttk.Frame):
    """
    Inspector for any kubectl resource, embeddable in-place in a tab or a Toplevel.

    Tabs for every kind: Describe, YAML, Events (refreshable snapshots). Pods also
    get Logs (live stream) and two embedded pipe-terminals (sh / bash) via
    `kubectl exec -i`. All background processes and polling loops are torn down by
    teardown().
    """

    def __init__(self, parent, kind, name, namespace, context, on_back=None):
        super().__init__(parent)
        self.kind = kind
        self.name = name
        self.namespace = namespace       # "" for cluster-scoped resources
        self.pod = name                  # alias used by the logs/exec helpers
        self.context = context
        self._torn = False
        self._after_ids = []          # scheduled after() ids to cancel
        self._procs = []              # every Popen we own (logs + exec shells)

        # async command plumbing (describe/yaml/events run off the UI thread)
        self._cmd_q: queue.Queue = queue.Queue()
        # live-logs plumbing
        self._log_proc = None
        self._log_q: queue.Queue = queue.Queue()
        self._log_paused = False
        # child-pods ("what it deploys" / "pods on node") plumbing
        self._pods_q: queue.Queue = queue.Queue()
        self._rpods_rows = {}

        is_pod = kind in ("pod", "pods", "po")
        nsargs = ["-n", namespace] if namespace else []
        crumb = (f"{namespace} / " if namespace else "") + f"{kind}/{name}"

        header = ttk.Frame(self, padding=(4, 4, 4, 0))
        header.pack(fill="x")
        if on_back is not None:
            ttk.Button(header, text="← Back", command=on_back).pack(side="left")
        ttk.Label(header, text=f"  {crumb}", font=("", 10, "bold")).pack(side="left")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)

        self._build_cmd_tab(nb, "Describe", ["describe", kind, name] + nsargs)
        self._build_cmd_tab(nb, "YAML", ["get", kind, name] + nsargs + ["-o", "yaml"])
        if namespace:
            self._build_cmd_tab(nb, "Events", [
                "get", "events", "-n", namespace,
                "--field-selector", f"involvedObject.name={name}"])
        if is_pod:
            self._build_logs_tab(nb)
            self._build_exec_tab(nb, "sh")
            self._build_exec_tab(nb, "bash")
        elif kind in _HAS_PODS:
            self._build_resource_pods_tab(nb)

        self._after_ids.append(self.after(100, self._poll_cmd))
        # If the frame is destroyed out from under us (e.g. tab rebuild), clean up.
        self.bind("<Destroy>", lambda e: self.teardown() if e.widget is self else None)

    # -- describe / get (refreshable snapshots) ----------------------------
    def _build_cmd_tab(self, nb, title, args):
        frame = ttk.Frame(nb)
        nb.add(frame, text=title)
        bar = ttk.Frame(frame, padding=4)
        bar.pack(fill="x")
        status = ttk.Label(bar, text="")
        ttk.Button(bar, text="⟳ Refresh",
                   command=lambda: self._run_cmd(args, txt, status)).pack(side="left")
        status.pack(side="left", padx=8)

        wrap = ttk.Frame(frame)
        wrap.pack(fill="both", expand=True)
        txt = tk.Text(wrap, wrap="none", font=("Consolas", 9),
                      background="#111", foreground="#ddd", insertbackground="#ddd")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=txt.yview)
        hs = ttk.Scrollbar(frame, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        txt.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        hs.pack(fill="x")

        self._run_cmd(args, txt, status)  # populate immediately

    def _run_cmd(self, args, txt, status):
        status.config(text="⏳ running…")

        def work():
            try:
                out = kubectl_collect.run_text(args, context=self.context)
                self._cmd_q.put((txt, status, out, None))
            except Exception as e:  # noqa: BLE001 — surfaced in the widget
                self._cmd_q.put((txt, status, "", e))

        threading.Thread(target=work, daemon=True).start()

    def _poll_cmd(self):
        if self._torn:
            return
        try:
            while True:
                txt, status, out, err = self._cmd_q.get_nowait()
                txt.delete("1.0", "end")
                if err is not None:
                    txt.insert("1.0", str(err))
                    status.config(text="⚠ failed")
                else:
                    txt.insert("1.0", out)
                    status.config(text=f"updated {time.strftime('%H:%M:%S')}")
        except queue.Empty:
            pass
        self._after_ids.append(self.after(150, self._poll_cmd))

    # -- live logs ---------------------------------------------------------
    def _build_logs_tab(self, nb):
        frame = ttk.Frame(nb)
        nb.add(frame, text="Logs (live)")
        bar = ttk.Frame(frame, padding=4)
        bar.pack(fill="x")
        self._log_status = ttk.Label(bar, text="")
        self._pause_btn = ttk.Button(bar, text="⏸ Pause", command=self._toggle_pause)
        self._pause_btn.pack(side="left")
        ttk.Button(bar, text="↻ Restart", command=self._restart_logs).pack(side="left", padx=4)
        ttk.Button(bar, text="🗑 Clear", command=self._clear_logs).pack(side="left")
        self._autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="auto-scroll", variable=self._autoscroll).pack(side="left", padx=8)
        self._log_status.pack(side="left", padx=8)

        wrap = ttk.Frame(frame)
        wrap.pack(fill="both", expand=True)
        self._log_txt = tk.Text(wrap, wrap="none", font=("Consolas", 9),
                                background="#0b0b0b", foreground="#cfe3cf")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self._log_txt.yview)
        self._log_txt.configure(yscrollcommand=vs.set)
        self._log_txt.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        self._start_logs()
        self._pump_logs()

    def _start_logs(self):
        try:
            self._log_proc = kubectl_collect.popen_logs(
                self.namespace, self.pod, context=self.context)
            self._procs.append(self._log_proc)
        except Exception as e:  # noqa: BLE001
            self._log_txt.insert("end", f"⚠ could not start logs: {e}\n")
            self._log_status.config(text="⚠ failed")
            return
        self._log_status.config(text="● streaming")

        def reader(proc):
            try:
                for line in proc.stdout:
                    self._log_q.put(line)
            except Exception:  # noqa: BLE001 — process torn down
                pass
            self._log_q.put(("__eof__",))

        threading.Thread(target=reader, args=(self._log_proc,), daemon=True).start()

    def _pump_logs(self):
        if self._torn:
            return
        appended = False
        try:
            while True:
                item = self._log_q.get_nowait()
                if isinstance(item, tuple):  # EOF sentinel
                    self._log_status.config(text="stream ended")
                    continue
                if not self._log_paused:
                    self._log_txt.insert("end", item)
                    appended = True
        except queue.Empty:
            pass
        if appended and self._autoscroll.get():
            self._log_txt.see("end")
        self._after_ids.append(self.after(200, self._pump_logs))

    def _toggle_pause(self):
        self._log_paused = not self._log_paused
        self._pause_btn.config(text="▶ Resume" if self._log_paused else "⏸ Pause")
        self._log_status.config(text="paused" if self._log_paused else "● streaming")

    def _clear_logs(self):
        self._log_txt.delete("1.0", "end")

    def _stop_logs(self):
        self._terminate(self._log_proc)
        self._log_proc = None

    def _restart_logs(self):
        self._stop_logs()
        self._clear_logs()
        try:
            while True:
                self._log_q.get_nowait()
        except queue.Empty:
            pass
        self._start_logs()

    # -- child pods ("what it deploys" / "pods on node") -------------------
    def _build_resource_pods_tab(self, nb):
        frame = ttk.Frame(nb)
        label = "Pods on node" if self.kind in ("nodes", "node") else "Pods"
        nb.add(frame, text=label)
        bar = ttk.Frame(frame, padding=4)
        bar.pack(fill="x")
        self._rpods_status = ttk.Label(bar, text="")
        ttk.Button(bar, text="⟳ Refresh",
                   command=self._load_resource_pods).pack(side="left")
        self._rpods_status.pack(side="left", padx=8)

        cols = ("namespace", "pod", "ready", "phase", "restarts", "node", "age")
        wrap = ttk.Frame(frame)
        wrap.pack(fill="both", expand=True)
        tree = ttk.Treeview(wrap, columns=cols, show="headings")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vs.set)
        for c, w in zip(cols, [110, 300, 60, 90, 70, 150, 70]):
            tree.heading(c, text=c)
            tree.column(c, width=w, anchor="w")
        tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        tree.bind("<Double-1>", lambda e: self._open_child_pod(tree))
        self._rpods_tree = tree
        ttk.Label(frame, padding=4,
                  text="Double-click a pod to open it.").pack(anchor="w")

        self._load_resource_pods()
        self._after_ids.append(self.after(150, self._poll_resource_pods))

    def _load_resource_pods(self):
        self._rpods_status.config(text="⏳ loading…")

        def work():
            try:
                items = pods_for(self.kind, self.name, self.namespace, self.context)
                self._pods_q.put(("ok", items))
            except Exception as e:  # noqa: BLE001
                self._pods_q.put(("err", e))

        threading.Thread(target=work, daemon=True).start()

    def _poll_resource_pods(self):
        if self._torn:
            return
        try:
            while True:
                status, payload = self._pods_q.get_nowait()
                tree = self._rpods_tree
                for r in tree.get_children(""):
                    tree.delete(r)
                self._rpods_rows = {}
                if status == "err":
                    self._rpods_status.config(text=f"⚠ {payload}")
                else:
                    for it in sorted(payload, key=lambda x: _meta(x).get("name", "")):
                        ns = _meta(it).get("namespace", "")
                        iid = tree.insert("", "end", values=[ns] + _pod_row(it))
                        self._rpods_rows[iid] = (_meta(it).get("name", ""), ns)
                    self._rpods_status.config(text=f"{len(payload)} pods")
        except queue.Empty:
            pass
        self._after_ids.append(self.after(400, self._poll_resource_pods))

    def _open_child_pod(self, tree):
        iid = tree.focus()
        if not iid or iid not in self._rpods_rows:
            return
        name, ns = self._rpods_rows[iid]
        ResourceDetailWindow(self, "pods", name, ns, self.context)

    # -- embedded shell (kubectl exec -i) ----------------------------------
    def _build_exec_tab(self, nb, shell):
        frame = ttk.Frame(nb)
        nb.add(frame, text=f"Shell: {shell}")

        bar = ttk.Frame(frame, padding=4)
        bar.pack(fill="x")
        status = ttk.Label(bar, text="")
        # Per-tab mutable state so sh and bash don't collide.
        st = {"proc": None, "q": queue.Queue(), "out": None, "status": status}
        ttk.Button(bar, text="↻ Reconnect",
                   command=lambda: self._exec_start(shell, st)).pack(side="left")
        ttk.Button(bar, text="🗑 Clear",
                   command=lambda: st["out"].delete("1.0", "end")).pack(side="left", padx=4)
        status.pack(side="left", padx=8)

        wrap = ttk.Frame(frame)
        wrap.pack(fill="both", expand=True)
        out = tk.Text(wrap, wrap="char", font=("Consolas", 9),
                      background="#0b0b0b", foreground="#e0e0e0", insertbackground="#e0e0e0")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=out.yview)
        out.configure(yscrollcommand=vs.set)
        out.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        st["out"] = out

        inbar = ttk.Frame(frame, padding=4)
        inbar.pack(fill="x")
        ttk.Label(inbar, text="$").pack(side="left")
        entry = ttk.Entry(inbar)
        entry.pack(side="left", fill="x", expand=True, padx=4)
        entry.bind("<Return>", lambda e: self._exec_send(shell, st, entry))
        ttk.Button(inbar, text="Send",
                   command=lambda: self._exec_send(shell, st, entry)).pack(side="left")

        out.insert("end", f"(no {shell} session — press ↻ Reconnect to start)\n"
                          "Line-oriented shell: ls / cat / env work; "
                          "full-screen TUIs (vim, top) do not.\n\n")
        self._exec_start(shell, st)
        self._exec_pump(st)

    def _exec_start(self, shell, st):
        self._terminate(st["proc"])
        try:
            proc = kubectl_collect.popen_exec(
                self.namespace, self.pod, shell, context=self.context)
        except Exception as e:  # noqa: BLE001
            st["out"].insert("end", f"⚠ could not start {shell}: {e}\n")
            st["status"].config(text="⚠ failed")
            return
        st["proc"] = proc
        self._procs.append(proc)
        st["status"].config(text=f"● {shell} connected")

        def reader(p):
            try:
                for line in p.stdout:
                    st["q"].put(line)
            except Exception:  # noqa: BLE001
                pass
            st["q"].put(("__eof__",))

        threading.Thread(target=reader, args=(proc,), daemon=True).start()

    def _exec_send(self, shell, st, entry):
        cmd = entry.get()
        proc = st["proc"]
        if proc is None or proc.poll() is not None:
            st["out"].insert("end", "⚠ no live shell — press ↻ Reconnect.\n")
            return
        st["out"].insert("end", f"$ {cmd}\n")
        st["out"].see("end")
        try:
            proc.stdin.write(cmd + "\n")
            proc.stdin.flush()
        except Exception as e:  # noqa: BLE001 — broken pipe if shell died
            st["out"].insert("end", f"⚠ write failed: {e}\n")
        entry.delete(0, "end")

    def _exec_pump(self, st):
        if self._torn:
            return
        appended = False
        try:
            while True:
                item = st["q"].get_nowait()
                if isinstance(item, tuple):  # EOF
                    st["status"].config(text="session ended")
                    continue
                st["out"].insert("end", item)
                appended = True
        except queue.Empty:
            pass
        if appended:
            st["out"].see("end")
        self._after_ids.append(self.after(120, lambda: self._exec_pump(st)))

    # -- lifecycle ---------------------------------------------------------
    def _terminate(self, proc):
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass

    def teardown(self):
        if self._torn:
            return
        self._torn = True
        for aid in self._after_ids:
            try:
                self.after_cancel(aid)
            except Exception:  # noqa: BLE001
                pass
        for proc in self._procs:
            self._terminate(proc)


class ResourceDetailWindow(tk.Toplevel):
    """Thin Toplevel wrapper that hosts a ResourceDetailView in its own window."""

    def __init__(self, parent, kind, name, namespace, context):
        super().__init__(parent)
        self.title(f"{namespace + '/' if namespace else ''}{kind}/{name}")
        self.geometry("1000x680")
        self.view = ResourceDetailView(self, kind, name, namespace, context)
        self.view.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        self.view.teardown()
        self.destroy()


def wire_row_actions(tree, on_open_window, on_open_inplace):
    """
    Standard row interaction: the leading ⧉ column (single-click) opens a new
    window; a double-click anywhere else navigates in-place. Assumes the tree's
    first column is the ⧉ "open" column. Callbacks receive the row iid.
    """
    def on_click(e):
        if tree.identify_region(e.x, e.y) != "cell":
            return
        if tree.identify_column(e.x) != "#1":
            return
        row = tree.identify_row(e.y)
        if row:
            on_open_window(row)

    def on_double(e):
        if tree.identify_column(e.x) == "#1":
            return  # the ⧉ column is single-click only
        sel = tree.focus()
        if sel:
            on_open_inplace(sel)

    tree.bind("<Button-1>", on_click)
    tree.bind("<Double-1>", on_double)


class MultiSelect:
    """
    Adds checkbox behavior to a Treeview whose first column is "sel" (☐/☑) and
    second column is "open" (⧉). Single-click toggles the checkbox; ⧉ opens a new
    window; double-click elsewhere navigates in-place. targets() returns the
    checked rows, or the focused row if nothing is checked.
    """

    def __init__(self, tree, on_open_window, on_open_inplace):
        self.tree = tree
        self.checked = set()
        self._on_window = on_open_window
        self._on_inplace = on_open_inplace
        tree.bind("<Button-1>", self._on_click)
        tree.bind("<Double-1>", self._on_double)

    def _toggle(self, row):
        if row in self.checked:
            self.checked.discard(row)
            self.tree.set(row, "sel", "☐")
        else:
            self.checked.add(row)
            self.tree.set(row, "sel", "☑")

    def _on_click(self, e):
        if self.tree.identify_region(e.x, e.y) != "cell":
            return
        col = self.tree.identify_column(e.x)
        row = self.tree.identify_row(e.y)
        if not row:
            return
        if col == "#1":
            self._toggle(row)
        elif col == "#2":
            self._on_window(row)

    def _on_double(self, e):
        if self.tree.identify_column(e.x) in ("#1", "#2"):
            return
        sel = self.tree.focus()
        if sel:
            self._on_inplace(sel)

    def reset(self):
        self.checked.clear()

    def targets(self):
        if self.checked:
            return [i for i in self.checked if self.tree.exists(i)]
        f = self.tree.focus()
        return [f] if f else []


def open_inline_detail(host, list_frame, kind, name, namespace, context):
    """
    Swap list_frame out for a ResourceDetailView inside host, with a Back button
    that tears the detail down and restores the list. Returns the detail view.
    """
    holder = {}

    def back():
        d = holder.get("d")
        if d is not None:
            d.teardown()
            d.destroy()
        list_frame.pack(fill="both", expand=True)

    list_frame.pack_forget()
    d = ResourceDetailView(host, kind, name, namespace, context, on_back=back)
    holder["d"] = d
    d.pack(fill="both", expand=True)
    return d


# ---------------------------------------------------------------------------
# Generic resource browser (mirrors the k8s Dashboard's read-only views + a few
# safe, confirmed actions). One registry drives every grouped tab.
# ---------------------------------------------------------------------------
def _meta(it):
    return it.get("metadata", {})


def _row_age(it):
    return age_from(_meta(it).get("creationTimestamp"))


class Kind:
    """One resource type: how to list it, its columns, and what actions it allows."""

    def __init__(self, label, ktype, namespaced, columns, row, *,
                 scalable=False, restartable=False, deletable=True):
        self.label = label
        self.ktype = ktype            # kubectl resource type (e.g. "deployments")
        self.namespaced = namespaced
        self.columns = columns        # list of (header, width)
        self.row = row                # fn(item) -> list[str], len == len(columns)
        self.scalable = scalable
        self.restartable = restartable
        self.deletable = deletable


def _pod_row(it):
    st = it.get("status", {})
    cs = st.get("containerStatuses", [])
    total = len(it.get("spec", {}).get("containers", []))
    ready = sum(1 for c in cs if c.get("ready"))
    restarts = sum(c.get("restartCount", 0) for c in cs)
    return [_meta(it)["name"], f"{ready}/{total}", st.get("phase", "?"),
            str(restarts), it.get("spec", {}).get("nodeName", "—"), _row_age(it)]


def _deploy_row(it):
    sp, st = it.get("spec", {}), it.get("status", {})
    return [_meta(it)["name"], f"{st.get('readyReplicas', 0)}/{sp.get('replicas', 0)}",
            str(st.get("updatedReplicas", 0)), str(st.get("availableReplicas", 0)),
            _row_age(it)]


def _sts_row(it):
    sp, st = it.get("spec", {}), it.get("status", {})
    return [_meta(it)["name"], f"{st.get('readyReplicas', 0)}/{sp.get('replicas', 0)}",
            _row_age(it)]


def _ds_row(it):
    st = it.get("status", {})
    return [_meta(it)["name"], str(st.get("desiredNumberScheduled", 0)),
            str(st.get("numberReady", 0)), str(st.get("updatedNumberScheduled", 0)),
            str(st.get("numberAvailable", 0)), _row_age(it)]


def _rs_row(it):
    sp, st = it.get("spec", {}), it.get("status", {})
    return [_meta(it)["name"], str(sp.get("replicas", 0)),
            str(st.get("availableReplicas", 0)), str(st.get("readyReplicas", 0)),
            _row_age(it)]


def _job_row(it):
    sp, st = it.get("spec", {}), it.get("status", {})
    return [_meta(it)["name"], f"{st.get('succeeded', 0)}/{sp.get('completions', 1)}",
            str(st.get("active", 0)), _row_age(it)]


def _cronjob_row(it):
    sp, st = it.get("spec", {}), it.get("status", {})
    last = st.get("lastScheduleTime", "")
    return [_meta(it)["name"], sp.get("schedule", ""), str(sp.get("suspend", False)),
            str(len(st.get("active", []) or [])), last, _row_age(it)]


def _svc_row(it):
    sp = it.get("spec", {})
    ports = ",".join(str(p.get("port", "")) + ("/" + p.get("protocol", "") if p.get("protocol") else "")
                     for p in sp.get("ports", []))
    ext = sp.get("externalIPs") or (it.get("status", {}).get("loadBalancer", {}).get("ingress"))
    ext_s = ",".join(str(x) for x in ext) if isinstance(ext, list) else (str(ext) if ext else "—")
    return [_meta(it)["name"], sp.get("type", "ClusterIP"),
            sp.get("clusterIP", "—"), ext_s, ports, _row_age(it)]


def _ing_row(it):
    sp = it.get("spec", {})
    hosts = ",".join(r.get("host", "*") for r in sp.get("rules", []) or [])
    addr = ",".join(i.get("ip", i.get("hostname", ""))
                    for i in it.get("status", {}).get("loadBalancer", {}).get("ingress", []) or [])
    return [_meta(it)["name"], sp.get("ingressClassName", "—"), hosts or "—",
            addr or "—", _row_age(it)]


def _endpoints_row(it):
    n = sum(len(s.get("addresses", []) or []) for s in it.get("subsets", []) or [])
    return [_meta(it)["name"], str(n), _row_age(it)]


def _netpol_row(it):
    return [_meta(it)["name"],
            str(bool(it.get("spec", {}).get("podSelector", {}).get("matchLabels"))),
            _row_age(it)]


def _cm_row(it):
    return [_meta(it)["name"], str(len(it.get("data", {}) or {})), _row_age(it)]


def _secret_row(it):
    return [_meta(it)["name"], it.get("type", ""),
            str(len(it.get("data", {}) or {})), _row_age(it)]


def _pvc_row(it):
    sp, st = it.get("spec", {}), it.get("status", {})
    cap = st.get("capacity", {}).get("storage", "—")
    return [_meta(it)["name"], st.get("phase", "?"), sp.get("volumeName", "—"),
            cap, sp.get("storageClassName", "—"), _row_age(it)]


def _pv_row(it):
    sp, st = it.get("spec", {}), it.get("status", {})
    claim = sp.get("claimRef", {})
    claim_s = f'{claim.get("namespace","")}/{claim.get("name","")}' if claim else "—"
    return [_meta(it)["name"], sp.get("capacity", {}).get("storage", "—"),
            ",".join(sp.get("accessModes", []) or []), sp.get("persistentVolumeReclaimPolicy", ""),
            st.get("phase", "?"), claim_s, sp.get("storageClassName", "—"), _row_age(it)]


def _sc_row(it):
    return [_meta(it)["name"], it.get("provisioner", ""),
            it.get("reclaimPolicy", ""), _row_age(it)]


RESOURCE_GROUPS = {
    "Workloads": [
        Kind("Deployments", "deployments", True,
             [("name", 280), ("ready", 80), ("up-to-date", 90), ("available", 90), ("age", 80)],
             _deploy_row, scalable=True, restartable=True),
        Kind("StatefulSets", "statefulsets", True,
             [("name", 280), ("ready", 80), ("age", 80)],
             _sts_row, scalable=True, restartable=True),
        Kind("DaemonSets", "daemonsets", True,
             [("name", 260), ("desired", 80), ("ready", 70), ("up-to-date", 90),
              ("available", 90), ("age", 80)], _ds_row, restartable=True),
        Kind("ReplicaSets", "replicasets", True,
             [("name", 300), ("desired", 80), ("available", 90), ("ready", 70), ("age", 80)],
             _rs_row, scalable=True),
        Kind("Jobs", "jobs", True,
             [("name", 300), ("completions", 100), ("active", 70), ("age", 80)], _job_row),
        Kind("CronJobs", "cronjobs", True,
             [("name", 240), ("schedule", 120), ("suspend", 80), ("active", 70),
              ("last schedule", 170), ("age", 80)], _cronjob_row),
        Kind("Pods", "pods", True,
             [("name", 300), ("ready", 70), ("phase", 100), ("restarts", 80),
              ("node", 160), ("age", 80)], _pod_row),
    ],
    "Networking": [
        Kind("Services", "services", True,
             [("name", 260), ("type", 110), ("cluster IP", 130), ("external", 140),
              ("ports", 140), ("age", 80)], _svc_row),
        Kind("Ingresses", "ingresses", True,
             [("name", 240), ("class", 100), ("hosts", 260), ("address", 160), ("age", 80)],
             _ing_row),
        Kind("Endpoints", "endpoints", True,
             [("name", 320), ("addresses", 100), ("age", 80)], _endpoints_row),
        Kind("NetworkPolicies", "networkpolicies", True,
             [("name", 320), ("has selector", 110), ("age", 80)], _netpol_row),
    ],
    "Config": [
        Kind("ConfigMaps", "configmaps", True,
             [("name", 360), ("data keys", 100), ("age", 80)], _cm_row),
        Kind("Secrets", "secrets", True,
             [("name", 320), ("type", 200), ("data keys", 100), ("age", 80)], _secret_row),
    ],
    "Storage": [
        Kind("PersistentVolumeClaims", "persistentvolumeclaims", True,
             [("name", 260), ("status", 90), ("volume", 220), ("capacity", 90),
              ("storageclass", 130), ("age", 80)], _pvc_row),
        Kind("PersistentVolumes", "persistentvolumes", False,
             [("name", 240), ("capacity", 90), ("access", 120), ("reclaim", 90),
              ("status", 90), ("claim", 200), ("storageclass", 120), ("age", 70)], _pv_row),
        Kind("StorageClasses", "storageclasses", False,
             [("name", 260), ("provisioner", 300), ("reclaim", 110), ("age", 80)], _sc_row),
    ],
}


class ResourceBrowser(ttk.Frame):
    """A grouped resource tab: type selector + namespace filter + table + actions."""

    def __init__(self, parent, get_context, kinds):
        super().__init__(parent)
        self._get_context = get_context
        self._kinds = {k.label: k for k in kinds}
        self._q: queue.Queue = queue.Queue()
        self._rows = {}               # tree iid -> (name, namespace)
        self._namespaces = ["(all)"]
        self._tree = None
        self._detail = None           # in-place detail view, when open
        self._alive = True
        self.bind("<Destroy>", lambda e: setattr(self, "_alive", False)
                  if e.widget is self else None)

        # All list UI lives in a container we can hide to show a detail in-place.
        self._list = ttk.Frame(self)
        self._list.pack(fill="both", expand=True)

        bar = ttk.Frame(self._list, padding=4)
        bar.pack(fill="x")
        ttk.Label(bar, text="Type:").pack(side="left")
        self.kind_var = tk.StringVar(value=kinds[0].label)
        kb = ttk.Combobox(bar, textvariable=self.kind_var, width=22, state="readonly",
                          values=[k.label for k in kinds])
        kb.pack(side="left", padx=4)
        kb.bind("<<ComboboxSelected>>", lambda e: self._reload())

        ttk.Label(bar, text="Namespace:").pack(side="left", padx=(10, 0))
        self.ns_var = tk.StringVar(value="(all)")
        self.ns_box = ttk.Combobox(bar, textvariable=self.ns_var, width=22,
                                   state="readonly", values=self._namespaces)
        self.ns_box.pack(side="left", padx=4)
        self.ns_box.bind("<<ComboboxSelected>>", lambda e: self._reload())
        ttk.Button(bar, text="⟳ Refresh", command=self._reload).pack(side="left", padx=6)
        self.status = ttk.Label(bar, text="")
        self.status.pack(side="left", padx=8)

        act = ttk.Frame(self._list, padding=(4, 0, 4, 4))
        act.pack(fill="x")
        self.scale_btn = ttk.Button(act, text="Scale…", command=self._do_scale)
        self.restart_btn = ttk.Button(act, text="Rollout restart", command=self._do_restart)
        self.delete_btn = ttk.Button(act, text="Delete…", command=self._do_delete)
        self.scale_btn.pack(side="left")
        self.restart_btn.pack(side="left", padx=4)
        self.delete_btn.pack(side="left")
        ttk.Label(act, text="  (double-click a row to open it here; click ⧉ for a new window)"
                  ).pack(side="left", padx=6)

        self._table = ttk.Frame(self._list)
        self._table.pack(fill="both", expand=True)

        self.after(120, self._poll)
        self._reload()

    def _cur_kind(self):
        return self._kinds[self.kind_var.get()]

    def _reload(self):
        if not self._alive:
            return
        kind = self._cur_kind()
        self.ns_box.state(["!disabled"] if kind.namespaced else ["disabled"])
        self.scale_btn.state(["!disabled"] if kind.scalable else ["disabled"])
        self.restart_btn.state(["!disabled"] if kind.restartable else ["disabled"])
        self.delete_btn.state(["!disabled"] if kind.deletable else ["disabled"])
        self.status.config(text="⏳ loading…")
        ctx = self._get_context()
        ns = self.ns_var.get()
        want_ns = ns if (kind.namespaced and ns != "(all)") else ""
        all_ns = kind.namespaced and ns == "(all)"
        need_ns_list = kind.namespaced and self._namespaces == ["(all)"]

        def work():
            try:
                items = kubectl_collect.list_items(
                    kind.ktype, namespace=want_ns, all_namespaces=all_ns, context=ctx)
                nslist = None
                if need_ns_list:
                    nslist = kubectl_collect.list_namespaces(ctx)
                self._q.put(("ok", kind, items, nslist))
            except Exception as e:  # noqa: BLE001
                self._q.put(("err", kind, e, None))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        if not self._alive:
            return
        try:
            while True:
                status, kind, payload, nslist = self._q.get_nowait()
                if nslist:
                    self._namespaces = ["(all)"] + nslist
                    self.ns_box["values"] = self._namespaces
                if status == "err":
                    self.status.config(text=f"⚠ {payload}")
                elif status == "action_ok":
                    self.status.config(text=f"✓ {payload}")
                else:
                    self._fill(kind, payload)
        except queue.Empty:
            pass
        self.after(200, self._poll)

    def _fill(self, kind, items):
        for w in self._table.winfo_children():
            w.destroy()
        self._rows = {}
        headers = ["open"] + (["namespace"] if kind.namespaced else []) \
            + [h for h, _ in kind.columns]
        widths = [40] + ([120] if kind.namespaced else []) \
            + [w for _, w in kind.columns]

        wrap = ttk.Frame(self._table)
        wrap.pack(fill="both", expand=True)
        tree = ttk.Treeview(wrap, columns=headers, show="headings")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vs.set)
        for h, w in zip(headers, widths):
            tree.heading(h, text="⧉" if h == "open" else h)
            tree.column(h, width=w, anchor=("center" if h == "open" else "w"),
                        stretch=(h != "open"))
        tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self._tree = tree

        for it in sorted(items, key=lambda x: _meta(x).get("name", "")):
            ns = _meta(it).get("namespace", "")
            vals = ["⧉"] + ([ns] if kind.namespaced else []) + kind.row(it)
            iid = tree.insert("", "end", values=vals)
            self._rows[iid] = (_meta(it).get("name", ""), ns)
        self.status.config(text=f"{len(items)} {kind.label.lower()}")

        wire_row_actions(tree, self._open_window_for, self._open_inplace_for)

    def _row_target(self, iid):
        if iid not in self._rows:
            return None
        name, ns = self._rows[iid]
        return self._cur_kind(), name, ns

    def _open_window_for(self, iid):
        t = self._row_target(iid)
        if t:
            kind, name, ns = t
            ResourceDetailWindow(self, kind.ktype, name, ns, self._get_context())

    def _open_inplace_for(self, iid):
        t = self._row_target(iid)
        if not t:
            return
        kind, name, ns = t
        self._detail = open_inline_detail(
            self, self._list, kind.ktype, name, ns, self._get_context())

    def _selection(self):
        if not self._tree:
            return None
        iid = self._tree.focus()
        if not iid or iid not in self._rows:
            messagebox.showinfo("No selection", "Select a row first.")
            return None
        name, ns = self._rows[iid]
        return self._cur_kind(), name, ns

    def _do_scale(self):
        sel = self._selection()
        if not sel:
            return
        kind, name, ns = sel
        n = simpledialog.askinteger("Scale", f"Replicas for {kind.ktype}/{name}:",
                                    parent=self, minvalue=0)
        if n is None:
            return
        self._run_action(lambda: kubectl_collect.scale(
            kind.ktype, name, n, namespace=ns, context=self._get_context()),
            f"scaled {name} to {n}")

    def _do_restart(self):
        sel = self._selection()
        if not sel:
            return
        kind, name, ns = sel
        if not messagebox.askyesno("Rollout restart",
                                   f"Trigger a rolling restart of {kind.ktype}/{name}?"):
            return
        self._run_action(lambda: kubectl_collect.rollout_restart(
            kind.ktype, name, namespace=ns, context=self._get_context()),
            f"restarted {name}")

    def _do_delete(self):
        sel = self._selection()
        if not sel:
            return
        kind, name, ns = sel
        if not messagebox.askyesno(
                "Delete", f"Delete {kind.ktype}/{name}"
                + (f" in {ns}" if ns else "") + "?\n\nThis cannot be undone.",
                icon="warning"):
            return
        self._run_action(lambda: kubectl_collect.delete(
            kind.ktype, name, namespace=ns, context=self._get_context()),
            f"deleted {name}")

    def _run_action(self, fn, ok_msg):
        self.status.config(text="⏳ working…")

        def work():
            try:
                fn()
                self._q.put(("action_ok", self._cur_kind(), ok_msg, None))
            except Exception as e:  # noqa: BLE001
                self._q.put(("err", self._cur_kind(), e, None))

        threading.Thread(target=work, daemon=True).start()
        # reload shortly after so the table reflects the change
        self.after(600, self._reload)


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "data"
    Dashboard(folder).mainloop()


if __name__ == "__main__":
    main()
