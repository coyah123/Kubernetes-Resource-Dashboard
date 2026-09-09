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


def run_text(args: list[str], *, context: str = "", timeout: int = 60) -> str:
    """Run `kubectl <args>` and return its raw text output (describe/get/etc.)."""
    return _run(args, context=context, timeout=timeout)


def list_namespaces(context: str = "") -> list[str]:
    """Every namespace name in the cluster (sorted). [] if the call fails."""
    try:
        out = _run(["get", "ns", "-o", "name"], context=context)
    except KubectlError:
        return []
    names = [ln.split("/", 1)[-1].strip() for ln in out.splitlines() if ln.strip()]
    return sorted(names)


def popen_logs(namespace: str, pod: str, *, context: str = "",
               tail: int = 200, follow: bool = True) -> subprocess.Popen:
    """
    Start `kubectl logs [-f]` for a pod and return the running Popen.

    stdout+stderr are merged and line-buffered so a reader thread can stream them
    into the UI. --all-containers/--prefix keeps multi-container pods readable.
    The caller owns the process and must terminate() it when done.
    """
    cmd = _base_cmd()
    if context:
        cmd += ["--context", context]
    cmd += ["logs", pod, "-n", namespace,
            "--all-containers=true", "--prefix=true", f"--tail={tail}"]
    if follow:
        cmd.append("-f")
    if shutil.which(cmd[0]) is None:
        raise KubectlError(f"'{cmd[0]}' was not found on your PATH.")
    return subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, creationflags=_NO_WINDOW,
    )


def popen_exec(namespace: str, pod: str, shell: str = "sh", *,
               context: str = "") -> subprocess.Popen:
    """
    Start `kubectl exec -i <pod> -- <shell>` for an embedded terminal.

    Uses -i (no -t): there's no real TTY, so this is a line-oriented pipe shell —
    great for ls/cat/env, but full-screen programs (vim, top) won't render. stdin
    is a pipe the caller writes commands to; stdout+stderr are merged for a reader
    thread. The caller owns the process and must terminate() it when done.
    """
    cmd = _base_cmd()
    if context:
        cmd += ["--context", context]
    cmd += ["exec", "-i", pod, "-n", namespace, "--", shell]
    if shutil.which(cmd[0]) is None:
        raise KubectlError(f"'{cmd[0]}' was not found on your PATH.")
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, creationflags=_NO_WINDOW,
    )
    # On Windows, text-mode writes translate "\n" -> "\r\n", so the remote shell
    # sees a stray CR ("ls\r": command not found). Disable newline translation on
    # stdin so exactly what we write reaches the shell.
    try:
        proc.stdin.reconfigure(newline="")
    except (AttributeError, ValueError):
        pass
    return proc


def list_items(ktype: str, *, namespace: str = "", all_namespaces: bool = False,
               context: str = "", timeout: int = 30) -> list[dict]:
    """`kubectl get <ktype> [-A|-n ns] -o json` -> the .items list."""
    args = ["get", ktype]
    if all_namespaces:
        args.append("-A")
    elif namespace:
        args += ["-n", namespace]
    args += ["-o", "json"]
    data = json.loads(_run(args, context=context, timeout=timeout))
    return data.get("items", [])


def scale(ktype: str, name: str, replicas: int, *, namespace: str = "",
          context: str = "") -> str:
    args = ["scale", ktype, name, f"--replicas={replicas}"]
    if namespace:
        args += ["-n", namespace]
    return _run(args, context=context)


def rollout_restart(ktype: str, name: str, *, namespace: str = "",
                    context: str = "") -> str:
    args = ["rollout", "restart", f"{ktype}/{name}"]
    if namespace:
        args += ["-n", namespace]
    return _run(args, context=context)


def delete(ktype: str, name: str, *, namespace: str = "", context: str = "") -> str:
    args = ["delete", ktype, name]
    if namespace:
        args += ["-n", namespace]
    return _run(args, context=context)


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

    Returns a dict with keys: deployments, replicasets, pods, nodes, pod_metrics,
    node_metrics (each the parsed kubectl JSON). metrics-server data degrades to {}
    if the metrics API isn't available, so the dashboard still works without it.
    ReplicaSets are used to attribute pod usage back to its owning Deployment.
    """
    deployments = _get_json(["get", "deploy", "-A", "-o", "json"], context=context, timeout=timeout)
    replicasets = _get_json(["get", "rs", "-A", "-o", "json"], context=context, timeout=timeout)
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
        "replicasets": replicasets,
        "pods": pods,
        "nodes": nodes,
        "pod_metrics": pod_metrics,
        "node_metrics": node_metrics,
    }
