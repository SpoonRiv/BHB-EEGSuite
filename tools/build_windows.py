"""Build the self-contained Windows app, then package base and music assets."""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from build_modules import ROOT, build


def main(asset_base_url):
    dist_dir = ROOT / "dist" / "windows"
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", "BHB-EEGSuite", "--distpath", str(dist_dir),
        "--workpath", str(ROOT / "build" / "pyinstaller"),
        "--specpath", str(ROOT / "build"),
        "--collect-all", "pylsl", "--collect-submodules", "uvicorn",
        "--hidden-import", "pyedflib", str(ROOT / "windows_launcher.py"),
    ], cwd=ROOT, check=True)

    base_dir = dist_dir / "BHB-EEGSuite"
    shutil.copytree(ROOT / "web", base_dir / "web", dirs_exist_ok=True)
    (base_dir / "configs" / "electrodes").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "configs" / "config.yaml", base_dir / "configs" / "config.yaml")
    shutil.copytree(ROOT / "configs" / "electrodes", base_dir / "configs" / "electrodes", dirs_exist_ok=True)
    shutil.copy2(ROOT / "README.md", base_dir / "README.md")
    (base_dir / "docs").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "docs" / "optional-modules.md", base_dir / "docs" / "optional-modules.md")
    build(asset_base_url, base_dir)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-base-url", required=True)
    main(parser.parse_args().asset_base_url)
