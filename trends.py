"""
Trends: sample deployment resource data over time and draw it, stdlib-only.

Two pieces:
  * a tiny append-only JSONL store (one line per deployment per sample) so a full
    day of data accumulates on disk and survives an app restart, and
  * LineChart, a line graph drawn directly on a tkinter Canvas (no matplotlib) so
    the app stays zero-dependency.

Each stored record:
    {"ts": <epoch int>, "ns": <namespace>, "dep": <deployment>,
     "cpu_req_m": float, "mem_req_b": float,
     "cpu_used_m": float|null, "mem_used_b": float|null}

cpu_*  are millicores, mem_* are bytes. req = total reserved (per-pod x replicas),
used = actual usage summed over the deployment's pods (null if no metrics-server).
"""
from __future__ import annotations

import json
import time
import tkinter as tk
from pathlib import Path

HISTORY_FILENAME = "trends-history.jsonl"

# A palette of visually distinct colors, cycled one-per-deployment.
_PALETTE = [
    "#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#b07aa1", "#76b7b2",
    "#edc948", "#ff9da7", "#9c755f", "#bab0ac", "#86bcb6", "#d37295",
]


def color_for(index: int) -> str:
    return _PALETTE[index % len(_PALETTE)]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def samples_from_model(model: dict, ts: int | None = None) -> list[dict]:
    """Turn a built model's deployments into timestamped sample records."""
    if ts is None:
        ts = int(time.time())
    out = []
    for d in model.get("deployments", []):
        out.append({
            "ts": ts,
            "ns": d["namespace"],
            "dep": d["deployment"],
            "cpu_req_m": d.get("cpu_req_total_m", 0.0),
            "mem_req_b": d.get("mem_req_total_b", 0.0),
            "cpu_used_m": d.get("cpu_used_m"),
            "mem_used_b": d.get("mem_used_b"),
        })
    return out


def append_samples(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def load_samples(path: Path) -> list[dict]:
    """Read all stored records; silently skip any corrupt lines."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def namespaces_in(records: list[dict]) -> list[str]:
    return sorted({r["ns"] for r in records})


# ---------------------------------------------------------------------------
# Canvas line chart
# ---------------------------------------------------------------------------
class LineChart(tk.Canvas):
    """
    A minimal time-series line chart. Feed it series via set_series() and it draws
    axes, gridlines, time labels, the lines, and a legend. Redraws on resize.

    A series is a dict: {"label": str, "color": "#rrggbb", "dash": bool,
                         "points": [(ts_epoch, value), ...]}
    Dashed lines are used for the "request" trace vs solid for "usage".
    """
    _PAD_L = 66   # left margin for y labels
    _PAD_R = 12
    _PAD_T = 12
    _PAD_B = 34   # bottom margin for x (time) labels

    def __init__(self, parent, y_fmt=lambda v: f"{v:.0f}", **kw):
        super().__init__(parent, background="#1e1e1e", highlightthickness=0, **kw)
        self._series: list[dict] = []
        self._y_fmt = y_fmt
        self._empty_msg = "No data yet — start capture."
        self.bind("<Configure>", lambda e: self._draw())

    def set_series(self, series: list[dict], y_fmt=None, empty_msg: str | None = None):
        self._series = series or []
        if y_fmt is not None:
            self._y_fmt = y_fmt
        if empty_msg is not None:
            self._empty_msg = empty_msg
        self._draw()

    # -- drawing -----------------------------------------------------------
    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 40 or h < 40:
            return

        pts_all = [p for s in self._series for p in s["points"]]
        if not pts_all:
            self.create_text(w / 2, h / 2, text=self._empty_msg,
                             fill="#888888", font=("TkDefaultFont", 11))
            return

        x0, x1 = self._PAD_L, w - self._PAD_R
        y0, y1 = self._PAD_T, h - self._PAD_B

        t_min = min(p[0] for p in pts_all)
        t_max = max(p[0] for p in pts_all)
        v_min = 0.0  # resource values start at zero
        v_max = max(p[1] for p in pts_all) or 1.0
        v_max *= 1.08  # headroom
        if t_max == t_min:
            t_min -= 30
            t_max += 30

        def sx(t):
            return x0 + (t - t_min) / (t_max - t_min) * (x1 - x0)

        def sy(v):
            return y1 - (v - v_min) / (v_max - v_min) * (y1 - y0)

        # gridlines + y labels
        for i in range(5):
            v = v_min + (v_max - v_min) * i / 4
            y = sy(v)
            self.create_line(x0, y, x1, y, fill="#333333")
            self.create_text(x0 - 6, y, text=self._y_fmt(v), fill="#aaaaaa",
                             anchor="e", font=("TkDefaultFont", 8))
        # x (time) labels
        for i in range(5):
            t = t_min + (t_max - t_min) * i / 4
            x = sx(t)
            self.create_line(x, y0, x, y1, fill="#2a2a2a")
            self.create_text(x, y1 + 6, text=time.strftime("%H:%M", time.localtime(t)),
                             fill="#aaaaaa", anchor="n", font=("TkDefaultFont", 8))
        # axes
        self.create_line(x0, y0, x0, y1, fill="#666666")
        self.create_line(x0, y1, x1, y1, fill="#666666")

        # series
        for s in self._series:
            pts = sorted(s["points"])
            coords = []
            for t, v in pts:
                coords += [sx(t), sy(v)]
            if len(coords) >= 4:
                self.create_line(*coords, fill=s["color"], width=2,
                                 dash=(4, 3) if s.get("dash") else None, smooth=False)
            elif len(coords) == 2:  # single sample -> dot so it's visible
                x, y = coords
                self.create_oval(x - 2, y - 2, x + 2, y + 2, fill=s["color"], outline="")

        self._draw_legend(x1, y0)

    def _draw_legend(self, right, top):
        # De-duplicate by label (usage+request share a color/label).
        seen = {}
        for s in self._series:
            seen.setdefault(s["label"], s["color"])
        y = top + 4
        for label, color in list(seen.items())[:14]:
            self.create_line(right - 150, y + 6, right - 132, y + 6, fill=color, width=3)
            self.create_text(right - 128, y + 6, text=label, fill="#dddddd",
                             anchor="w", font=("TkDefaultFont", 8))
            y += 16
