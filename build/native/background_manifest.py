"""Pin the native bridge and stage its game components after compilation.

Build dependencies: x86 TinyCC, Python and pefile. WindowedMode v3.0.4's
official release ZIP must already be downloaded to runtime/windowed-mode.zip.
"""

import hashlib
import json
import shutil
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
    bridge = NATIVE / "GTW.BackgroundInput.asi"
    pe = pefile.PE(str(bridge))
    rva = next(s.address for s in pe.DIRECTORY_ENTRY_EXPORT.symbols if s.name in (b"GTWInput", b"_GTWInput"))
    data = bridge.read_bytes()
    write(NATIVE / "background.json", {"format": 1, "protocol": 1, "sha256": digest(data), "state_rva": rva})
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
