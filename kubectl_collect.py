"""
Cross-platform kubectl collector.

Runs the user's own `kubectl` (found on PATH) against whatever context/kubeconfig
they already have configured — no SSH, no copying JSON around. Works on Windows,
macOS, and Linux because it only shells out to the kubectl binary the user has.

The GUI calls collect() on a background thread and gets back the same five JSON
objects that used to live in ./data. If kubectl isn't on PATH, or a command
fails, we raise KubectlError with a human-readable message.

Override the binary for non-standard setups (e.g. k3s on a Pi):
    KUBECTL="sudo k3s kubectl" python k8s_dashboard_gui.py
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys

# Hide the flashing console window kubectl would otherwise pop on Windows.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class KubectlError(RuntimeError):
    """kubectl is missing, misconfigured, or a command failed."""


def _base_cmd() -> list[str]:
    """The kubectl invocation, honoring a KUBECTL override like 'sudo k3s kubectl'."""
    override = os.environ.get("KUBECTL", "").strip()
    if override:
        # Split respecting quotes so "sudo k3s kubectl" -> ["sudo","k3s","kubectl"].
        return shlex.split(override, posix=(sys.platform != "win32"))
    return ["kubectl"]


def kubectl_path() -> str | None:
    """Absolute path to the kubectl binary, or None if it can't be found on PATH."""
    return shutil.which(_base_cmd()[0])


def _run(args: list[str], *, context: str = "", timeout: int = 60) -> str:
    """Run `kubectl <args>` and return stdout, raising KubectlError on failure."""
    cmd = _base_cmd()
    if context:
        cmd += ["--context", context]
    cmd += args

    if shutil.which(cmd[0]) is None:
        raise KubectlError(
            f"'{cmd[0]}' was not found on your PATH.\n\n"
            "Install kubectl and make sure it's on PATH, or set the KUBECTL "
            "environment variable to the full command (e.g. 'sudo k3s kubectl')."
        )
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError as e:
        raise KubectlError(f"Could not execute {cmd[0]}: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise KubectlError(f"kubectl timed out after {timeout}s: {' '.join(args)}") from e

    if proc.returncode != 0:
        raise KubectlError(
            f"kubectl {' '.join(args)} failed (exit {proc.returncode}):\n"
            + (proc.stderr.strip() or "(no error output)")
        )
    return proc.stdout


def list_contexts() -> tuple[list[str], str]:
    """Return (all context names, current context). Empty/[] if none configured."""
    try:
        out = _run(["config", "get-contexts", "-o", "name"])
    except KubectlError:
        return [], ""
    contexts = [ln.strip() for ln in out.splitlines() if ln.strip()]
    try:
        current = _run(["config", "current-context"]).strip()
    except KubectlError:
        current = ""
    return contexts, current


def _get_json(args: list[str], *, context: str, timeout: int) -> dict:
    return json.loads(_run(args, context=context, timeout=timeout))


def collect(context: str = "", *, timeout: int = 60) -> dict:
    """
    Pull everything the dashboard needs, in memory, from the live cluster.

    Returns a dict with keys: deployments, pods, nodes, pod_metrics, node_metrics
    (each the parsed kubectl JSON). metrics-server data degrades to {} if the
    metrics API isn't available, so the dashboard still works without it.
    """
    deployments = _get_json(["get", "deploy", "-A", "-o", "json"], context=context, timeout=timeout)
    pods = _get_json(["get", "pods", "-A", "-o", "json"], context=context, timeout=timeout)
    nodes = _get_json(["get", "nodes", "-o", "json"], context=context, timeout=timeout)

    # metrics-server: kubectl top has no JSON, so hit the raw metrics API. Optional.
    try:
        pod_metrics = _get_json(
            ["get", "--raw", "/apis/metrics.k8s.io/v1beta1/pods"], context=context, timeout=timeout)
    except KubectlError:
        pod_metrics = {}
    try:
        node_metrics = _get_json(
            ["get", "--raw", "/apis/metrics.k8s.io/v1beta1/nodes"], context=context, timeout=timeout)
    except KubectlError:
        node_metrics = {}

    return {
        "deployments": deployments,
        "pods": pods,
        "nodes": nodes,
        "pod_metrics": pod_metrics,
        "node_metrics": node_metrics,
    }
