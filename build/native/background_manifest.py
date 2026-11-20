"""Pin the native bridge and stage its game components after compilation.

Build dependencies: x86 TinyCC, Python and pefile. WindowedMode v3.0.4's
official release ZIP must already be downloaded to runtime/windowed-mode.zip.
"""

import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

import pefile


ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / "core/native"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def main():
    sys.path.insert(0, str(ROOT))
    from build.build_suite import add_component

    bridge = NATIVE / "GTW.BackgroundInput.asi"
    pe = pefile.PE(str(bridge))
    rva = next(s.address for s in pe.DIRECTORY_ENTRY_EXPORT.symbols if s.name in (b"GTWInput", b"_GTWInput"))
    terrain_rva = next(s.address for s in pe.DIRECTORY_ENTRY_EXPORT.symbols
                       if s.name in (b"GTWTerrain", b"_GTWTerrain"))
    data = bridge.read_bytes()
    # A bridge rebuild changes the complete package fingerprint. Keep the exact
    # previous definition so normal repair can carry original backups forward.
    for game in ("gta_iii", "vice_city", "san_andreas"):
        installer = ROOT / "games" / game / "installer"
        current = json.loads((installer / "background.json").read_text(encoding="utf-8"))
        if next(entry['sha256'] for entry in current['files']
                if entry['path'] == 'scripts/GTW.BackgroundInput.asi') == digest(data):
            continue
        previous = add_component(None, game)
        path = installer / 'manifest.json'
        base = json.loads(path.read_text(encoding='utf-8'))
        versions = base.setdefault('previous_versions', [])
        if not any(entry['component_id'] == previous['component_id'] for entry in versions):
            versions.append({'component_id': previous['component_id'],
                             'files': {entry['path']: entry['sha256'] for entry in previous['files']}})
            write(path, base)
    write(NATIVE / "background.json", {"format": 1, "protocol": 1, "sha256": digest(data),
                                      "state_rva": rva, "terrain_rva": terrain_rva})
    archive_path = ROOT / "runtime/windowed-mode.zip"
    lock_path = ROOT / "build/native/windowed-mode.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if digest(archive_path.read_bytes()) != lock["sha256"]:
        raise ValueError("Windowed mode archive does not match its pinned release")
    with zipfile.ZipFile(archive_path) as archive:
        windowed = archive.read("III.VC.SA.WindowedMode.asi")
    for game in ("gta_iii", "vice_city", "san_andreas"):
        installer = ROOT / "games" / game / "installer"
        files = []
        for name, content in (("scripts/GTW.BackgroundInput.asi", data), ("scripts/III.VC.SA.WindowedMode.asi", windowed)):
            path = installer / "payload/background" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            files.append({"path": name, "sha256": digest(content), "size": len(content), "clean_sha256": None})
        write(installer / "background.json", {"format": 1, "package_id": "background-input-1", "files": files})
    # The existing builder verifies and distributes every entry in this manifest.
    manifest = json.loads((NATIVE / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"][bridge.name] = digest(data)
    write(NATIVE / "manifest.json", manifest)


if __name__ == "__main__":
    main()
