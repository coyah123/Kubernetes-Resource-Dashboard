# Collect everything the dashboard needs, using the CURRENT kubectl context.
# Runs anywhere kubectl works — no SSH. Output: .\data\*.json
#
# Usage:
#   .\collect.ps1                              # uses current context
#   .\collect.ps1 -Context my-work-cluster     # or pin a context
#
# Then: python k8s_dashboard_gui.py

param(
    [string]$Context = ""
)

$ErrorActionPreference = "Stop"
$ctxArg = if ($Context) { @("--context", $Context) } else { @() }
$out = Join-Path $PSScriptRoot "data"
New-Item -ItemType Directory -Force -Path $out | Out-Null

$cur = & kubectl @ctxArg config current-context
Write-Host "Collecting from context: $cur" -ForegroundColor Cyan

Write-Host "  deployments..."
& kubectl @ctxArg get deploy -A -o json | Out-File -Encoding utf8 (Join-Path $out "deployments.json")

Write-Host "  pods..."
& kubectl @ctxArg get pods -A -o json   | Out-File -Encoding utf8 (Join-Path $out "pods.json")

Write-Host "  nodes..."
& kubectl @ctxArg get nodes -o json     | Out-File -Encoding utf8 (Join-Path $out "nodes.json")

# metrics-server (actual usage). `kubectl top` has no JSON, so hit the raw metrics API.
# If metrics-server isn't installed these fail -> write {} so the dashboard degrades gracefully.
Write-Host "  pod metrics (actual usage)..."
try {
    & kubectl @ctxArg get --raw "/apis/metrics.k8s.io/v1beta1/pods"  | Out-File -Encoding utf8 (Join-Path $out "pod-metrics.json")
} catch {
    Write-Host "    (no metrics-server -> skipping pod usage)" -ForegroundColor Yellow
    "{}" | Out-File -Encoding utf8 (Join-Path $out "pod-metrics.json")
}

Write-Host "  node metrics (actual usage)..."
try {
    & kubectl @ctxArg get --raw "/apis/metrics.k8s.io/v1beta1/nodes" | Out-File -Encoding utf8 (Join-Path $out "node-metrics.json")
} catch {
    Write-Host "    (no metrics-server -> skipping node usage)" -ForegroundColor Yellow
    "{}" | Out-File -Encoding utf8 (Join-Path $out "node-metrics.json")
}

Write-Host "Done -> $out" -ForegroundColor Green
