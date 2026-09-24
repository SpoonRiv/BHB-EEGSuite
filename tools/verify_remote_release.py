"""Verify publicly hosted module assets without changing an installation."""

import argparse
import tempfile
from pathlib import Path

from core.module_store import ModuleStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog_url", help="Public HTTPS URL of modules.json")
    args = parser.parse_args()

    store = ModuleStore(args.catalog_url)
    release = store.release("music")
    with tempfile.TemporaryDirectory(prefix="eegsuite-release-check-") as temporary:
        archive = Path(temporary) / "music.zip"
        store.download(release, archive, lambda *_: None)
        destination = Path(temporary) / "music"
        store.extract(archive, destination, "music")
        from core.modules import OptionalModuleManager

        manifest = OptionalModuleManager(temporary, None, None, None)._validate_package(destination)
        if manifest.get("version") != release["version"]:
            raise ValueError("模块包版本与远程索引不一致")
    print(f"Public release verified: music {release['version']} ({release['size']} bytes)")


if __name__ == "__main__":
    main()
