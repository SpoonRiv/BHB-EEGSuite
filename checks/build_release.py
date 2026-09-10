"""Build separate source distributions for the host and optional music module."""
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
HOST_ENTRIES = (
    'main.py', 'ws_hub_eeg.py', 'ws_hub_impedance.py', 'ws_hub_psd.py',
    'ws_hub_variance.py', 'environment.yml', 'README.md',
    'core', 'configs', 'web', 'docs',
)
IGNORED_PARTS = {'__pycache__', 'node_modules'}


def files_under(path):
    candidates = [path] if path.is_file() else sorted(path.rglob('*'))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(ROOT)
        if IGNORED_PARTS.intersection(relative.parts):
            continue
        if candidate.suffix in {'.pyc', '.pyo', '.log'} or candidate.name == 'config.local.yaml':
            continue
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(ROOT):
            raise ValueError(f'External link cannot be packaged: {relative}')
        yield candidate, relative.as_posix()


def write_archive(output, entries):
    with ZipFile(output, 'w', compression=ZIP_DEFLATED) as archive:
        for entry in entries:
            for source, relative in files_under(ROOT / entry):
                archive.write(source, relative)
    print(f'{output} ({output.stat().st_size:,} bytes)')


def main():
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    package = ROOT / 'optional_modules/music'
    manifest = json.loads((package / 'manifest.json').read_text(encoding='utf-8'))
    version = str(manifest['version'])
    if not version or any(char not in '0123456789.-' for char in version):
        raise ValueError('Invalid module version')
    write_archive(output / 'BHB-EEGSuite-base.zip', HOST_ENTRIES)
    write_archive(output / f'BHB-EEGSuite-music-{version}.zip', ('optional_modules/music',))


if __name__ == '__main__':
    main()
