"""Build a single-file executable for the current OS with PyInstaller.

    python -m pip install -r requirements.txt -r requirements-build.txt
    python build.py

Output: dist/AdHocShare-windows-x64.exe or dist/AdHocShare-linux-x86_64
"""
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
machine = platform.machine().lower()
arch = {"amd64": "x64", "x86_64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(machine, machine)
if os.name == "nt":
    name = f"AdHocShare-windows-{arch}"
elif sys.platform == "darwin":
    name = f"AdHocShare-macos-{arch}"
else:
    name = f"AdHocShare-linux-{arch}"

args = [
    sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
    "--name", name,
    "--icon", str(ROOT / "assets" / "icon.ico"),
    "--add-data", f"{ROOT / 'assets' / 'icon.png'}{os.pathsep}assets",
    "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"),
    "--specpath", str(ROOT / "build"),
    str(ROOT / "adhoc_share.py"),
]
subprocess.run(args, check=True, cwd=ROOT)
out = next((ROOT / "dist").glob(name + "*"))
print(f"\nBuilt {out} ({out.stat().st_size / 1e6:.1f} MB)")
shutil.rmtree(ROOT / "build", ignore_errors=True)
