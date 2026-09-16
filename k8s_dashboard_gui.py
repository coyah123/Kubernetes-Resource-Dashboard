"""
CULMIN8 — Client-side, Ultra-Lightweight Manager, Inspector & Navigator for K8s
— native desktop window, standard library ONLY.

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

How this file is organized (top to bottom, follow the `# ---` banners):
  1. Quantity parsing        — CPU/memory string <-> number helpers.
  2. Load + shape the data    — turn raw kubectl JSON into the display `model`
                                (build_model*, build_*_row).
  3. GUI — main window        — the Dashboard class: control rows, sidebar nav,
                                and the analysis tabs (Overview/Nodes/Deployments/
                                Pods/Resource Management/Trends).
  4. Resource detail inspector— ResourceDetailView (Describe/YAML/Events, plus
                                Logs+Shell for pods, the secret decoder + openssl
                                cert inspector, and child-pod lists).
  5. Row interaction          — the shared click model (⧉ / double-click / ☑).
  6. Generic resource browser — the Kind registry (RESOURCE_GROUPS), its row
                                renderers, ResourceBrowser, and the CRD-discovering
                                CustomResourceBrowser.

Threading rule of thumb: kubectl never runs on the UI thread. Each screen kicks
work onto a daemon thread that drops results on a queue.Queue, and an after()
timer on the UI thread drains that queue to update widgets.
"""
from __future__ import annotations

import base64
import binascii
import csv
import json
import os
import queue
import re
import shutil
import subprocess
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
    """A Kubernetes CPU quantity ('250m', '2', '500000n') -> millicores (float)."""
    if not q:
        return 0.0
    q = str(q).strip()
    for suf, mult in _CPU_SUFFIX.items():
        if q.endswith(suf):
            return float(q[:-len(suf)]) * mult
    return float(q) * 1000.0


def mem_to_bytes(q):
    """A Kubernetes memory quantity ('512Mi', '2Gi', '1000000') -> bytes (float)."""
    if not q:
        return 0.0
    q = str(q).strip()
    for suf, mult in _MEM_SUFFIX.items():
        if q.endswith(suf):
            return float(q[:-len(suf)]) * mult
    return float(q)


def fmt_cpu(m):
    """Millicores -> display string ('750m', '2.00 cores'); '—' for None."""
    if m is None:
        return "—"
    if m >= 1000:
        return f"{m/1000:.2f} cores"
    return f"{m:.0f}m"


def fmt_mem(b):
    """Bytes -> display string ('1.5Gi', '512.0Mi'); '—' for None."""
    if b is None:
        return "—"
    for unit, size in (("Gi", 1024**3), ("Mi", 1024**2), ("Ki", 1024)):
        if b >= size:
            return f"{b/size:.1f}{unit}"
    return f"{b:.0f}B"


def pct(n, d):
    """n/d as a rounded percent string ('83%'); '—' when the denominator is 0."""
    return f"{n/d*100:.0f}%" if d else "—"


# A pod that restarted within this window is "flapping now" → amber. A large
# window would light up pods that bounced hours ago on a node drain; CrashLoop
# backoff caps at 5-min gaps, so 10 min catches slow flappers without stale noise.
RECENT_RESTART_SECS = 10 * 60


def _secs_since_iso(ts):
    """Seconds since a Kubernetes ISO8601-Z timestamp, or None if unparseable."""
    if not ts:
        return None
    try:
        t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (datetime.now(timezone.utc) - t).total_seconds()


def age_from(ts):
    """Kubernetes creationTimestamp (ISO8601 Z) -> compact age like '3d4h', '12m'."""
    secs = _secs_since_iso(ts)
    if secs is None:
        return "—"
    secs = int(secs)
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
# Load + shape the data — turn raw kubectl JSON (live or cached files) into the
# flat per-row dicts the tables consume. build_model*() are the entry points;
# build_*_row() shape one node/pod/deployment each.
# ---------------------------------------------------------------------------
def _load(folder: Path, name: str) -> dict:
    """Read+parse one cached JSON file from the data folder; {} if missing/bad."""
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
    last_restart_secs = None  # seconds since the most recent container restart
    for cs in cstatuses:
        cstate = cs.get("state", {})
        w, t = cstate.get("waiting"), cstate.get("terminated")
        if w and w.get("reason"):
            reasons.append(w["reason"])
        elif t and t.get("reason") and t.get("exitCode"):
            reasons.append(t["reason"])
        # When a container has restarted, the prior instance's death time
        # (lastState.terminated.finishedAt) marks when the restart happened.
        if cs.get("restartCount", 0):
            fin = cs.get("lastState", {}).get("terminated", {}).get("finishedAt")
            secs = _secs_since_iso(fin)
            if secs is not None and (last_restart_secs is None or secs < last_restart_secs):
                last_restart_secs = secs
    restarted_recently = (last_restart_secs is not None
                          and last_restart_secs <= RECENT_RESTART_SECS)
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
    # Severity for row coloring: "crit" = actively failing (bad phase, or a
    # container stuck/crashed), "warn" = running but restarted *recently* (flapping
    # now), "" = healthy. Old restarts from a long-ago node drain don't earn a color.
    if phase not in ("Running", "Succeeded") or reasons:
        severity = "crit"
    elif restarted_recently:
        severity = "warn"
    else:
        severity = ""
    return {
        "namespace": ns, "pod": name, "node": node, "phase": phase, "owner": owner,
        "ready": f"{n_ready}/{n_total}", "restarts": restarts,
        "age": age_from(p["metadata"].get("creationTimestamp")),
        "pod_ip": status.get("podIP", "—"), "status_note": ", ".join(note_parts),
        "severity": severity,
        "cpu_req_m": cpu_req, "cpu_lim_m": cpu_lim, "mem_req_b": mem_req, "mem_lim_b": mem_lim,
        "cpu_used_m": usage[0], "mem_used_b": usage[1], "missing_limits": missing,
    }


# Well-known labels that name the node pool / node group, most specific first.
# Covers AWS EKS (managed node groups + Karpenter), Azure AKS, and GKE.
_POOL_LABELS = (
    "eks.amazonaws.com/nodegroup",      # EKS managed node group
    "karpenter.sh/nodepool",            # Karpenter (newer)
    "karpenter.sh/provisioner-name",    # Karpenter (older)
    "agentpool",                        # AKS (short form)
    "kubernetes.azure.com/agentpool",   # AKS (fully-qualified)
    "cloud.google.com/gke-nodepool",    # GKE
    "node.kubernetes.io/instancegroup",
    "nodepool",
)


def node_pool_from_labels(labels: dict) -> str:
    """Best-effort node-pool name from the standard cloud labels; — if none set."""
    labels = labels or {}
    for key in _POOL_LABELS:
        val = labels.get(key)
        if val:
            return val
    return "—"


def build_node_row(n: dict, usage=(None, None)) -> dict:
    """One node's row dict: allocatable capacity plus optional live usage."""
    alloc = n["status"].get("allocatable", {})
    ready = next((c["status"] for c in n["status"].get("conditions", [])
                  if c["type"] == "Ready"), "?")
    return {
        "node": n["metadata"]["name"], "ready": ready,
        "pool": node_pool_from_labels(n["metadata"].get("labels")),
        "cpu_alloc_m": cpu_to_milli(alloc.get("cpu")),
        "mem_alloc_b": mem_to_bytes(alloc.get("memory")),
        "cpu_used_m": usage[0], "mem_used_b": usage[1],
    }


def build_dep_row(d: dict, used=None) -> dict:
    """One deployment's row dict: per-pod and ×replicas requests/limits, and the
    list of missing requests/limits (the 'missing' column)."""
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
# Table export (CSV / XLSX) — shared by every analysis tab and the grouped
# resource browser. Exports exactly what's on screen: the Treeview's current
# (already filtered/sorted) rows, mining the ☑/⧉ helper columns out first.
# XLSX keeps numbers numeric and percents as real Excel percentages so totals
# and sorting still work in the sheet.
# ---------------------------------------------------------------------------
# Helper columns that only make sense inside the live GUI (checkbox / open).
_EXPORT_SKIP_COLS = {"sel", "open"}
_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_PCT_RE = re.compile(r"^-?\d+(?:\.\d+)?%$")


def _exportable_columns(tree) -> list[str]:
    """Column ids worth exporting — everything but the GUI-only helper columns."""
    return [c for c in tree["columns"] if c not in _EXPORT_SKIP_COLS]


def _export_rows(tree, columns) -> list[list[str]]:
    """The current on-screen rows (post-filter, in display order) as strings."""
    return [[tree.set(iid, c) for c in columns] for iid in tree.get_children("")]


def _xlsx_cell(value: str):
    """Map a displayed string to (python_value, number_format|None) for Excel.

    Plain integers/floats become real numbers; "45%" becomes 0.45 with a percent
    format; anything with a unit suffix (900m, 1.5Gi, "2 cores") stays text so it
    reads exactly as it does in the GUI.
    """
    s = (value or "").strip()
    if _NUM_RE.match(s):
        num = float(s)
        return (int(num) if num.is_integer() else num, None)
    if _PCT_RE.match(s):
        body = s[:-1]
        fmt = "0.0%" if "." in body else "0%"
        return (float(body) / 100.0, fmt)
    return (value, None)


def export_tree(tree, fmt, *, status_cb=None):
    """Save a Treeview's current view to ``fmt`` ("csv" or "xlsx").

    ``status_cb(text)`` (optional) is called with a one-line result message.
    """
    columns = _exportable_columns(tree)
    if not columns:
        return
    rows = _export_rows(tree, columns)
    if not rows:
        messagebox.showinfo("Export", "This table is empty — nothing to export.")
        return
    headers = [tree.heading(c, "text") or c for c in columns]
    stamp = time.strftime("%Y%m%d-%H%M%S")

    if fmt == "xlsx":
        path = filedialog.asksaveasfilename(
            title="Export table to Excel",
            defaultextension=".xlsx",
            initialfile=f"k8s-export-{stamp}.xlsx",
            filetypes=[("Excel workbook", "*.xlsx"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            import openpyxl
            from openpyxl.styles import Font
        except ImportError:
            messagebox.showerror(
                "Export failed",
                "XLSX export needs the 'openpyxl' package.\n\n"
                "Install it with:  pip install openpyxl")
            return
        try:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "export"
            ws.append(headers)
            for cell in ws[1]:
                cell.font = Font(bold=True)
            for row in rows:
                ws.append([""] * len(columns))
                r = ws.max_row
                for ci, raw in enumerate(row, start=1):
                    val, numfmt = _xlsx_cell(raw)
                    cell = ws.cell(row=r, column=ci, value=val)
                    if numfmt:
                        cell.number_format = numfmt
            ws.freeze_panes = "A2"
            for ci, h in enumerate(headers, start=1):
                width = max(len(h), *(len(str(row[ci - 1])) for row in rows))
                ws.column_dimensions[
                    openpyxl.utils.get_column_letter(ci)].width = min(max(width + 2, 8), 60)
            wb.save(path)
        except OSError as e:
            messagebox.showerror("Export failed", str(e))
            return
    else:  # csv
        path = filedialog.asksaveasfilename(
            title="Export table to CSV",
            defaultextension=".csv",
            initialfile=f"k8s-export-{stamp}.csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.writer(fh)
                writer.writerow(headers)
                writer.writerows(rows)
        except OSError as e:
            messagebox.showerror("Export failed", str(e))
            return

    if status_cb:
        status_cb(f"✓ exported {len(rows)} rows → {path}")


def add_export_buttons(toolbar, tree, *, status_cb=None, side="right"):
    """Pack CSV + XLSX export buttons (and wire a right-click menu on ``tree``)."""
    ttk.Button(toolbar, text="⬇ CSV",
               command=lambda: export_tree(tree, "csv", status_cb=status_cb)
               ).pack(side=side, padx=(4, 0))
    ttk.Button(toolbar, text="⬇ XLSX",
               command=lambda: export_tree(tree, "xlsx", status_cb=status_cb)
               ).pack(side=side)
    menu = tk.Menu(tree, tearoff=0)
    menu.add_command(label="Export to CSV…",
                     command=lambda: export_tree(tree, "csv", status_cb=status_cb))
    menu.add_command(label="Export to Excel (XLSX)…",
                     command=lambda: export_tree(tree, "xlsx", status_cb=status_cb))

    def popup(event):
        menu.tk_popup(event.x_root, event.y_root)

    tree.bind("<Button-3>", popup)      # right-click (Win/Linux)
    tree.bind("<Button-2>", popup)      # middle/right on some macs
    return menu


# ---------------------------------------------------------------------------
# GUI — main window (Dashboard): control rows, sidebar nav, and the analysis
# tabs (Overview / Nodes / Deployments / Pods / Resource Management / Trends).
# The grouped browser + resource-kind registry live further down.
# ---------------------------------------------------------------------------
class Dashboard(tk.Tk):
    """
    The main application window and controller.

    Owns the top control rows (context picker + live refresh, offline folder
    loader, status line), the collapsible left sidebar, and the notebook whose
    tabs are the app's screens. Data flows in two ways:

      - live:    refresh_from_cluster() shells out to kubectl on a worker thread,
                 drops the raw JSON on _result_q, and _poll_collect() picks it up.
      - offline: reload() reads previously-saved JSON from ``folder``.

    Either way the raw JSON is turned into the display ``model`` and the analysis
    tabs are rebuilt. Long-lived state (Trends capture, history) is kept here so
    it survives tab rebuilds; the persistent Trends tab is never torn down.
    """

    def __init__(self, folder: Path):
        # ``folder`` is where offline JSON snapshots and the trends history live.
        super().__init__()
        self.title("☸ CULMIN8 — Manager, Inspector & Navigator for K8s")
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

        # --- Bottom: kubectl command buffer ("don't make me dumb") ------------
        # Packed side=bottom BEFORE the body so it stays pinned to the window's
        # bottom edge. It shows the kubectl command the app just ran OR the command
        # you'd type to fetch whatever row you've highlighted — a live cheat sheet.
        cmdbar = ttk.Frame(self, padding=(6, 2, 6, 4))
        cmdbar.pack(side="bottom", fill="x")
        ttk.Label(cmdbar, text="kubectl:").pack(side="left")
        self._cmd_source = ttk.Label(cmdbar, text="", width=16, foreground="#777")
        self._cmd_source.pack(side="left", padx=(4, 4))
        self.cmd_var = tk.StringVar(
            value="highlight a row or refresh — the equivalent kubectl command shows here")
        cmd_entry = ttk.Entry(cmdbar, textvariable=self.cmd_var, state="readonly")
        cmd_entry.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(cmdbar, text="Copy", width=6,
                   command=self._copy_command).pack(side="left")
        # kubectl runs on worker threads, so executed commands arrive via a queue
        # that the UI thread drains; selection previews call show_command directly.
        self._cmd_disp_q: queue.Queue = queue.Queue()
        kubectl_collect.command_listener = self._cmd_disp_q.put
        self.after(200, self._poll_cmd_display)

        # --- Body: collapsible sidebar (hamburger) + notebook content -------
        # The notebook keeps driving tab content, but its top tab strip is
        # hidden — navigation lives in the left sidebar instead.
        # Hide the tab strip ONLY on the main notebook (nav lives in the sidebar).
        # Use a dedicated style name so detail-view notebooks (Describe/YAML/Logs/
        # exec) keep their normal, clickable tab headers.
        style = ttk.Style(self)
        style.layout("Sidebar.TNotebook.Tab", [])

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
        self._sidebar_title = ttk.Label(top, text="CULMIN8", font=("", 10, "bold"))
        self._sidebar_title.pack(side="left", padx=6)

        # Container that holds one button per notebook tab.
        self._nav = ttk.Frame(self.sidebar)
        self._nav.pack(fill="both", expand=True, pady=(6, 0))
        self._nav_buttons = {}  # tab-id -> ttk.Button

        self.nb = ttk.Notebook(body, style="Sidebar.TNotebook")
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
        """Draw the sidebar button for ``tab_id`` as pressed, the rest as normal."""
        for tid, btn in self._nav_buttons.items():
            btn.state(["pressed"] if str(tid) == str(tab_id) else ["!pressed"])

    def _on_tab_changed(self, _event=None):
        """Keep the sidebar highlight in sync when the notebook tab changes."""
        sel = self.nb.select()
        if sel:
            self._highlight_active(sel)

    def _toggle_sidebar(self):
        """Collapse/expand the left sidebar (the ☰ hamburger)."""
        self._sidebar_open = not self._sidebar_open
        if self._sidebar_open:
            self._nav.pack(fill="both", expand=True, pady=(6, 0))
            self._sidebar_title.pack(side="left", padx=6)
        else:
            self._nav.pack_forget()
            self._sidebar_title.pack_forget()

    # -- kubectl command buffer ("don't make me dumb") ---------------------
    def _set_command(self, text, source):
        """Show one command string in the bottom bar with a short source tag."""
        self.cmd_var.set(text)
        self._cmd_source.config(text=source)

    def show_command(self, argv, source="selected"):
        """Public: show the kubectl command for a highlighted row (argv list).

        Any widget can call this via winfo_toplevel(), so the preview is available
        everywhere without threading a callback through every table.
        """
        self._set_command(kubectl_collect.format_command(argv), f"({source})")

    def _poll_cmd_display(self):
        """Drain executed-command argv from the worker queue into the bar."""
        last = None
        try:
            while True:
                last = self._cmd_disp_q.get_nowait()
        except queue.Empty:
            pass
        if last is not None:
            self._set_command(kubectl_collect.format_command(last), "(last run)")
        self.after(300, self._poll_cmd_display)

    def _copy_command(self):
        """Copy the currently-shown command to the clipboard."""
        self.clipboard_clear()
        self.clipboard_append(self.cmd_var.get())
        self.status.config(text="✓ command copied to clipboard")

    # -- live cluster ------------------------------------------------------
    def load_contexts(self):
        """Populate the context dropdown from kubeconfig; disable refresh if none."""
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
        """
        Kick off a full live pull via kubectl on a background thread.

        The UI thread must never block on kubectl, so the actual collect() runs in
        ``work()`` and hands its result (or exception) back through _result_q; the
        _poll_collect() timer drains that queue and updates the screen. ``_busy``
        guards against overlapping refreshes.
        """
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
        """Drain the collect worker's result queue; on success rebuild the model."""
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
        """Cache the raw kubectl JSON to the data folder for offline reloads."""
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
        """Pick an offline data folder, then load the JSON snapshot in it."""
        d = filedialog.askdirectory(initialdir=self.path_var.get() or ".")
        if d:
            self.path_var.set(d)
            self.reload()

    def reload(self):
        """Load a previously-saved JSON snapshot from the data folder (offline mode)."""
        folder = Path(self.path_var.get())
        if not (folder / "pods.json").exists():
            self.status.config(text="⚠ no pods.json in that folder — "
                                     "refresh from the cluster instead.")
            return
        self.model = build_model(folder)
        self._render(source=f"files in {folder}")

    def _render(self, source: str = ""):
        """Update the status summary line and rebuild every analysis tab."""
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
        self._build_offenders_tab()
        for group in RESOURCE_GROUPS:
            self._build_group_tab(group)
        self._build_crd_tab()
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
        """
        Build a standard sortable/exportable table (ttk.Treeview) used by the
        analysis tabs. Wires up: click-header-to-sort, a CSV export button plus
        right-click "Export to CSV", and the shared red/amber row tags used to
        flag failing/heads-up rows. Returns the Treeview.
        """
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        # (the command-preview wiring is added by callers via _wire_cmd_preview)

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
        tree.tag_configure("crit", background="#5a1e1e")   # red — failing
        tree.tag_configure("warn", background="#5a4a1e")   # amber — heads-up

        # Visible CSV + XLSX buttons, plus a right-click export menu. Both export
        # exactly the rows currently on screen (current filter + sort order).
        ttk.Label(tools, text="Export:").pack(side="right", padx=(0, 4))
        add_export_buttons(tools, tree,
                           status_cb=lambda t: self.status.config(text=t))
        return tree

    def _wire_cmd_preview(self, tree, ktype, name_col, ns_col=None):
        """
        Show `kubectl get <ktype> <name> [-n ns] -o yaml` in the bottom bar whenever
        a row is highlighted in ``tree``. This is the "teach me the command" hook;
        call it once per table, naming the columns that hold the object's name/ns.
        """
        def on_sel(_e=None):
            row = tree.focus()
            if not row:
                return
            name = tree.set(row, name_col)
            if not name:
                return
            ns = tree.set(row, ns_col) if ns_col else ""
            argv = kubectl_collect.get_argv(
                ktype, name, namespace=ns, context=self.context_var.get().strip())
            self.show_command(argv, source="selected")
        tree.bind("<<TreeviewSelect>>", on_sel, add="+")

    def _sort(self, tree, col, desc):
        """
        Sort a table by one column, toggling direction on repeat clicks.

        Values are compared numerically when possible (stripping unit suffixes
        like %, m, Gi/Mi/Ki, " cores"), otherwise case-insensitively as text — so
        "900m" and "2 cores" sort sensibly rather than lexically.
        """
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
        """{node name -> (cpu millicores, mem bytes)} live from metrics-server."""
        nu = {}
        for it in kubectl_collect.node_metrics(context).get("items", []):
            nu[it["metadata"]["name"]] = (
                cpu_to_milli(it.get("usage", {}).get("cpu")),
                mem_to_bytes(it.get("usage", {}).get("memory")))
        return nu

    # -- tabs --------------------------------------------------------------
    def _build_overview_tab(self):
        """🏠 Overview: cluster totals, CPU/mem capacity vs requested vs used,
        counts of missing limits, and the biggest memory offenders."""
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
        """Nodes: requested % vs used % per node; red when >85% requested or
        NotReady. Rows open a node's details/events and the pods it hosts."""
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

        ttk.Label(bar, text="Node pool:").pack(side="left", padx=(12, 2))
        pool_filter = tk.StringVar(value="All pools")
        pool_box = ttk.Combobox(bar, textvariable=pool_filter, width=24, state="readonly",
                                values=["All pools"])
        pool_box.pack(side="left")
        pool_box.bind("<<ComboboxSelected>>", lambda _e: refill())

        status.pack(side="left", padx=8)

        cols = ("open", "node", "pool", "ready", "pods", "cpu alloc", "cpu req%", "cpu used%",
                "mem alloc", "mem req", "mem req%", "mem used", "mem used%", "⚠ why")
        tree = self._make_tree(list_frame, cols,
                               [40, 150, 130, 70, 55, 90, 80, 80, 90, 90, 80, 90, 80, 200])
        tree.heading("open", text="⧉")
        tree.column("open", anchor="center", stretch=False)
        self._wire_cmd_preview(tree, "nodes", "node")

        def refill():
            for r in tree.get_children(""):
                tree.delete(r)

            # Keep the pool picker in sync with the current nodes, preserving choice.
            pools = sorted({n["pool"] for n in self.model["nodes"]})
            pool_box["values"] = ["All pools"] + pools
            if pool_filter.get() not in pool_box["values"]:
                pool_filter.set("All pools")
            selected = pool_filter.get()

            # aggregate pod requests per node (from the current pod model)
            agg = {}
            for p in self.model["pods"]:
                a = agg.setdefault(p["node"], {"pods": 0, "cpu": 0.0, "mem": 0.0})
                a["pods"] += 1
                a["cpu"] += p["cpu_req_m"]
                a["mem"] += p["mem_req_b"]
            for n in self.model["nodes"]:
                if selected != "All pools" and n["pool"] != selected:
                    continue
                a = agg.get(n["node"], {"pods": 0, "cpu": 0.0, "mem": 0.0})
                req_cpu_p = a["cpu"] / n["cpu_alloc_m"] * 100 if n["cpu_alloc_m"] else 0
                req_mem_p = a["mem"] / n["mem_alloc_b"] * 100 if n["mem_alloc_b"] else 0
                why = []
                if req_cpu_p > 85:
                    why.append(f"CPU {req_cpu_p:.0f}% requested")
                if req_mem_p > 85:
                    why.append(f"mem {req_mem_p:.0f}% requested")
                not_ready = n["ready"] != "True"
                if not_ready:
                    why.append(f"NotReady ({n['ready']})")
                # NotReady is red (failing); over-provisioning alone is amber.
                sev = "crit" if not_ready else ("warn" if why else "")
                tree.insert("", "end", tags=(sev,), values=(
                    "⧉", n["node"], n["pool"], n["ready"], a["pods"],
                    fmt_cpu(n["cpu_alloc_m"]), f"{req_cpu_p:.0f}%",
                    pct(n["cpu_used_m"], n["cpu_alloc_m"]) if n["cpu_used_m"] is not None else "—",
                    fmt_mem(n["mem_alloc_b"]), fmt_mem(a["mem"]), f"{req_mem_p:.0f}%",
                    fmt_mem(n["mem_used_b"]) if n["mem_used_b"] is not None else "—",
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
        """Deployments: per-pod and ×replicas reserved footprint, missing
        requests/limits, and bulk rollout-restart / delete on checked rows."""
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
        self._wire_cmd_preview(tree, "deployments", "deployment", "namespace")

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
        """Pods: filterable list flagging unhealthy/restarting pods (red/amber),
        with bulk delete and per-pod details / logs / shell on open."""
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="Pods")
        self._analysis_frames.append(frame)
        list_frame = ttk.Frame(frame)
        list_frame.pack(fill="both", expand=True)
        bar = ttk.Frame(list_frame, padding=4)
        bar.pack(fill="x")
        ctx = lambda: self.context_var.get().strip()

        ttk.Label(bar, text="Namespace:").pack(side="left")
        namespaces = ["(all)"] + sorted({p["namespace"] for p in self.model["pods"]})
        ns_var = tk.StringVar(value="(all)")
        ns_box = ttk.Combobox(bar, textvariable=ns_var, width=20, state="readonly",
                              values=namespaces)
        ns_box.pack(side="left", padx=4)

        # Nodepool + Node filters. Pool comes from the nodes model (Karpenter/EKS/
        # GKE labels); the Node dropdown cascades to just the selected pool's nodes.
        ttk.Label(bar, text="Nodepool:").pack(side="left", padx=(10, 0))
        pool_var = tk.StringVar(value="(all)")
        pool_box = ttk.Combobox(bar, textvariable=pool_var, width=18, state="readonly",
                                values=["(all)"])
        pool_box.pack(side="left", padx=4)

        ttk.Label(bar, text="Node:").pack(side="left", padx=(10, 0))
        node_var = tk.StringVar(value="(all)")
        node_box = ttk.Combobox(bar, textvariable=node_var, width=22, state="readonly",
                                values=["(all)"])
        node_box.pack(side="left", padx=4)

        ttk.Label(bar, text="Filter:").pack(side="left", padx=(10, 0))
        filt = tk.StringVar()
        ttk.Entry(bar, textvariable=filt, width=24).pack(side="left", padx=4)
        refresh_status = ttk.Label(bar, text="")

        def node_pool_map():
            """node name -> nodepool, from whatever nodes we've loaded."""
            return {n["node"]: n["pool"] for n in self.model["nodes"]}

        def pods_in_pool(pool):
            """The set of node names belonging to ``pool`` (or all if '(all)')."""
            npm = node_pool_map()
            if pool == "(all)":
                return None
            return {node for node, pl in npm.items() if pl == pool}

        def do_refresh():
            ns = ns_var.get()
            allns = ns == "(all)"

            def fetch():
                items = kubectl_collect.list_items(
                    "pods", namespace="" if allns else ns,
                    all_namespaces=allns, context=ctx())
                usage = usage_map_from_metrics(kubectl_collect.pod_metrics(ctx()))
                rows = [build_pod_row(
                    it, usage.get((it["metadata"]["namespace"], it["metadata"]["name"]),
                                  (None, None))) for it in items]
                return (ns, rows)

            def apply(data):
                scope, rows = data
                if scope == "(all)":
                    self.model["pods"] = rows
                else:
                    self.model["pods"] = [
                        p for p in self.model["pods"] if p["namespace"] != scope] + rows
                refill()
            self._scoped_refresh(fetch, apply, refresh_status)

        ttk.Button(bar, text="⟳ Refresh pods", command=do_refresh).pack(side="left", padx=6)
        refresh_status.pack(side="left", padx=6)
        cols = ("sel", "open", "namespace", "pod", "node", "nodepool", "ready", "phase",
                "restarts", "age", "cpu req", "cpu used", "mem req", "mem used",
                "no lim", "⚠ why")
        tree = self._make_tree(list_frame, cols,
                               [34, 40, 110, 200, 140, 120, 60, 80, 70, 60,
                                80, 80, 80, 80, 50, 180])
        tree.heading("sel", text="☑")
        tree.heading("open", text="⧉")
        tree.column("sel", anchor="center", stretch=False)
        tree.column("open", anchor="center", stretch=False)
        self._wire_cmd_preview(tree, "pods", "pod", "namespace")

        def sync_scopes():
            """Refresh the pool/node dropdown values from the current model,
            cascading the Node list to the selected pool. Preserves selections
            when still valid; otherwise falls back to '(all)'."""
            npm = node_pool_map()
            pools = ["(all)"] + sorted({p for p in npm.values() if p})
            pool_box["values"] = pools
            if pool_var.get() not in pools:
                pool_var.set("(all)")
            in_pool = pods_in_pool(pool_var.get())
            nodes = sorted({p["node"] for p in self.model["pods"] if p["node"]
                            and (in_pool is None or p["node"] in in_pool)})
            node_box["values"] = ["(all)"] + nodes
            if node_var.get() not in node_box["values"]:
                node_var.set("(all)")

        def refill(*_):
            ms.reset()
            for r in tree.get_children(""):
                tree.delete(r)
            sync_scopes()
            q = filt.get().lower()
            ns = ns_var.get()
            pool = pool_var.get()
            node = node_var.get()
            npm = node_pool_map()
            in_pool = pods_in_pool(pool)
            rows = sorted(self.model["pods"], key=lambda p: p["mem_req_b"], reverse=True)
            for p in rows:
                if ns != "(all)" and p["namespace"] != ns:
                    continue
                if in_pool is not None and p["node"] not in in_pool:
                    continue
                if node != "(all)" and p["node"] != node:
                    continue
                pool_name = npm.get(p["node"], "")
                hay = f'{p["namespace"]} {p["pod"]} {p["node"]} {pool_name}'.lower()
                if q and q not in hay:
                    continue
                tree.insert("", "end", tags=(p["severity"],), values=(
                    "☐", "⧉", p["namespace"], p["pod"], p["node"], pool_name,
                    p["ready"], p["phase"], p["restarts"], p["age"],
                    fmt_cpu(p["cpu_req_m"]), fmt_cpu(p["cpu_used_m"]),
                    fmt_mem(p["mem_req_b"]), fmt_mem(p["mem_used_b"]),
                    "⚠" if p["missing_limits"] else "", p["status_note"],
                ))

        def on_pool_change(*_):
            # Changing the pool resets the (now possibly stale) node choice.
            node_var.set("(all)")
            refill()

        ctx = lambda: self.context_var.get().strip()
        ms = MultiSelect(
            tree,
            lambda row: ResourceDetailWindow(self, "pod", tree.set(row, "pod"),
                                             tree.set(row, "namespace"), ctx()),
            lambda sel: open_inline_detail(frame, list_frame, "pod",
                                           tree.set(sel, "pod"),
                                           tree.set(sel, "namespace"), ctx()))
        ns_box.bind("<<ComboboxSelected>>", refill)
        pool_box.bind("<<ComboboxSelected>>", on_pool_change)
        node_box.bind("<<ComboboxSelected>>", refill)
        filt.trace_add("write", refill)
        refill()

        # The Nodepool/Node filters need the nodes model. Offline snapshots and a
        # whole-cluster refresh already load it, but a live session that only ever
        # scoped-refreshed pods wouldn't have it — so lazily pull nodes the first
        # time this tab is shown (once per context; skipped when offline).
        _nodes_probe = {"ctx": None}

        def ensure_nodes(*_):
            if self.model["nodes"]:
                return
            ctx0 = ctx()
            if not ctx0 or _nodes_probe["ctx"] == ctx0:
                return
            _nodes_probe["ctx"] = ctx0

            def fetch():
                nodes = kubectl_collect.list_items("nodes", context=ctx0)
                nu = self._node_usage_map(ctx0)
                return [build_node_row(n, nu.get(n["metadata"]["name"], (None, None)))
                        for n in nodes]

            def apply(rows):
                self.model["nodes"] = rows
                refill()
            self._scoped_refresh(fetch, apply, refresh_status)

        frame.bind("<Map>", ensure_nodes, add="+")

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

        ttk.Label(list_frame, text="Filter by Namespace, Nodepool, and/or Node (the Node list "
                  "narrows to the chosen pool), and/or type in Filter. Tick ☑ to select multiple "
                  "(or just click a row), then Delete. Double-click a pod for details / logs / "
                  "shell; click ⧉ for a new window. Red = failing, amber = running but restarted; "
                  "the “⚠ why” column says why. ⬇ CSV/XLSX exports exactly the rows shown.",
                  padding=4).pack(anchor="w")

    def _build_group_tab(self, group):
        """Build one grouped browser tab (Workloads/Networking/Config/Storage/
        Security) from RESOURCE_GROUPS[group] — see ResourceBrowser."""
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text=group)
        self._analysis_frames.append(frame)
        browser = ResourceBrowser(
            frame, lambda: self.context_var.get().strip(), RESOURCE_GROUPS[group])
        browser.pack(fill="both", expand=True)

    def _build_crd_tab(self):
        """Custom Resources: browse instances of any CRD the cluster has installed."""
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="Custom Resources")
        self._analysis_frames.append(frame)
        browser = CustomResourceBrowser(
            frame, lambda: self.context_var.get().strip())
        browser.pack(fill="both", expand=True)

    def _build_offenders_tab(self):
        """Resource Management: pods sorted worst-first by wasted memory
        (requested − actually used) — the over-reservation this tool exists to find."""
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
                "cpu req", "cpu used", "cpu WASTED")
        tree = self._make_tree(frame, cols, [110, 240, 150, 90, 90, 100, 80, 80, 90])
        self._wire_cmd_preview(tree, "pods", "pod", "namespace")
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
                cpu_waste = (p["cpu_req_m"] or 0) - (p["cpu_used_m"] or 0)
                tree.insert("", "end", values=(
                    p["namespace"], p["pod"], p["node"], fmt_mem(p["mem_req_b"]),
                    fmt_mem(p["mem_used_b"]), fmt_mem(waste),
                    fmt_cpu(p["cpu_req_m"]), fmt_cpu(p["cpu_used_m"]),
                    fmt_cpu(cpu_waste),
                ))
            empty.pack_forget()
            if not rows:
                empty.pack(anchor="w")
        refill()

    # -- Trends tab (time series) ------------------------------------------
    def _build_trends_tab(self):
        """Trends: sample deployment CPU/mem on an interval while the app runs and
        graph one line per deployment. This tab persists across data reloads."""
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
        """Start/stop periodic sampling. Start takes one sample now, then schedules
        the next on the chosen interval; stop cancels the pending timer."""
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
        """Take one trends sample off-thread (guarded so ticks can't overlap)."""
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
        """Drain a finished sample, append it to history, redraw, then reschedule."""
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
        """Refresh the Trends status line (sample count, time span, capture state)."""
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
        """Sync the Trends namespace dropdown to whatever history we've captured."""
        namespaces = trends.namespaces_in(self._history)
        self.trends_ns_box["values"] = namespaces
        if namespaces and self.trends_ns.get() not in namespaces:
            self.trends_ns.set(namespaces[0])

    def _trends_redraw(self):
        """Redraw the chart for the selected namespace/metric: one line per
        deployment, solid = actual usage, dashed = requested."""
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


# ---------------------------------------------------------------------------
# Resource detail inspector — the per-object view (Describe / YAML / Events, and
# for pods Logs + Shell, for secrets the decoder + cert inspector, for
# controllers/nodes their Pods). Plus the openssl helpers it uses for TLS certs.
# ---------------------------------------------------------------------------
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


_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
_PEM_CERT_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
    re.DOTALL)


def openssl_available():
    """True if an `openssl` binary is on PATH (the cert features shell out to it)."""
    return shutil.which("openssl") is not None


def openssl_missing_message():
    """Friendly, per-OS guidance shown when the openssl binary can't be found."""
    if sys.platform == "win32":
        how = ("Install it (e.g. `winget install ShiningLight.OpenSSL` or "
               "`choco install openssl`), or if you have Git for Windows it ships "
               "one at C:\\Program Files\\Git\\usr\\bin — add that to PATH.")
    elif sys.platform == "darwin":
        how = "Install it with `brew install openssl` and make sure it's on PATH."
    else:
        how = ("Install it with your package manager (e.g. `sudo apt install openssl` "
               "or `sudo dnf install openssl`).")
    return ("⚠ openssl was not found on your PATH.\n\n"
            "Certificate inspection shells out to the openssl command-line tool "
            "(no Python crypto library is used), so it needs that binary.\n\n"
            + how)


def _run_openssl(args, stdin_text=""):
    """
    Run `openssl <args>`, feeding stdin_text as the input PEM. -> (out, err).

    Never raises: a missing binary, a failed exec, or a timeout all come back as
    ("", <human message>) so callers can just display ``err``.
    """
    if not openssl_available():
        return "", openssl_missing_message()
    try:
        proc = subprocess.run(["openssl"] + args, input=stdin_text, text=True,
                              capture_output=True, timeout=20, creationflags=_NO_WINDOW)
    except FileNotFoundError as e:
        # PATH said openssl exists but exec failed (race, or a broken shim).
        return "", f"could not run openssl: {e}\n\n{openssl_missing_message()}"
    except subprocess.TimeoutExpired:
        return "", "openssl timed out after 20s."
    except OSError as e:  # permissions, bad executable, etc.
        return "", f"could not run openssl: {e}"
    return proc.stdout, proc.stderr.strip()


def _split_pem_certs(text):
    """Every PEM CERTIFICATE block in a bundle (leaf first, as stored)."""
    return _PEM_CERT_RE.findall(text or "")


def _is_cert_pem(text):
    return "-----BEGIN CERTIFICATE-----" in (text or "")


def _cert_chain_summary(pem_bundle):
    """
    Human summary of a cert bundle: per-cert subject/issuer/validity + expiry
    status, plus a naive linkage check (issuer[i] == subject[i+1]). Uses openssl
    to read each field so it works without any Python TLS libs.
    """
    certs = _split_pem_certs(pem_bundle)
    if not certs:
        return "No PEM CERTIFICATE blocks found."
    if not openssl_available():
        return openssl_missing_message()

    lines = [f"{len(certs)} certificate(s) in bundle (leaf first):\n"]
    subjects, issuers = [], []
    now = datetime.now(timezone.utc)
    for i, cert in enumerate(certs):
        out, err = _run_openssl(
            ["x509", "-noout", "-subject", "-issuer", "-dates",
             "-nameopt", "RFC2253"], cert)
        fields = {}
        for ln in out.splitlines():
            k, _, v = ln.partition("=")
            if _:
                fields[k.strip()] = v.strip()
        subj = fields.get("subject", "?")
        iss = fields.get("issuer", "?")
        subjects.append(subj)
        issuers.append(iss)
        expiry = fields.get("notAfter", "")
        exp_note = ""
        try:
            # openssl prints e.g. "Jun 11 12:00:00 2026 GMT"
            dt = datetime.strptime(expiry, "%b %d %H:%M:%S %Y %Z").replace(
                tzinfo=timezone.utc)
            days = (dt - now).days
            exp_note = (f"  ⚠ EXPIRED {-days}d ago" if days < 0
                        else f"  ({days}d left)" + (" ⚠ soon" if days < 30 else ""))
        except ValueError:
            pass
        lines.append(f"[{i}] {'leaf' if i == 0 else 'intermediate/root'}")
        lines.append(f"    subject: {subj}")
        lines.append(f"    issuer : {iss}")
        lines.append(f"    valid  : {fields.get('notBefore','?')}  ->  "
                     f"{expiry}{exp_note}")
        if err:
            lines.append(f"    (openssl: {err})")
        lines.append("")

    # Linkage: does each cert's issuer match the next cert's subject?
    lines.append("Chain linkage:")
    if len(certs) == 1:
        lines.append("  single cert — no intermediates bundled "
                     "(clients may need the issuer separately).")
    else:
        for i in range(len(certs) - 1):
            ok = issuers[i] == subjects[i + 1]
            lines.append(f"  [{i}]→[{i+1}] {'OK' if ok else '⚠ MISMATCH'}: "
                         f"issuer of [{i}] vs subject of [{i+1}]")
    if issuers and subjects and issuers[-1] == subjects[-1]:
        lines.append("  last cert is self-signed (root).")
    return "\n".join(lines)


def launch_in_terminal(argv, title="kubectl"):
    """
    Open a NEW terminal window running ``argv`` and return (ok, message).

    Used for interactive kubectl subcommands (edit) that need a real TTY + the
    user's editor. We don't capture output — the point is to hand control to the
    terminal exactly like running the command yourself. Best-effort per OS:
      - Windows: a new console via `start` + `cmd /k` (stays open to show result).
      - macOS:   Terminal.app via AppleScript `do script`.
      - Linux:   the first available terminal emulator that accepts `-e`.
    """
    try:
        if sys.platform == "win32":
            # `start "" cmd /k <argv>` — empty title arg, /k keeps the window up.
            subprocess.Popen(["cmd", "/c", "start", title, "cmd", "/k", *argv])
            return True, "opened a new console window"
        if sys.platform == "darwin":
            cmd = " ".join(shlex.quote(a) for a in argv)
            script = (f'tell application "Terminal" to do script "{cmd}"\n'
                      'tell application "Terminal" to activate')
            subprocess.Popen(["osascript", "-e", script])
            return True, "opened Terminal.app"
        # Linux / other unix: try known terminal emulators in order.
        cmd = " ".join(shlex.quote(a) for a in argv)
        for term in ("x-terminal-emulator", "gnome-terminal", "konsole",
                     "xfce4-terminal", "xterm"):
            if shutil.which(term):
                if term == "gnome-terminal":
                    subprocess.Popen([term, "--", "bash", "-lc", cmd])
                else:
                    subprocess.Popen([term, "-e", f"bash -lc {shlex.quote(cmd)}"])
                return True, f"opened {term}"
        return False, ("no terminal emulator found (tried x-terminal-emulator, "
                       "gnome-terminal, konsole, xfce4-terminal, xterm).")
    except OSError as e:
        return False, f"could not open a terminal: {e}"


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
        # secret-decoder plumbing
        self._secret_q: queue.Queue = queue.Queue()
        self._sec_data = {}           # key -> stored base64 string

        is_pod = kind in ("pod", "pods", "po")
        is_secret = kind in ("secret", "secrets")
        nsargs = ["-n", namespace] if namespace else []
        crumb = (f"{namespace} / " if namespace else "") + f"{kind}/{name}"

        header = ttk.Frame(self, padding=(4, 4, 4, 0))
        header.pack(fill="x")
        if on_back is not None:
            ttk.Button(header, text="← Back", command=on_back).pack(side="left")
        ttk.Label(header, text=f"  {crumb}", font=("", 10, "bold")).pack(side="left")
        # Live edit: hand off to `kubectl edit` in a real terminal (see _launch_edit).
        ttk.Button(header, text="✎ Edit (kubectl edit)",
                   command=self._launch_edit).pack(side="right")

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
        elif is_secret:
            self._build_secret_tab(nb)
        elif kind in _HAS_PODS:
            self._build_resource_pods_tab(nb)

        self._after_ids.append(self.after(100, self._poll_cmd))
        if is_secret:
            self._after_ids.append(self.after(120, self._poll_secret))
        # If the frame is destroyed out from under us (e.g. tab rebuild), clean up.
        self.bind("<Destroy>", lambda e: self.teardown() if e.widget is self else None)

    # -- live edit (hand off to `kubectl edit`) ----------------------------
    def _launch_edit(self):
        """
        Open `kubectl edit <kind>/<name>` in a new terminal window.

        Deliberately NOT a built-in YAML editor: kubectl edit already does the hard
        parts (server-side validation, optimistic concurrency, applying on save)
        using the user's own $KUBE_EDITOR/$EDITOR. We just launch it in a real
        terminal so that whole flow happens, triggered from the GUI.
        """
        if kubectl_collect.kubectl_path() is None:
            messagebox.showerror("kubectl not found",
                                 "kubectl must be on your PATH (or set KUBECTL) to edit.")
            return
        editor = os.environ.get("KUBE_EDITOR") or os.environ.get("EDITOR") \
            or ("notepad" if sys.platform == "win32" else "the system default")
        if not messagebox.askyesno(
                "Edit in terminal",
                f"Open `kubectl edit {self.kind}/{self.name}`"
                + (f" -n {self.namespace}" if self.namespace else "")
                + " in a new terminal window?\n\n"
                f"It will use your editor ({editor}). Saving in that editor applies "
                "the change to the cluster; quitting without saving cancels.\n\n"
                "Click ⟳ Refresh on a tab afterward to see the result."):
            return
        argv = kubectl_collect.edit_argv(
            self.kind, self.name, namespace=self.namespace, context=self.context)
        ok, msg = launch_in_terminal(argv, title=f"edit {self.kind}/{self.name}")
        if not ok:
            messagebox.showerror("Could not open a terminal", msg)

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

    # -- secret decoder ("Get secret") -------------------------------------
    def _build_secret_tab(self, nb):
        """
        A Secret-only tab: lists each data key with its value hidden by default,
        a decoder (Base64 first, extensible), plus reveal/copy per key. Kept
        separate from YAML so values aren't exposed until you ask for them.
        """
        frame = ttk.Frame(nb)
        nb.add(frame, text="Get secret")

        bar = ttk.Frame(frame, padding=4)
        bar.pack(fill="x")
        ttk.Label(bar, text="Decoder:").pack(side="left")
        self._sec_decoder = tk.StringVar(value="Base64")
        cb = ttk.Combobox(bar, textvariable=self._sec_decoder, width=14, state="readonly",
                          values=["Base64", "Raw (stored base64)"])
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self._render_secret())
        self._sec_reveal = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Reveal values", variable=self._sec_reveal,
                        command=self._render_secret).pack(side="left", padx=8)
        ttk.Button(bar, text="⟳ Refresh", command=self._load_secret).pack(side="left")
        self._sec_status = ttk.Label(bar, text="")
        self._sec_status.pack(side="left", padx=8)
        self._sec_type = ttk.Label(bar, text="", foreground="#555")
        self._sec_type.pack(side="right")

        # Scrollable body: one block per data key.
        wrap = ttk.Frame(frame)
        wrap.pack(fill="both", expand=True)
        canvas = tk.Canvas(wrap, highlightthickness=0)
        vs = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        self._sec_body = ttk.Frame(canvas)
        self._sec_body.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        self._sec_win = canvas.create_window((0, 0), window=self._sec_body, anchor="nw")
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(self._sec_win, width=e.width))
        canvas.configure(yscrollcommand=vs.set)
        canvas.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        self._load_secret()

    def _load_secret(self):
        self._sec_status.config(text="⏳ loading…")
        args = ["get", self.kind, self.name] \
            + (["-n", self.namespace] if self.namespace else []) + ["-o", "json"]

        def work():
            try:
                out = kubectl_collect.run_text(args, context=self.context)
                self._secret_q.put((json.loads(out), None))
            except Exception as e:  # noqa: BLE001 — surfaced in the tab
                self._secret_q.put((None, e))

        threading.Thread(target=work, daemon=True).start()

    def _poll_secret(self):
        if self._torn:
            return
        try:
            while True:
                obj, err = self._secret_q.get_nowait()
                if err is not None:
                    self._sec_status.config(text=f"⚠ {err}")
                    self._sec_data = {}
                else:
                    self._sec_data = obj.get("data", {}) or {}
                    self._sec_type.config(text=f"type: {obj.get('type', '')}")
                    self._sec_status.config(
                        text=f"{len(self._sec_data)} keys · "
                             f"loaded {time.strftime('%H:%M:%S')}")
                self._render_secret()
        except queue.Empty:
            pass
        self._after_ids.append(self.after(200, self._poll_secret))

    def _decode_secret(self, b64):
        """Apply the selected decoder to one stored (base64) value -> display text."""
        if self._sec_decoder.get().startswith("Raw"):
            return b64
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            return "⚠ not valid base64"
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return f"⟪binary: {len(raw)} bytes⟫\n{raw.hex()}"

    def _decoded_text(self, b64):
        """base64 -> utf-8 text, or '' if it isn't decodable text (e.g. a DER blob)."""
        try:
            return base64.b64decode(b64, validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return ""

    def _copy_secret(self, b64):
        self.clipboard_clear()
        self.clipboard_append(self._decode_secret(b64))
        self._sec_status.config(text="copied to clipboard")

    # openssl views offered for a cert; label -> openssl args (PEM fed on stdin).
    _CERT_VIEWS = {
        "Chain summary": None,          # special-cased (Python + openssl per cert)
        "Full text": ["x509", "-noout", "-text"],
        "Dates (expiry)": ["x509", "-noout", "-dates"],
        "Subject / Issuer": ["x509", "-noout", "-subject", "-issuer",
                             "-nameopt", "RFC2253"],
        "Subject Alt Names": ["x509", "-noout", "-ext", "subjectAltName"],
        "Fingerprint (SHA-256)": ["x509", "-noout", "-fingerprint", "-sha256"],
        "Serial": ["x509", "-noout", "-serial"],
    }

    def _inspect_cert(self, key, pem):
        """Open a window of openssl views over a cert (or bundle) from a secret key."""
        win = tk.Toplevel(self)
        win.title(f"TLS cert — {self.name}/{key}")
        win.geometry("820x620")

        bar = ttk.Frame(win, padding=4)
        bar.pack(fill="x")
        ttk.Label(bar, text="View:").pack(side="left")
        view_var = tk.StringVar(value="Chain summary")
        cb = ttk.Combobox(bar, textvariable=view_var, width=22, state="readonly",
                          values=list(self._CERT_VIEWS))
        cb.pack(side="left", padx=4)
        refresh_btn = ttk.Button(bar, text="⟳ Refresh")
        refresh_btn.pack(side="left")
        status = ttk.Label(bar, text="")
        status.pack(side="left", padx=8)

        wrap = ttk.Frame(win)
        wrap.pack(fill="both", expand=True)
        txt = tk.Text(wrap, wrap="none", font=("Consolas", 9),
                      background="#111", foreground="#ddd")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=txt.yview)
        hs = ttk.Scrollbar(win, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        txt.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        hs.pack(fill="x")

        def render(*_):
            view = view_var.get()
            txt.delete("1.0", "end")
            if view == "Chain summary":
                out = _cert_chain_summary(pem)
            else:
                # x509 reads the first cert on stdin; that's the leaf.
                out, err = _run_openssl(self._CERT_VIEWS[view], pem)
                if err and not out:
                    out = err
            txt.insert("1.0", out)
            status.config(text=f"{len(_split_pem_certs(pem))} cert(s) in this key")

        # Without openssl there's nothing these controls can do — disable them and
        # show the install guidance up front instead of letting the user click into
        # dead buttons.
        if not openssl_available():
            cb.state(["disabled"])
            refresh_btn.state(["disabled"])
            status.config(text="⚠ openssl not on PATH")
            txt.insert("1.0", openssl_missing_message())
            return

        cb.bind("<<ComboboxSelected>>", render)
        refresh_btn.config(command=render)
        render()

    def _render_secret(self):
        if self._torn or not hasattr(self, "_sec_body"):
            return
        for w in self._sec_body.winfo_children():
            w.destroy()
        if not self._sec_data:
            ttk.Label(self._sec_body, text="(no data keys)",
                      padding=8).pack(anchor="w")
            return
        reveal = self._sec_reveal.get()
        for key in sorted(self._sec_data):
            b64 = self._sec_data[key]
            block = ttk.Frame(self._sec_body, padding=(8, 6))
            block.pack(fill="x", expand=True)
            head = ttk.Frame(block)
            head.pack(fill="x")
            ttk.Label(head, text=key, font=("", 10, "bold")).pack(side="left")
            ttk.Button(head, text="Copy", width=6,
                       command=lambda v=b64: self._copy_secret(v)).pack(side="right")
            # Offer openssl inspection when the value is a PEM certificate.
            pem = self._decoded_text(b64)
            if _is_cert_pem(pem):
                ttk.Button(head, text="🔎 Inspect cert",
                           command=lambda k=key, p=pem: self._inspect_cert(k, p)
                           ).pack(side="right", padx=4)
            value = self._decode_secret(b64) if reveal else "•" * 24 + "  (hidden)"
            lines = value.count("\n") + 1
            txt = tk.Text(block, height=min(max(lines, 1), 12), wrap="char",
                          font=("Consolas", 9), background="#111", foreground="#ddd")
            txt.insert("1.0", value)
            txt.configure(state="disabled")
            txt.pack(fill="x", expand=True, pady=(2, 0))
            ttk.Separator(self._sec_body, orient="horizontal").pack(fill="x")

    # -- live logs ---------------------------------------------------------
    def _build_logs_tab(self, nb):
        """Pods only: a live `kubectl logs -f` stream with pause/restart/clear
        and auto-scroll. A reader thread feeds _log_q; _pump_logs drains it."""
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
        """Spawn `kubectl logs -f` and a reader thread that pushes lines to _log_q."""
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
        """UI-thread timer: move buffered log lines into the Text widget (unless
        paused) and auto-scroll. Reschedules itself every 200ms."""
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
        """Stop the current stream, clear the pane and queue, and start fresh."""
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
        """For controllers/nodes: a table of the pods they own/host (see pods_for),
        each double-clickable to open its own detail window."""
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
        """Pods only: a line-oriented pipe terminal via `kubectl exec -i -- <shell>`.
        Each shell (sh/bash) gets its own ``st`` state so they don't collide."""
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
        """(Re)connect the shell: spawn `kubectl exec -i` and a reader thread."""
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
        """Write one entered command line to the shell's stdin (echoing it)."""
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
        """UI-thread timer: flush this shell's buffered output into its Text pane."""
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
        """Best-effort terminate() of a child process (logs/exec) if still running."""
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass

    def teardown(self):
        """Cancel every scheduled timer and kill every child process we own.

        Called when the view is destroyed (window closed or tabs rebuilt) so no
        orphaned kubectl logs/exec processes or after() callbacks leak.
        """
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


# ---------------------------------------------------------------------------
# Row interaction — the shared click model for every table: ⧉ opens a detail
# window, double-click opens it in-place, and ☑ multi-select for bulk actions.
# ---------------------------------------------------------------------------
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
                 scalable=False, restartable=False, deletable=True,
                 ns_by_subject=False):
        self.label = label
        self.ktype = ktype            # kubectl resource type (e.g. "deployments")
        self.namespaced = namespaced
        self.columns = columns        # list of (header, width)
        self.row = row                # fn(item) -> list[str], len == len(columns)
        self.scalable = scalable
        self.restartable = restartable
        self.deletable = deletable
        # Cluster-scoped kinds (e.g. ClusterRoleBindings) that can still be filtered
        # by the namespace of their .subjects, client-side.
        self.ns_by_subject = ns_by_subject


# Row renderers: one `_<kind>_row(item)` per resource type. Each takes a single
# kubectl item dict and returns the list of cell strings for that Kind's table,
# in the SAME order as the Kind's `columns` tuple (see RESOURCE_GROUPS below).
# Keep the two in sync when adding/removing a column.
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


def _sa_row(it):
    # secrets + imagePullSecrets are separate lists; automount defaults to true.
    n_secrets = len(it.get("secrets", []) or [])
    automount = it.get("automountServiceAccountToken")
    automount_s = "true" if automount is None else str(bool(automount)).lower()
    return [_meta(it)["name"], str(n_secrets), automount_s, _row_age(it)]


def _rules_summary(rules):
    """Compact 'N rules' with a ⚠ marker when any rule uses a wildcard verb/resource."""
    rules = rules or []
    wild = any("*" in (r.get("verbs") or []) or "*" in (r.get("resources") or [])
               for r in rules)
    return f"{len(rules)}{' ⚠*' if wild else ''}"


def _role_row(it):
    return [_meta(it)["name"], _rules_summary(it.get("rules")), _row_age(it)]


def _subjects_summary(subjects):
    """e.g. 'ServiceAccount:default/foo, User:alice' (truncated)."""
    parts = []
    for s in subjects or []:
        ns = s.get("namespace")
        nm = s.get("name", "?")
        parts.append(f'{s.get("kind","?")}:{ns + "/" if ns else ""}{nm}')
    text = ", ".join(parts) if parts else "—"
    return text if len(text) <= 60 else text[:57] + "…"


def _rolebinding_row(it):
    ref = it.get("roleRef", {})
    return [_meta(it)["name"], f'{ref.get("kind","?")}/{ref.get("name","?")}',
            _subjects_summary(it.get("subjects")), _row_age(it)]


# The registry that drives the grouped browser tabs. Each key becomes a sidebar
# tab (Workloads/Networking/Config/Storage/Security), and its list of Kind()s
# populates that tab's "Type" dropdown. To expose a new resource type, add a Kind
# here (and a matching `_<kind>_row` renderer) — no other wiring needed.
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
    "Security / RBAC": [
        Kind("ServiceAccounts", "serviceaccounts", True,
             [("name", 300), ("secrets", 80), ("automount", 90), ("age", 80)], _sa_row),
        Kind("Roles", "roles", True,
             [("name", 340), ("rules", 90), ("age", 80)], _role_row),
        Kind("RoleBindings", "rolebindings", True,
             [("name", 280), ("role ref", 200), ("subjects", 320), ("age", 80)],
             _rolebinding_row),
        Kind("ClusterRoles", "clusterroles", False,
             [("name", 380), ("rules", 90), ("age", 80)], _role_row),
        Kind("ClusterRoleBindings", "clusterrolebindings", False,
             [("name", 300), ("role ref", 200), ("subjects", 320), ("age", 80)],
             _rolebinding_row, ns_by_subject=True),
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
        self._rows = {}               # tree iid -> (Kind, name, namespace)
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
        self.kind_var = tk.StringVar(value=kinds[0].label if kinds else "")
        self.kind_box = ttk.Combobox(bar, textvariable=self.kind_var, width=30,
                                     state="readonly", values=[k.label for k in kinds])
        self.kind_box.pack(side="left", padx=4)
        self.kind_box.bind("<<ComboboxSelected>>", lambda e: self._reload())

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
        # Export the current view (respects the Type/Namespace filters on screen).
        ttk.Button(act, text="⬇ XLSX", command=lambda: self._export("xlsx")).pack(side="right")
        ttk.Button(act, text="⬇ CSV", command=lambda: self._export("csv")
                   ).pack(side="right", padx=(4, 0))
        ttk.Label(act, text="Export:").pack(side="right", padx=(0, 4))

        self._table = ttk.Frame(self._list)
        self._table.pack(fill="both", expand=True)

        self.after(120, self._poll)
        if kinds:
            self._reload()

    def set_kinds(self, kinds):
        """Replace the browsable types at runtime (used for discovered CRDs)."""
        self._kinds = {k.label: k for k in kinds}
        self.kind_box["values"] = [k.label for k in kinds]
        if kinds and self.kind_var.get() not in self._kinds:
            self.kind_var.set(kinds[0].label)
        if kinds:
            self._reload()

    def _cur_kind(self):
        """The Kind currently selected in the Type dropdown."""
        return self._kinds[self.kind_var.get()]

    def _item_kind(self, kind, it):
        """The Kind a single listed item belongs to.

        For ordinary single-type tables this is just ``kind``; aggregate views
        that merge several kinds override this to return each row's real kind so
        open/preview/delete target the right resource type.
        """
        return kind

    def _export(self, fmt):
        """Export the current browser table (current Type/Namespace view)."""
        if self._tree is None or not self._tree.get_children(""):
            messagebox.showinfo("Export", "Nothing to export yet — load a resource type first.")
            return
        export_tree(self._tree, fmt,
                    status_cb=lambda t: self.status.config(text=t))

    def _reload(self):
        """
        Re-list the selected type off-thread and enable the action buttons/namespace
        box appropriate to it. Namespaced kinds scope the query with -n/-A; cluster-
        scoped kinds list everything (ns_by_subject ones are filtered later in _fill).
        """
        if not self._alive:
            return
        kind = self._cur_kind()
        can_ns_filter = kind.namespaced or kind.ns_by_subject
        self.ns_box.state(["!disabled"] if can_ns_filter else ["disabled"])
        self.scale_btn.state(["!disabled"] if kind.scalable else ["disabled"])
        self.restart_btn.state(["!disabled"] if kind.restartable else ["disabled"])
        self.delete_btn.state(["!disabled"] if kind.deletable else ["disabled"])
        self.status.config(text="⏳ loading…")
        ctx = self._get_context()
        ns = self.ns_var.get()
        # Cluster-scoped kinds always list everything; ns_by_subject kinds are
        # filtered client-side in _fill. Only truly namespaced kinds scope the query.
        want_ns = ns if (kind.namespaced and ns != "(all)") else ""
        all_ns = kind.namespaced and ns == "(all)"
        need_ns_list = can_ns_filter and self._namespaces == ["(all)"]

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
        """UI-thread timer draining the worker queue: fill the table, refresh the
        namespace list, or show an error/action-result status."""
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
        """Rebuild the table for ``items``: a leading ⧉ open column, an optional
        namespace column, then the Kind's own columns; wire row open actions."""
        for w in self._table.winfo_children():
            w.destroy()
        self._rows = {}

        # Cluster-scoped bindings: filter client-side by the namespace of a subject.
        ns_sel = self.ns_var.get()
        if kind.ns_by_subject and ns_sel != "(all)":
            items = [it for it in items
                     if any(s.get("namespace") == ns_sel
                            for s in (it.get("subjects") or []))]
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
            # Store the row's OWN kind (usually ``kind``; may differ in an
            # aggregate view that merges several kinds — see _item_kind).
            self._rows[iid] = (self._item_kind(kind, it), _meta(it).get("name", ""), ns)
        self.status.config(text=f"{len(items)} {kind.label.lower()}")

        wire_row_actions(tree, self._open_window_for, self._open_inplace_for)
        tree.bind("<<TreeviewSelect>>", lambda e: self._preview_command(), add="+")

        # Right-click to export the current view (matches the toolbar buttons).
        menu = tk.Menu(tree, tearoff=0)
        menu.add_command(label="Export to CSV…", command=lambda: self._export("csv"))
        menu.add_command(label="Export to Excel (XLSX)…", command=lambda: self._export("xlsx"))
        tree.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))
        tree.bind("<Button-2>", lambda e: menu.tk_popup(e.x_root, e.y_root))
        self._export_menu = menu   # keep a ref so it isn't garbage-collected

    def _preview_command(self):
        """Show `kubectl get <type> <name> …` for the highlighted row in the app's
        bottom command bar (found via the toplevel), if it exposes show_command."""
        iid = self._tree.focus() if self._tree else None
        if not iid or iid not in self._rows:
            return
        top = self.winfo_toplevel()
        if not hasattr(top, "show_command"):
            return
        kind, name, ns = self._rows[iid]
        argv = kubectl_collect.get_argv(
            kind.ktype, name, namespace=ns, context=self._get_context())
        top.show_command(argv, source="selected")

    def _row_target(self, iid):
        """(Kind, name, namespace) for a row iid, or None if it's unknown."""
        if iid not in self._rows:
            return None
        kind, name, ns = self._rows[iid]
        return kind, name, ns

    def _open_window_for(self, iid):
        """Open a row's detail in a separate window (the ⧉ column action)."""
        t = self._row_target(iid)
        if t:
            kind, name, ns = t
            ResourceDetailWindow(self, kind.ktype, name, ns, self._get_context())

    def _open_inplace_for(self, iid):
        """Open a row's detail in-place, hiding the list (double-click action)."""
        t = self._row_target(iid)
        if not t:
            return
        kind, name, ns = t
        self._detail = open_inline_detail(
            self, self._list, kind.ktype, name, ns, self._get_context())

    def _selection(self):
        """(Kind, name, namespace) for the focused row, prompting if none is selected."""
        if not self._tree:
            return None
        iid = self._tree.focus()
        if not iid or iid not in self._rows:
            messagebox.showinfo("No selection", "Select a row first.")
            return None
        kind, name, ns = self._rows[iid]
        return kind, name, ns

    def _do_scale(self):
        """Prompt for a replica count and scale the selected workload."""
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
        """Confirm, then rollout-restart the selected workload."""
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
        """Confirm (with a warning), then delete the selected resource."""
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
        """Run a mutating action off-thread, report via the queue, then reload."""
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


def _status_hint(it):
    """
    Best-effort state for a CRD that declares no additionalPrinterColumns: peek at
    the common status conventions (Ready condition, phase, Argo health).
    """
    st = it.get("status", {}) or {}
    conds = st.get("conditions")
    if isinstance(conds, list) and conds:
        ready = next((c for c in conds if c.get("type") == "Ready"), None)
        c = ready or conds[-1]
        return f'{c.get("type","")}={c.get("status","")}'.strip("=")
    if isinstance(st.get("phase"), str):
        return st["phase"]
    if isinstance(st.get("health"), dict):
        return st["health"].get("status", "")
    return ""


# A single JSONPath step: a key optionally followed by [N], [*], or [?(@.k=="v")].
_JP_STEP = re.compile(
    r'([^.\[]*)'                                        # key (may be empty)
    r'(?:\[(?:(\d+)|(\*)|\?\(@\.([^=]+)==["\']([^"\']*)["\']\))\])?')


def _jp_split(path):
    """Split a JSONPath on '.' but never inside [...] (filters contain '@.type')."""
    steps, buf, depth = [], [], 0
    for ch in path.lstrip('.'):
        if ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
        if ch == '.' and depth == 0:
            steps.append(''.join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        steps.append(''.join(buf))
    return steps


def _jsonpath(obj, path):
    """
    Evaluate a Kubernetes printer-column JSONPath (a restricted subset) against a
    resource dict and return a display string.

    Supports the forms that actually appear in additionalPrinterColumns: dotted
    keys, array index [0], wildcard [*] (joined by ','), and the filter
    [?(@.type=="Ready")] used by cert-manager & friends. Anything it can't parse
    yields "" rather than raising.
    """
    cur = [obj]
    for raw in _jp_split(path):
        if not raw:
            continue
        m = _JP_STEP.fullmatch(raw)
        if not m:
            return ""
        key, idx, star, fkey, fval = m.groups()
        nxt = []
        for node in cur:
            val = node.get(key) if (key and isinstance(node, dict)) else node
            if val is None:
                continue
            if idx is not None:
                if isinstance(val, list) and len(val) > int(idx):
                    nxt.append(val[int(idx)])
            elif star is not None:
                nxt.extend(val if isinstance(val, list) else [])
            elif fkey is not None:
                for e in (val if isinstance(val, list) else []):
                    if isinstance(e, dict) and str(e.get(fkey.strip())) == fval:
                        nxt.append(e)
            else:
                nxt.append(val)
        cur = nxt
        if not cur:
            return ""
    parts = [str(v) for v in cur if v is not None and not isinstance(v, (dict, list))]
    return ",".join(parts)


def _kind_from_crd(crd):
    """
    Build a browsable Kind from a discovered CRD dict (see list_crds).

    Columns come from the CRD's own additionalPrinterColumns so each object shows
    its author-defined state (STATE/READY/VALID/etc.) exactly like `kubectl get`.
    CRDs that declare none fall back to a generic 'status' hint column.
    """
    label = f'{crd["kind"]}  ({crd["group"]})' if crd["group"] else crd["kind"]
    namespaced = crd["scope"] == "Namespaced"
    # Only priority-0 columns show in the default table (priority>0 == -o wide).
    pcols = [c for c in (crd.get("printerColumns") or []) if c.get("priority", 0) == 0]

    if pcols:
        columns = [("name", 300)]
        for c in pcols:
            columns.append((c["name"].lower(), 160))
        columns.append(("age", 80))

        def row(it, _pcols=pcols):
            vals = [_meta(it)["name"]]
            for c in _pcols:
                raw = _jsonpath(it, c["jsonPath"])
                if c.get("type") == "date" and raw:
                    raw = age_from(raw)      # render timestamps as an age
                vals.append(raw)
            vals.append(_row_age(it))
            return vals
    else:
        columns = [("name", 360), ("status", 220), ("age", 80)]

        def row(it):
            return [_meta(it)["name"], _status_hint(it), _row_age(it)]

    k = Kind(label, crd["fq"], namespaced, columns, row)
    # Extra metadata so the custom-resource browser can group by API group and
    # drill into a single kind (label stays globally unique for the _kinds map).
    k.group = crd["group"] or ""
    k.crd_kind = crd["kind"]
    return k


# Sentinel shown in the Kind dropdown to list every kind in the chosen group.
_ALL_KINDS = "(all kinds)"
_ALL_GROUPS = "(all groups)"


class CustomResourceBrowser(ResourceBrowser):
    """
    A ResourceBrowser whose types are the cluster's CRDs, discovered at runtime.

    This is how non-standard, operator-installed resources (cert-manager
    Certificates, Argo Applications, NGINX VirtualServers/TransportServers, etc.)
    become browsable — the app doesn't hardcode them, it asks the cluster what
    CRDs exist and offers each as a Type. A "⟳ Reload CRDs" button re-discovers
    after installing/removing an operator.
    """

    def __init__(self, parent, get_context):
        super().__init__(parent, get_context, [])  # no kinds until discovered
        self._crd_q: queue.Queue = queue.Queue()
        self._crd_kinds: list = []            # every discovered CRD as a Kind
        self._visible: dict = {}              # Kind-dropdown label -> Kind (current group)

        bar = self.kind_box.master
        # A "Group" (API group) filter to the LEFT of the existing Type dropdown.
        # Picking a group narrows the Type list to that group's kinds and offers
        # an "(all kinds)" aggregate view of everything the group deploys.
        first = bar.winfo_children()[0]       # the existing "Type:" label
        self.group_lbl = ttk.Label(bar, text="Group:")
        self.group_var = tk.StringVar(value=_ALL_GROUPS)
        self.group_box = ttk.Combobox(bar, textvariable=self.group_var, width=26,
                                      state="readonly", values=[_ALL_GROUPS])
        self.group_lbl.pack(side="left", before=first)
        self.group_box.pack(side="left", padx=4, before=first)
        self.group_box.bind("<<ComboboxSelected>>", lambda e: self._on_group_change())
        # The base's "Type:" label now reads "Kind:" — it selects the kind/CR.
        first.config(text="Kind:")

        # Add a "Reload CRDs" button into the existing Type/Namespace bar row.
        ttk.Button(bar, text="⟳ Reload CRDs",
                   command=self._discover).pack(side="left", padx=(2, 8))
        self.status.config(text="Discovering CRDs…")
        self.after(150, self._poll_crds)
        self._discover()

    # -- group / kind selection -------------------------------------------
    def _on_group_change(self):
        """Repopulate the Kind dropdown for the selected group and reload.

        '(all groups)' shows every kind flat (the original behaviour). A specific
        group lists its kinds plus an '(all kinds)' aggregate, defaulting to the
        aggregate so you see everything the group deploys at a glance.
        """
        group = self.group_var.get()
        if group == _ALL_GROUPS:
            kinds = sorted(self._crd_kinds, key=lambda k: k.label)
            self._visible = {k.label: k for k in kinds}
            values = [k.label for k in kinds]
            self.kind_var.set(values[0] if values else "")
        else:
            kinds = sorted((k for k in self._crd_kinds if k.group == group),
                           key=lambda k: k.crd_kind)
            self._visible = {k.crd_kind: k for k in kinds}
            values = [_ALL_KINDS] + [k.crd_kind for k in kinds]
            self.kind_var.set(_ALL_KINDS)
        self.kind_box["values"] = values
        self._reload()

    def _aggregate_kind(self, group):
        """A synthetic Kind that merges every kind in ``group`` into one table."""
        members = [k for k in self._crd_kinds if k.group == group]
        cols = [("kind", 180), ("name", 300), ("status", 220), ("age", 80)]

        def row(it):
            mk = it.get("_agg_kind")
            return [mk.crd_kind if mk else it.get("kind", ""),
                    _meta(it)["name"], _status_hint(it), _row_age(it)]

        ak = Kind(f"all {group}", "", True, cols, row, deletable=False)
        ak.group = group
        ak.crd_kind = _ALL_KINDS
        ak.is_aggregate = True
        ak.member_kinds = members
        return ak

    def _cur_kind(self):
        """The Kind selected via Group + Kind (may be the aggregate synthetic)."""
        name = self.kind_var.get()
        if name == _ALL_KINDS:
            return self._aggregate_kind(self.group_var.get())
        return self._visible.get(name)

    def _item_kind(self, kind, it):
        """In an aggregate table each row carries its own tagged member kind."""
        return it.get("_agg_kind") or kind

    def _reload(self):
        """List the current selection; multi-list & merge in aggregate mode."""
        if not self._alive:
            return
        kind = self._cur_kind()
        if kind is None:
            self.status.config(text="Pick a group / kind.")
            return
        if not getattr(kind, "is_aggregate", False):
            super()._reload()
            return

        # Aggregate: list every member kind and merge. Bulk actions don't apply
        # to a mixed table, so disable them; the namespace filter still does.
        self.ns_box.state(["!disabled"])
        for b in (self.scale_btn, self.restart_btn, self.delete_btn):
            b.state(["disabled"])
        self.status.config(text=f"⏳ loading {len(kind.member_kinds)} kinds…")
        ctx = self._get_context()
        ns = self.ns_var.get()
        need_ns_list = self._namespaces == ["(all)"]

        def work():
            merged, errors = [], []
            for mk in kind.member_kinds:
                want_ns = ns if (mk.namespaced and ns != "(all)") else ""
                all_ns = mk.namespaced and ns == "(all)"
                try:
                    items = kubectl_collect.list_items(
                        mk.ktype, namespace=want_ns, all_namespaces=all_ns, context=ctx)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{mk.crd_kind}: {e}")
                    continue
                for it in items:
                    it["_agg_kind"] = mk
                merged.extend(items)
            nslist = None
            if need_ns_list:
                try:
                    nslist = kubectl_collect.list_namespaces(ctx)
                except Exception:  # noqa: BLE001
                    nslist = None
            self._q.put(("ok", kind, merged, nslist))
            if errors:
                self._q.put(("err", kind,
                             f"{len(errors)} kind(s) unavailable: " + "; ".join(errors[:3]),
                             None))

        threading.Thread(target=work, daemon=True).start()

    def _discover(self):
        """Ask the cluster for its CRDs off-thread (result handled in _poll_crds)."""
        ctx = self._get_context()
        self.status.config(text="⏳ discovering CRDs…")

        def work():
            try:
                crds = kubectl_collect.list_crds(ctx)
                self._crd_q.put(("ok", crds))
            except Exception as e:  # noqa: BLE001
                self._crd_q.put(("err", e))

        threading.Thread(target=work, daemon=True).start()

    def _poll_crds(self):
        """Drain discovery results and repopulate the Type dropdown from the CRDs."""
        if not self._alive:
            return
        try:
            while True:
                status, payload = self._crd_q.get_nowait()
                if status == "err":
                    self.status.config(text=f"⚠ {payload}")
                elif not payload:
                    self.status.config(text="No CRDs found in this cluster.")
                else:
                    self._apply_crds([_kind_from_crd(c) for c in payload])
        except queue.Empty:
            pass
        self.after(300, self._poll_crds)

    def _apply_crds(self, kinds):
        """Store discovered CRD kinds and (re)populate the Group dropdown."""
        self._crd_kinds = kinds
        self._kinds = {k.label: k for k in kinds}   # keep base map consistent
        groups = sorted({k.group for k in kinds if k.group})
        self.group_box["values"] = [_ALL_GROUPS] + groups
        if self.group_var.get() not in self.group_box["values"]:
            self.group_var.set(_ALL_GROUPS)
        self.status.config(
            text=f"{len(kinds)} CRD types across {len(groups)} group(s)")
        self._on_group_change()


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "data"
    Dashboard(folder).mainloop()


if __name__ == "__main__":
    main()
