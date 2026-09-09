"""
Airgapped_Prep — vendor every Python dependency for offline / air-gapped install.

The desktop dashboard (k8s_dashboard_gui.py) is standard-library only and needs
NOTHING here. This script exists for the optional Streamlit web variant (app.py),
whose dependencies live in requirements.txt (streamlit, pandas + their transitive
deps). It downloads wheels for Windows, macOS, and Linux — each into its own
folder — on a machine WITH internet, so you can carry them to an air-gapped box
and `pip install` with no network.

Run it on any machine that has internet + pip (the OS you run it on does NOT
matter — `pip download --platform` cross-fetches wheels for all targets):

    python Airgapped_Prep.py

Output (default ./airgapped_bundle):

    airgapped_bundle/
      windows/   <wheels> + requirements.txt + INSTALL.txt
      macos/     <wheels> + requirements.txt + INSTALL.txt
      linux/     <wheels> + requirements.txt + INSTALL.txt
      INSTALL_COMMANDS.txt   (all three, in one place)

On the air-gapped machine, copy the matching OS folder over and run the one
command in its INSTALL.txt.

Options:
    --python-versions 3.11 3.12 3.13   Interpreter versions to fetch wheels for
    --requirements PATH                Requirements file (default: ./requirements.txt)
    --output DIR                       Output bundle dir (default: ./airgapped_bundle)
    --os windows macos linux           Subset of targets (default: all)
    --include-arm                      Also fetch Linux aarch64 + more macOS arm64
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Wheel platform tags per OS. pip will download any wheel matching ANY listed
# tag, so an OS folder ends up holding wheels for every arch/glibc we ask for.
BASE_PLATFORMS = {
    "windows": ["win_amd64"],
    "macos": ["macosx_10_9_x86_64", "macosx_11_0_arm64"],
    "linux": ["manylinux2014_x86_64", "manylinux_2_17_x86_64", "manylinux_2_28_x86_64"],
}
ARM_PLATFORMS = {
    "windows": [],
    "macos": ["macosx_12_0_arm64", "macosx_14_0_arm64"],
    "linux": ["manylinux2014_aarch64", "manylinux_2_28_aarch64"],
}


def build_platforms(os_name: str, include_arm: bool) -> list[str]:
    plats = list(BASE_PLATFORMS[os_name])
    if include_arm:
        plats += ARM_PLATFORMS[os_name]
    return plats


def download_os(os_name: str, platforms: list[str], py_versions: list[str],
                requirements: Path, dest: Path) -> list[str]:
    """
    Fetch wheels for one OS into `dest`. Returns a list of human-readable warnings
    for (python-version × platform) combos that had no compatible wheel — common
    and harmless when a package simply doesn't ship that exact tag.
    """
    dest.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    for pyver in py_versions:
        # One pip call per (os, python-version). --only-binary=:all: is REQUIRED
        # when using --platform: pip can't build sdists for a foreign platform, so
        # everything must come as a prebuilt wheel.
        cmd = [
            sys.executable, "-m", "pip", "download",
            "-r", str(requirements),
            "--dest", str(dest),
            "--only-binary=:all:",
            "--python-version", pyver,
            "--implementation", "cp",
        ]
        for plat in platforms:
            cmd += ["--platform", plat]

        print(f"  - {os_name}: python {pyver}  ({len(platforms)} platform tag(s))")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            # Retry once per platform so a single unsatisfiable tag doesn't sink
            # the whole version. Collect what we can; note the rest.
            for plat in platforms:
                one = [
                    sys.executable, "-m", "pip", "download",
                    "-r", str(requirements), "--dest", str(dest),
                    "--only-binary=:all:", "--python-version", pyver,
                    "--implementation", "cp", "--platform", plat,
                ]
                p2 = subprocess.run(one, capture_output=True, text=True)
                if p2.returncode != 0:
                    warnings.append(f"{os_name} py{pyver} {plat}: no compatible wheel")
    return warnings


INSTALL_TEMPLATE = """\
# Offline install — {os_title}
# ---------------------------------------------------------------------------
# Prerequisites on the air-gapped machine:
#   * Python {pyvers} (matching one of the versions these wheels were built for)
#   * tkinter — this ships WITH Python and is NOT pip-installable. It is only
#     needed for the desktop app (k8s_dashboard_gui.py), which otherwise has
#     zero dependencies. These wheels are for the optional Streamlit web app.
#
# 1. Copy this entire "{os_folder}" folder to the offline machine.
# 2. Open a terminal IN this folder and run:
#
{install_cmd}
#
# The flags mean: --no-index = never touch the internet; --find-links . = install
# only from wheels in this folder.
#
# Then launch the web app from the project folder:
#   streamlit run app.py
"""


def write_install_file(os_name: str, dest: Path, py_versions: list[str]) -> str:
    os_title = {"windows": "Windows", "macos": "macOS", "linux": "Linux"}[os_name]
    launcher = "py" if os_name == "windows" else "python3"
    install_cmd = f"    {launcher} -m pip install --no-index --find-links . -r requirements.txt"
    text = INSTALL_TEMPLATE.format(
        os_title=os_title, os_folder=os_name, pyvers=" / ".join(py_versions),
        install_cmd=install_cmd,
    )
    (dest / "INSTALL.txt").write_text(text, encoding="utf-8")
    return text


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Vendor Python deps for air-gapped install.")
    ap.add_argument("--python-versions", nargs="+", default=["3.11", "3.12", "3.13"],
                    help="Interpreter versions to fetch wheels for (default: 3.11 3.12 3.13)")
    ap.add_argument("--requirements", type=Path, default=here / "requirements.txt")
    ap.add_argument("--output", type=Path, default=here / "airgapped_bundle")
    ap.add_argument("--os", nargs="+", choices=list(BASE_PLATFORMS),
                    default=list(BASE_PLATFORMS), help="Subset of target OSes.")
    ap.add_argument("--include-arm", action="store_true",
                    help="Also fetch ARM wheels (Linux aarch64, more macOS arm64).")
    args = ap.parse_args()

    # Windows consoles default to cp1252 and choke on non-ASCII; make stdout UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    if not args.requirements.exists():
        print(f"! requirements file not found: {args.requirements}")
        return 1

    args.output.mkdir(parents=True, exist_ok=True)
    all_warnings: list[str] = []
    combined = ["Air-gapped install commands", "=" * 40, ""]

    for os_name in args.os:
        print(f"\n== {os_name} ==")
        dest = args.output / os_name
        platforms = build_platforms(os_name, args.include_arm)
        all_warnings += download_os(os_name, platforms, args.python_versions,
                                    args.requirements, dest)
        # Ship the requirements file next to the wheels so the install command
        # is self-contained inside the OS folder.
        shutil.copy2(args.requirements, dest / "requirements.txt")
        text = write_install_file(os_name, dest, args.python_versions)
        n_wheels = len(list(dest.glob("*.whl")))
        print(f"  OK: {n_wheels} wheels in {dest}")
        combined += [f"## {os_name}  ({n_wheels} wheels)", text, ""]

    (args.output / "INSTALL_COMMANDS.txt").write_text("\n".join(combined), encoding="utf-8")

    print("\nDone.")
    print(f"Bundle: {args.output}")
    if all_warnings:
        print("\nNotes (missing tags — usually fine, another tag/version covers it):")
        for w in sorted(set(all_warnings)):
            print(f"  - {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
