#!/usr/bin/env bash
# Run this ON A MACHINE WHERE kubectl WORKS (e.g. the Pi: `ssh pi`, then `sudo k3s kubectl`
# — but note metrics --raw needs the same permissions).
# Dumps everything the dashboard needs as JSON into ./data.
#
# Usage:
#   ./collect.sh                 # uses `kubectl`
#   KUBECTL="sudo k3s kubectl" ./collect.sh    # k3s on the Pi
#
# Then copy the ./data folder back to the Windows box next to app.py.

set -euo pipefail
KUBECTL="${KUBECTL:-kubectl}"
OUT="$(dirname "$0")/data"
mkdir -p "$OUT"

echo "collecting deployments..."
$KUBECTL get deploy -A -o json               > "$OUT/deployments.json"

echo "collecting pods..."
$KUBECTL get pods -A -o json                 > "$OUT/pods.json"

echo "collecting nodes..."
$KUBECTL get nodes -o json                   > "$OUT/nodes.json"

# metrics-server (actual live usage). `kubectl top` has no JSON, so hit the raw API.
# If metrics-server isn't installed these will fail — that's fine, the app degrades gracefully.
echo "collecting pod metrics (actual usage)..."
$KUBECTL get --raw "/apis/metrics.k8s.io/v1beta1/pods"  > "$OUT/pod-metrics.json"  2>/dev/null || echo "{}" > "$OUT/pod-metrics.json"

echo "collecting node metrics (actual usage)..."
$KUBECTL get --raw "/apis/metrics.k8s.io/v1beta1/nodes" > "$OUT/node-metrics.json" 2>/dev/null || echo "{}" > "$OUT/node-metrics.json"

echo "done -> $OUT"
