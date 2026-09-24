"""Build separate base and extension assets for a remote release."""

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "extensions" / "music"
DIST = ROOT / "dist"
BASE_FILES = ("main.py", "environment.yml", "README.md")
BASE_DIRS = ("core", "web", "configs")


def files(directory):
    return sorted(path for path in directory.rglob("*") if path.is_file()
                  and "__pycache__" not in path.parts and path.suffix not in (".pyc", ".pyo")
                  and path.name not in ("config.local.yaml", ".DS_Store"))


def write_zip(target, paths, prefix=None):
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=7) as archive:
        for path in paths:
            relative = path.relative_to(SOURCE if prefix else ROOT)
            archive.write(path, (Path(prefix) / relative if prefix else relative).as_posix())


def build(asset_base_url):
    if not asset_base_url.startswith("https://"):
        raise ValueError("--asset-base-url 必须是公开的 HTTPS Release 地址")
    manifest = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    DIST.mkdir(exist_ok=True)
    base = [ROOT / name for name in BASE_FILES]
    base.extend(sorted(ROOT.glob("ws_hub_*.py")))
    for name in BASE_DIRS:
        base.extend(files(ROOT / name))
    base.append(ROOT / "docs" / "optional-modules.md")
    base_archive = DIST / "BHB-EEGSuite-base.zip"
    write_zip(base_archive, base)
    with zipfile.ZipFile(base_archive) as archive:
        if any(name.startswith(("extensions/", "optional_modules/", "module_data/"))
               for name in archive.namelist()):
            raise ValueError("基础包包含扩展或本机安装目录")
    name = f"BHB-EEGSuite-music-{version}.zip"
    package = DIST / name
    write_zip(package, files(SOURCE), prefix="music")
    index = {"schema": 1, "modules": [{
        "id": "music", "version": version, "host_api": manifest["host_api"],
        "url": f"{asset_base_url.rstrip('/')}/{name}",
        "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
        "size": package.stat().st_size,
    }]}
    (DIST / "modules.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Built {package.name}, BHB-EEGSuite-base.zip and modules.json in {DIST}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-base-url", required=True, help="Release 资产目录，含 https://")
    args = parser.parse_args()
    try:
        build(args.asset_base_url)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
