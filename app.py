"""
K8s Resource Dashboard — parses pasted/collected kubectl JSON and lets you click around
to see nodes, the pods on them, and requests vs limits vs actual usage.

Run:
    pip install streamlit pandas
    streamlit run app.py

Data: put the JSON files from collect.sh into ./data (default), or point the sidebar
"Data folder" at wherever you copied them.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

st.set_page_config(page_title="K8s Resource Dashboard", layout="wide")

# ---------------------------------------------------------------------------
# Quantity parsing: K8s uses things like "500m", "2", "128Mi", "1Gi", "1500000n".
# We normalize CPU -> millicores (int) and memory -> bytes (int).
# ---------------------------------------------------------------------------
_CPU_SUFFIX = {"n": 1e-6, "u": 1e-3, "m": 1.0}  # -> millicores
_MEM_SUFFIX = {
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5,
    "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15,
    "k": 1e3,
}


def cpu_to_milli(q: str | None) -> float:
    """CPU quantity -> millicores."""
    if not q:
        return 0.0
    q = str(q).strip()
    for suf, mult in _CPU_SUFFIX.items():
        if q.endswith(suf):
            return float(q[:-len(suf)]) * mult
    return float(q) * 1000.0  # bare number = whole cores


def mem_to_bytes(q: str | None) -> float:
    """Memory quantity -> bytes."""
    if not q:
        return 0.0
    q = str(q).strip()
    for suf, mult in _MEM_SUFFIX.items():
        if q.endswith(suf):
            return float(q[:-len(suf)]) * mult
    return float(q)


def fmt_cpu(milli: float) -> str:
    if milli >= 1000:
        return f"{milli/1000:.2f} cores"
    return f"{milli:.0f}m"


def fmt_mem(b: float) -> str:
    for unit, size in (("Gi", 1024**3), ("Mi", 1024**2), ("Ki", 1024)):
        if b >= size:
            return f"{b/size:.1f}{unit}"
    return f"{b:.0f}B"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _load(folder: Path, name: str) -> dict:
    p = folder / name
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return {}


@st.cache_data(show_spinner=False)
def load_all(folder_str: str) -> dict[str, pd.DataFrame]:
    folder = Path(folder_str)
    deployments = _load(folder, "deployments.json")
    pods = _load(folder, "pods.json")
    nodes = _load(folder, "nodes.json")
    pod_metrics = _load(folder, "pod-metrics.json")
    node_metrics = _load(folder, "node-metrics.json")

    # --- actual usage lookups ---
    # pod usage keyed by (namespace, pod) -> summed containers
    pod_usage: dict[tuple[str, str], dict[str, float]] = {}
    for item in pod_metrics.get("items", []):
        ns = item["metadata"]["namespace"]
        name = item["metadata"]["name"]
        cpu = sum(cpu_to_milli(c.get("usage", {}).get("cpu")) for c in item.get("containers", []))
        mem = sum(mem_to_bytes(c.get("usage", {}).get("memory")) for c in item.get("containers", []))
        pod_usage[(ns, name)] = {"cpu": cpu, "mem": mem}

    node_usage: dict[str, dict[str, float]] = {}
    for item in node_metrics.get("items", []):
        node_usage[item["metadata"]["name"]] = {
            "cpu": cpu_to_milli(item.get("usage", {}).get("cpu")),
            "mem": mem_to_bytes(item.get("usage", {}).get("memory")),
        }

    # --- nodes ---
    node_rows = []
    for n in nodes.get("items", []):
        name = n["metadata"]["name"]
        alloc = n["status"].get("allocatable", {})
        cap = n["status"].get("capacity", {})
        ready = next((c["status"] for c in n["status"].get("conditions", []) if c["type"] == "Ready"), "?")
        u = node_usage.get(name, {})
        node_rows.append({
            "node": name,
            "ready": ready,
            "cpu_capacity_m": cpu_to_milli(cap.get("cpu")),
            "cpu_alloc_m": cpu_to_milli(alloc.get("cpu")),
            "mem_capacity_b": mem_to_bytes(cap.get("memory")),
            "mem_alloc_b": mem_to_bytes(alloc.get("memory")),
            "cpu_used_m": u.get("cpu", float("nan")),
            "mem_used_b": u.get("mem", float("nan")),
        })
    nodes_df = pd.DataFrame(node_rows)

    # --- pods (one row per pod, requests/limits summed over containers) ---
    pod_rows = []
    for p in pods.get("items", []):
        ns = p["metadata"]["namespace"]
        name = p["metadata"]["name"]
        spec = p.get("spec", {})
        node = spec.get("nodeName", "<unscheduled>")
        phase = p.get("status", {}).get("phase", "?")
        owner = "-"
        for o in p["metadata"].get("ownerReferences", []):
            owner = f'{o["kind"]}/{o["name"]}'
            break
        cpu_req = cpu_lim = 0.0
        mem_req = mem_lim = 0.0
        containers = spec.get("containers", [])
        n_missing_lim = 0
        for c in containers:
            res = c.get("resources", {})
            req = res.get("requests", {})
            lim = res.get("limits", {})
            cpu_req += cpu_to_milli(req.get("cpu"))
            mem_req += mem_to_bytes(req.get("memory"))
            cpu_lim += cpu_to_milli(lim.get("cpu"))
            mem_lim += mem_to_bytes(lim.get("memory"))
            if not lim.get("cpu") or not lim.get("memory"):
                n_missing_lim += 1
        u = pod_usage.get((ns, name), {})
        pod_rows.append({
            "namespace": ns,
            "pod": name,
            "node": node,
            "phase": phase,
            "owner": owner,
            "containers": len(containers),
            "cpu_req_m": cpu_req,
            "cpu_lim_m": cpu_lim,
            "mem_req_b": mem_req,
            "mem_lim_b": mem_lim,
            "cpu_used_m": u.get("cpu", float("nan")),
            "mem_used_b": u.get("mem", float("nan")),
            "missing_limits": n_missing_lim,
        })
    pods_df = pd.DataFrame(pod_rows)

    # --- deployments (requests/limits from pod template * replicas) ---
    dep_rows = []
    for d in deployments.get("items", []):
        ns = d["metadata"]["namespace"]
        name = d["metadata"]["name"]
        replicas = d["spec"].get("replicas", 1)
        ready = d.get("status", {}).get("readyReplicas", 0)
        containers = d["spec"]["template"]["spec"].get("containers", [])
        cpu_req = cpu_lim = mem_req = mem_lim = 0.0
        missing = []
        for c in containers:
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
            "namespace": ns,
            "deployment": name,
            "replicas": replicas,
            "ready": ready,
            # per-pod values:
            "cpu_req_m": cpu_req,
            "cpu_lim_m": cpu_lim,
            "mem_req_b": mem_req,
            "mem_lim_b": mem_lim,
            # total footprint across all replicas:
            "cpu_req_total_m": cpu_req * replicas,
            "mem_req_total_b": mem_req * replicas,
            "missing": ", ".join(missing) if missing else "",
        })
    deps_df = pd.DataFrame(dep_rows)

    return {"nodes": nodes_df, "pods": pods_df, "deployments": deps_df}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
default_folder = os.path.join(os.path.dirname(__file__), "data")
st.sidebar.title("⚙️  Data")
folder = st.sidebar.text_input("Data folder", value=default_folder)
if st.sidebar.button("🔄 Reload"):
    st.cache_data.clear()

if not Path(folder).exists() or not (Path(folder) / "pods.json").exists():
    st.title("K8s Resource Dashboard")
    st.warning(
        "No data found. Run **collect.sh** on a machine where kubectl works, then copy the "
        "`data/` folder here (or point the sidebar at it)."
    )
    st.code("KUBECTL='sudo k3s kubectl' ./collect.sh", language="bash")
    st.stop()

data = load_all(folder)
nodes_df, pods_df, deps_df = data["nodes"], data["pods"], data["deployments"]
has_metrics = pods_df["cpu_used_m"].notna().any()

st.title("☸️  K8s Resource Dashboard")
if not has_metrics:
    st.info("No metrics-server data found — 'actual usage' columns will be blank. "
            "Requests/limits still work.")

tab_over, tab_nodes, tab_deploys, tab_pods = st.tabs(
    ["📊 Overview", "🖥️ Nodes", "🚀 Deployments", "📦 Pods"]
)

# ---- Overview -------------------------------------------------------------
with tab_over:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Nodes", len(nodes_df))
    c2.metric("Deployments", len(deps_df))
    c3.metric("Pods", len(pods_df))
    c4.metric("Pods missing limits", int((pods_df["missing_limits"] > 0).sum()))

    st.subheader("Requests vs actual usage — biggest gaps")
    st.caption("Scheduling uses **requests**, not usage. Big gaps = over-provisioned pods "
               "reserving capacity they don't use, which forces new nodes.")
    gap = pods_df.copy()
    gap["cpu_gap_m"] = gap["cpu_req_m"] - gap["cpu_used_m"]
    gap["mem_gap_b"] = gap["mem_req_b"] - gap["mem_used_b"]
    show = gap.sort_values("mem_gap_b", ascending=False).head(20)
    st.dataframe(
        pd.DataFrame({
            "namespace": show["namespace"],
            "pod": show["pod"],
            "node": show["node"],
            "cpu req": show["cpu_req_m"].map(fmt_cpu),
            "cpu used": show["cpu_used_m"].map(lambda v: fmt_cpu(v) if pd.notna(v) else "—"),
            "mem req": show["mem_req_b"].map(fmt_mem),
            "mem used": show["mem_used_b"].map(lambda v: fmt_mem(v) if pd.notna(v) else "—"),
            "mem wasted": show["mem_gap_b"].map(lambda v: fmt_mem(v) if pd.notna(v) else "—"),
        }),
        use_container_width=True, hide_index=True,
    )

    st.subheader("⚠️ Deployments with missing requests/limits")
    bad = deps_df[deps_df["missing"] != ""]
    if len(bad):
        st.dataframe(bad[["namespace", "deployment", "replicas", "missing"]],
                     use_container_width=True, hide_index=True)
    else:
        st.success("Every deployment sets requests and limits. 🎉")

# ---- Nodes (click a node -> see its pods) ---------------------------------
with tab_nodes:
    st.subheader("Nodes — requested vs allocatable vs actual")
    ndf = nodes_df.copy()
    # aggregate requests landed on each node from pods
    pod_by_node = pods_df.groupby("node").agg(
        pods=("pod", "count"),
        cpu_req_m=("cpu_req_m", "sum"),
        mem_req_b=("mem_req_b", "sum"),
    ).reset_index()
    ndf = ndf.merge(pod_by_node, left_on="node", right_on="node", how="left").fillna(
        {"pods": 0, "cpu_req_m": 0, "mem_req_b": 0}
    )
    ndf["cpu_req_%"] = (ndf["cpu_req_m"] / ndf["cpu_alloc_m"] * 100).round(0)
    ndf["mem_req_%"] = (ndf["mem_req_b"] / ndf["mem_alloc_b"] * 100).round(0)
    ndf["cpu_used_%"] = (ndf["cpu_used_m"] / ndf["cpu_alloc_m"] * 100).round(0)
    ndf["mem_used_%"] = (ndf["mem_used_b"] / ndf["mem_alloc_b"] * 100).round(0)

    st.dataframe(
        pd.DataFrame({
            "node": ndf["node"],
            "ready": ndf["ready"],
            "pods": ndf["pods"].astype(int),
            "cpu alloc": ndf["cpu_alloc_m"].map(fmt_cpu),
            "cpu requested": ndf["cpu_req_%"].map(lambda v: f"{v:.0f}%"),
            "cpu used": ndf["cpu_used_%"].map(lambda v: f"{v:.0f}%" if pd.notna(v) else "—"),
            "mem alloc": ndf["mem_alloc_b"].map(fmt_mem),
            "mem requested": ndf["mem_req_%"].map(lambda v: f"{v:.0f}%"),
            "mem used": ndf["mem_used_%"].map(lambda v: f"{v:.0f}%" if pd.notna(v) else "—"),
        }),
        use_container_width=True, hide_index=True,
    )
    st.caption("**requested %** = scheduler's view (why nodes fill up).  "
               "**used %** = real utilization. Big divergence is your smoking gun.")

    st.divider()
    pick = st.selectbox("🔎 Inspect a node → its pods", ndf["node"].tolist())
    if pick:
        row = ndf[ndf["node"] == pick].iloc[0]
        m1, m2, m3 = st.columns(3)
        m1.metric("Pods", int(row["pods"]))
        m2.metric("CPU requested", f"{row['cpu_req_%']:.0f}%",
                  help=f"{fmt_cpu(row['cpu_req_m'])} of {fmt_cpu(row['cpu_alloc_m'])}")
        m3.metric("Mem requested", f"{row['mem_req_%']:.0f}%",
                  help=f"{fmt_mem(row['mem_req_b'])} of {fmt_mem(row['mem_alloc_b'])}")
        node_pods = pods_df[pods_df["node"] == pick].sort_values("mem_req_b", ascending=False)
        st.dataframe(
            pd.DataFrame({
                "namespace": node_pods["namespace"],
                "pod": node_pods["pod"],
                "phase": node_pods["phase"],
                "owner": node_pods["owner"],
                "cpu req": node_pods["cpu_req_m"].map(fmt_cpu),
                "cpu lim": node_pods["cpu_lim_m"].map(lambda v: fmt_cpu(v) if v else "—"),
                "cpu used": node_pods["cpu_used_m"].map(lambda v: fmt_cpu(v) if pd.notna(v) else "—"),
                "mem req": node_pods["mem_req_b"].map(fmt_mem),
                "mem lim": node_pods["mem_lim_b"].map(lambda v: fmt_mem(v) if v else "—"),
                "mem used": node_pods["mem_used_b"].map(lambda v: fmt_mem(v) if pd.notna(v) else "—"),
            }),
            use_container_width=True, hide_index=True,
        )

# ---- Deployments ----------------------------------------------------------
with tab_deploys:
    st.subheader("Deployments — requests & limits")
    namespaces = ["(all)"] + sorted(deps_df["namespace"].unique())
    ns_pick = st.selectbox("Namespace", namespaces, key="dep_ns")
    view = deps_df if ns_pick == "(all)" else deps_df[deps_df["namespace"] == ns_pick]
    view = view.sort_values("mem_req_total_b", ascending=False)
    st.dataframe(
        pd.DataFrame({
            "namespace": view["namespace"],
            "deployment": view["deployment"],
            "replicas": view["replicas"].astype(str) + " (" + view["ready"].astype(str) + " ready)",
            "cpu req/pod": view["cpu_req_m"].map(fmt_cpu),
            "cpu lim/pod": view["cpu_lim_m"].map(lambda v: fmt_cpu(v) if v else "— none"),
            "mem req/pod": view["mem_req_b"].map(fmt_mem),
            "mem lim/pod": view["mem_lim_b"].map(lambda v: fmt_mem(v) if v else "— none"),
            "cpu req TOTAL": view["cpu_req_total_m"].map(fmt_cpu),
            "mem req TOTAL": view["mem_req_total_b"].map(fmt_mem),
            "missing": view["missing"],
        }),
        use_container_width=True, hide_index=True,
    )
    st.caption("TOTAL = per-pod × replicas — the real footprint reserved on the cluster.")

# ---- Pods (filterable) ----------------------------------------------------
with tab_pods:
    st.subheader("All pods")
    col1, col2 = st.columns(2)
    ns_f = col1.multiselect("Namespace", sorted(pods_df["namespace"].unique()))
    node_f = col2.multiselect("Node", sorted(pods_df["node"].unique()))
    view = pods_df.copy()
    if ns_f:
        view = view[view["namespace"].isin(ns_f)]
    if node_f:
        view = view[view["node"].isin(node_f)]
    view = view.sort_values("mem_req_b", ascending=False)
    st.dataframe(
        pd.DataFrame({
            "namespace": view["namespace"],
            "pod": view["pod"],
            "node": view["node"],
            "phase": view["phase"],
            "owner": view["owner"],
            "cpu req": view["cpu_req_m"].map(fmt_cpu),
            "cpu used": view["cpu_used_m"].map(lambda v: fmt_cpu(v) if pd.notna(v) else "—"),
            "mem req": view["mem_req_b"].map(fmt_mem),
            "mem used": view["mem_used_b"].map(lambda v: fmt_mem(v) if pd.notna(v) else "—"),
            "no limits": view["missing_limits"].map(lambda v: "⚠️" if v else ""),
        }),
        use_container_width=True, hide_index=True,
    )
