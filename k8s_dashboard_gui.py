"""
K8s Resource Dashboard — native desktop window, standard library ONLY.

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

import json
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import kubectl_collect

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
        "pods": _load(folder, "pods.json"),
        "nodes": _load(folder, "nodes.json"),
        "pod_metrics": _load(folder, "pod-metrics.json"),
        "node_metrics": _load(folder, "node-metrics.json"),
    })


def build_model_from_raw(raw: dict) -> dict:
    """Build the model from the five parsed kubectl JSON objects (live or cached)."""
    deployments = raw.get("deployments") or {}
    pods = raw.get("pods") or {}
    nodes = raw.get("nodes") or {}
    pod_metrics = raw.get("pod_metrics") or {}
    node_metrics = raw.get("node_metrics") or {}

    pod_usage = {}
    for it in pod_metrics.get("items", []):
        ns, name = it["metadata"]["namespace"], it["metadata"]["name"]
        cpu = sum(cpu_to_milli(c.get("usage", {}).get("cpu")) for c in it.get("containers", []))
        mem = sum(mem_to_bytes(c.get("usage", {}).get("memory")) for c in it.get("containers", []))
        pod_usage[(ns, name)] = (cpu, mem)

    node_usage = {}
    for it in node_metrics.get("items", []):
        node_usage[it["metadata"]["name"]] = (
            cpu_to_milli(it.get("usage", {}).get("cpu")),
            mem_to_bytes(it.get("usage", {}).get("memory")),
        )

    node_rows = []
    for n in nodes.get("items", []):
        name = n["metadata"]["name"]
        alloc = n["status"].get("allocatable", {})
        ready = next((c["status"] for c in n["status"].get("conditions", []) if c["type"] == "Ready"), "?")
        u = node_usage.get(name, (None, None))
        node_rows.append({
            "node": name, "ready": ready,
            "cpu_alloc_m": cpu_to_milli(alloc.get("cpu")),
            "mem_alloc_b": mem_to_bytes(alloc.get("memory")),
            "cpu_used_m": u[0], "mem_used_b": u[1],
        })

    pod_rows = []
    for p in pods.get("items", []):
        ns, name = p["metadata"]["namespace"], p["metadata"]["name"]
        spec = p.get("spec", {})
        node = spec.get("nodeName", "<unscheduled>")
        phase = p.get("status", {}).get("phase", "?")
        owner = next((f'{o["kind"]}/{o["name"]}' for o in p["metadata"].get("ownerReferences", [])), "-")
        cpu_req = cpu_lim = mem_req = mem_lim = 0.0
        missing = 0
        conts = spec.get("containers", [])
        for c in conts:
            res = c.get("resources", {})
            req, lim = res.get("requests", {}), res.get("limits", {})
            cpu_req += cpu_to_milli(req.get("cpu"))
            mem_req += mem_to_bytes(req.get("memory"))
            cpu_lim += cpu_to_milli(lim.get("cpu"))
            mem_lim += mem_to_bytes(lim.get("memory"))
            if not lim.get("cpu") or not lim.get("memory"):
                missing += 1
        u = pod_usage.get((ns, name), (None, None))
        pod_rows.append({
            "namespace": ns, "pod": name, "node": node, "phase": phase, "owner": owner,
            "cpu_req_m": cpu_req, "cpu_lim_m": cpu_lim, "mem_req_b": mem_req, "mem_lim_b": mem_lim,
            "cpu_used_m": u[0], "mem_used_b": u[1], "missing_limits": missing,
        })

    dep_rows = []
    for d in deployments.get("items", []):
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
        dep_rows.append({
            "namespace": ns, "deployment": name, "replicas": replicas, "ready": ready,
            "cpu_req_m": cpu_req, "cpu_lim_m": cpu_lim, "mem_req_b": mem_req, "mem_lim_b": mem_lim,
            "cpu_req_total_m": cpu_req * replicas, "mem_req_total_b": mem_req * replicas,
            "missing": ", ".join(missing),
        })

    return {"nodes": node_rows, "pods": pod_rows, "deployments": dep_rows}


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class Dashboard(tk.Tk):
    def __init__(self, folder: Path):
        super().__init__()
        self.title("☸ K8s Resource Dashboard")
        self.geometry("1200x760")
        self.folder = folder
        self.model = {}
        self._result_q: queue.Queue = queue.Queue()
        self._busy = False

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

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=6, pady=6)

        self.load_contexts()
        # Show cached files at startup if present; otherwise prompt to refresh.
        if (folder / "pods.json").exists():
            self.reload()
        elif kubectl_collect.kubectl_path():
            self.status.config(text="Pick a context and hit “Refresh from cluster”.")
        else:
            self.status.config(text="⚠ kubectl not found on PATH — load JSON files, "
                                     "or install kubectl / set the KUBECTL env var.")

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
            "deployments": "deployments.json", "pods": "pods.json", "nodes": "nodes.json",
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
        for tab in self.nb.tabs():
            self.nb.forget(tab)
        self._build_nodes_tab()
        self._build_deploys_tab()
        self._build_pods_tab()
        self._build_offenders_tab()

    # -- helpers -----------------------------------------------------------
    def _make_tree(self, parent, columns, widths=None):
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        tree = ttk.Treeview(wrap, columns=columns, show="headings")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vs.set)
        for i, c in enumerate(columns):
            tree.heading(c, text=c, command=lambda col=c, t=tree: self._sort(t, col, False))
            tree.column(c, width=(widths[i] if widths else 120), anchor="w")
        tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        tree.tag_configure("warn", background="#5a1e1e")
        return tree

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

    # -- tabs --------------------------------------------------------------
    def _build_nodes_tab(self):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="Nodes")

        # aggregate pod requests per node
        agg = {}
        for p in self.model["pods"]:
            a = agg.setdefault(p["node"], {"pods": 0, "cpu": 0.0, "mem": 0.0})
            a["pods"] += 1
            a["cpu"] += p["cpu_req_m"]
            a["mem"] += p["mem_req_b"]

        cols = ("node", "ready", "pods", "cpu alloc", "cpu req%", "cpu used%",
                "mem alloc", "mem req%", "mem used%")
        tree = self._make_tree(frame, cols, [150, 70, 55, 90, 80, 80, 90, 80, 80])
        for n in self.model["nodes"]:
            a = agg.get(n["node"], {"pods": 0, "cpu": 0.0, "mem": 0.0})
            req_cpu_p = a["cpu"] / n["cpu_alloc_m"] * 100 if n["cpu_alloc_m"] else 0
            req_mem_p = a["mem"] / n["mem_alloc_b"] * 100 if n["mem_alloc_b"] else 0
            tag = "warn" if req_cpu_p > 85 or req_mem_p > 85 else ""
            tree.insert("", "end", tags=(tag,), values=(
                n["node"], n["ready"], a["pods"],
                fmt_cpu(n["cpu_alloc_m"]), f"{req_cpu_p:.0f}%",
                pct(n["cpu_used_m"], n["cpu_alloc_m"]) if n["cpu_used_m"] is not None else "—",
                fmt_mem(n["mem_alloc_b"]), f"{req_mem_p:.0f}%",
                pct(n["mem_used_b"], n["mem_alloc_b"]) if n["mem_used_b"] is not None else "—",
            ))

        ttk.Label(frame, text="Double-click a node to see the pods on it. "
                  "req% = scheduler's view (why nodes fill), used% = real usage.",
                  padding=4).pack(anchor="w")
        tree.bind("<Double-1>", lambda e: self._show_node_pods(tree))

    def _show_node_pods(self, tree):
        sel = tree.focus()
        if not sel:
            return
        node = tree.set(sel, "node")
        win = tk.Toplevel(self)
        win.title(f"Pods on {node}")
        win.geometry("1000x500")
        cols = ("namespace", "pod", "phase", "owner", "cpu req", "cpu lim",
                "cpu used", "mem req", "mem lim", "mem used")
        t = self._make_tree(win, cols, [110, 240, 70, 150, 80, 80, 80, 80, 80, 80])
        rows = sorted((p for p in self.model["pods"] if p["node"] == node),
                      key=lambda p: p["mem_req_b"], reverse=True)
        for p in rows:
            t.insert("", "end", tags=("warn" if p["missing_limits"] else "",), values=(
                p["namespace"], p["pod"], p["phase"], p["owner"],
                fmt_cpu(p["cpu_req_m"]), fmt_cpu(p["cpu_lim_m"]) if p["cpu_lim_m"] else "—",
                fmt_cpu(p["cpu_used_m"]), fmt_mem(p["mem_req_b"]),
                fmt_mem(p["mem_lim_b"]) if p["mem_lim_b"] else "—", fmt_mem(p["mem_used_b"]),
            ))

    def _build_deploys_tab(self):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="Deployments")
        cols = ("namespace", "deployment", "replicas", "cpu req/pod", "cpu lim/pod",
                "mem req/pod", "mem lim/pod", "cpu req TOTAL", "mem req TOTAL", "missing")
        tree = self._make_tree(frame, cols, [110, 200, 80, 90, 90, 90, 90, 95, 95, 160])
        for d in sorted(self.model["deployments"], key=lambda x: x["mem_req_total_b"], reverse=True):
            tree.insert("", "end", tags=("warn" if d["missing"] else "",), values=(
                d["namespace"], d["deployment"], f'{d["replicas"]} ({d["ready"]} ready)',
                fmt_cpu(d["cpu_req_m"]), fmt_cpu(d["cpu_lim_m"]) if d["cpu_lim_m"] else "none",
                fmt_mem(d["mem_req_b"]), fmt_mem(d["mem_lim_b"]) if d["mem_lim_b"] else "none",
                fmt_cpu(d["cpu_req_total_m"]), fmt_mem(d["mem_req_total_b"]), d["missing"],
            ))
        ttk.Label(frame, text="TOTAL = per-pod × replicas (real reserved footprint). "
                  "Red = missing a request/limit.", padding=4).pack(anchor="w")

    def _build_pods_tab(self):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="Pods")
        bar = ttk.Frame(frame, padding=4)
        bar.pack(fill="x")
        ttk.Label(bar, text="Filter:").pack(side="left")
        filt = tk.StringVar()
        ttk.Entry(bar, textvariable=filt, width=40).pack(side="left", padx=4)
        cols = ("namespace", "pod", "node", "phase", "cpu req", "cpu used",
                "mem req", "mem used", "no lim")
        tree = self._make_tree(frame, cols, [110, 240, 150, 70, 80, 80, 80, 80, 55])

        def refill(*_):
            for r in tree.get_children(""):
                tree.delete(r)
            q = filt.get().lower()
            rows = sorted(self.model["pods"], key=lambda p: p["mem_req_b"], reverse=True)
            for p in rows:
                hay = f'{p["namespace"]} {p["pod"]} {p["node"]}'.lower()
                if q and q not in hay:
                    continue
                tree.insert("", "end", tags=("warn" if p["missing_limits"] else "",), values=(
                    p["namespace"], p["pod"], p["node"], p["phase"],
                    fmt_cpu(p["cpu_req_m"]), fmt_cpu(p["cpu_used_m"]),
                    fmt_mem(p["mem_req_b"]), fmt_mem(p["mem_used_b"]),
                    "⚠" if p["missing_limits"] else "",
                ))
        filt.trace_add("write", refill)
        refill()

    def _build_offenders_tab(self):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="Biggest offenders")
        ttk.Label(frame, padding=6, text=(
            "Pods reserving far more memory than they use — the over-provisioning that "
            "forces new nodes. Scheduling uses requests, not usage.")).pack(anchor="w")
        cols = ("namespace", "pod", "node", "mem req", "mem used", "mem WASTED",
                "cpu req", "cpu used")
        tree = self._make_tree(frame, cols, [110, 240, 150, 90, 90, 100, 80, 80])
        rows = [p for p in self.model["pods"] if p["mem_used_b"] is not None]
        rows.sort(key=lambda p: p["mem_req_b"] - (p["mem_used_b"] or 0), reverse=True)
        for p in rows[:40]:
            waste = p["mem_req_b"] - (p["mem_used_b"] or 0)
            tree.insert("", "end", values=(
                p["namespace"], p["pod"], p["node"], fmt_mem(p["mem_req_b"]),
                fmt_mem(p["mem_used_b"]), fmt_mem(waste),
                fmt_cpu(p["cpu_req_m"]), fmt_cpu(p["cpu_used_m"]),
            ))
        if not rows:
            ttk.Label(frame, padding=6, text="No metrics-server data available.").pack(anchor="w")


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "data"
    Dashboard(folder).mainloop()


if __name__ == "__main__":
    main()
