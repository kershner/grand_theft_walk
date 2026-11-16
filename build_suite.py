"""Build the personal offline suite ZIP from hash-pinned, Git-ignored payloads.

Uses the existing venv. No Python runtime, virtualenv, credentials or live game
state is copied. Run: venv/Scripts/python.exe build_suite.py
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from gtw.runtime import DEFAULTS
from gtw.setup import sha256, under, verify_bundle

GAMES = ("san_andreas", "vice_city")


def copy_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def add_component(stage, game):
    source = ROOT / game / "installer"
    base = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    manifests = [(base, source / "payload")]
    if game == "san_andreas":
        camera = json.loads((source / "first-person.json").read_text(encoding="utf-8"))
        manifests.append((camera, source / "payload/first_person"))
    files = []
    seen = set()
    for manifest, payload in manifests:
        for entry in manifest["files"]:
            name = entry["path"]
            if name.casefold() in seen:
                raise ValueError(f"Duplicate component destination: {game}/{name}")
            seen.add(name.casefold())
            original = under(payload, name)
            if sha256(original) != entry["sha256"]:
                raise ValueError(f"Source component missing or changed: {original}")
            relative = f"{game}/installer/payload/{name}"
            copy_file(original, under(stage, relative))
            accepted = [entry.get("clean_sha256")]
            if game == "vice_city" and name.lower() == "ddraw.dll":
                accepted.append(None)  # A fresh original game has no startup fix.
            files.append({"path": name, "payload": relative, "sha256": entry["sha256"],
                          "accepted_before": accepted})
    originals = {
        "MTLX.dll": "470f523814110ca249dbb84134194ddba48f0c76a85815ae16ef28fd956d62c7",
        "index.bin": "f584192253849361915898ee621e03248fab1e7af33b9030742336b8ad73fc45",
    } if game == "san_andreas" else {}
    for name in base.get("disable", []):
        expected = originals[name]
        files.append({"path": name, "payload": None, "sha256": None, "accepted_before": [None, expected]})
    profile = json.loads((ROOT / game / "profile.json").read_text(encoding="utf-8"))
    fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:16]
    definition = {
        "component_id": f"{game}-{fingerprint}", "title": profile["title"],
        "executable": profile["executable"], "install_folder": profile["install_folder"],
        "controller_ready": profile["controller_ready"], "files": files,
        "controller_defaults": DEFAULTS,
        "required_files": ["models/gta3.img", "anim/anim.img", "data/script/main.scm"] if game == "san_andreas" else ["models/gta3.img", "anim/ped.ifp", "data/main.scm"],
        "required_hashes": {"data/timecycp.dat": base["ps2_timecycle_sha256"]} if game == "san_andreas" else {},
    }
    installer = stage / game / "installer"
    installer.mkdir(parents=True, exist_ok=True)
    (installer / "manifest.json").write_text(json.dumps(definition, indent=2) + "\n", encoding="utf-8")
    if (source / "documentation").is_dir():
        for path in (source / "documentation").rglob("*"):
            if path.is_file():
                copy_file(path, installer / "documentation" / path.relative_to(source / "documentation"))
    records = installer / "documentation/source-manifests"
    records.mkdir(parents=True, exist_ok=True)
    for filename in ("manifest.json", "first-person.json"):
        if (source / filename).is_file():
            copy_file(source / filename, records / filename)
    return definition


def cleanup_stage(stage):
    build_root = (ROOT / "build").resolve()
    if stage.parent.resolve() != build_root or not stage.name.startswith("suite-"):
        raise ValueError(f"Refusing to clean an unexpected build path: {stage}")
    if stage.resolve().parent != build_root:
        raise ValueError("Build staging path resolves outside the build directory")
    for path in (stage, *stage.rglob("*")):
        info = path.lstat()
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Refusing to clean linked build contents: {path}")
    shutil.rmtree(stage)
    try:
        build_root.rmdir()  # Remove only if no other build artifacts remain.
    except OSError:
        pass


def build(keep_stage=False):
    stage = ROOT / "build" / ("suite-" + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    # Explicit inclusion lists keep runtime state and private local files out.
    for name in ("gtw", "san_andreas", "vice_city", "tests"):
        folder = ROOT / name
        for path in folder.rglob("*.py"):
            parts = path.relative_to(folder).parts
            if any(part in {"runtime", "installer", "__pycache__"} for part in parts):
                continue
            copy_file(path, stage / name / path.relative_to(folder))
    for name in ("gtw_chat_bot.py", "dev_server.py", "requirements.txt"):
        copy_file(ROOT / name, stage / name)
    for game in GAMES:
        copy_file(ROOT / game / "profile.json", stage / game / "profile.json")
        sources = ROOT / game / "docs/SOURCES.txt"
        if sources.exists():
            copy_file(sources, stage / game / "docs/SOURCES.txt")
        else:
            path = stage / game / "docs/SOURCES.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Component origins and hashes: ../installer/documentation/\nController is not yet ready; memory writes are disabled.\n", encoding="utf-8")
    for path in (ROOT / "templates").rglob("*"):
        if path.is_file():
            copy_file(path, stage / "templates" / path.relative_to(ROOT / "templates"))
    native = ROOT / "san_andreas/native"
    native_manifest = json.loads((native / "manifest.json").read_text(encoding="utf-8"))
    for name, expected in native_manifest["files"].items():
        if sha256(native / name) != expected:
            raise ValueError(f"Native helper failed verification: {name}")
    for name in ("manifest.json", "build.ps1", "save_bridge.c", "save_shared.h", "save_request.c", *native_manifest["files"]):
        copy_file(native / name, stage / "san_andreas/native" / name)
    definitions = {game: add_component(stage, game) for game in GAMES}
    manifest = {"format": 1, "suite_version": "2026.11-sa-vc-preview2", "uses_existing_venv": True, "games": definitions}
    (stage / "suite-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    copy_file(ROOT / "Setup.ps1", stage / "Setup.ps1")
    copy_file(ROOT / "Setup.cmd", stage / "Setup.cmd")
    copy_file(ROOT / "Launch Grand Theft Walk.cmd", stage / "Launch Grand Theft Walk.cmd")
    files = {path.relative_to(stage).as_posix(): sha256(path) for path in sorted(stage.rglob("*")) if path.is_file()}
    bundle_id = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    index = {"format": 1, "bundle_id": bundle_id, "files": files}
    (stage / "bundle-index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    verify_bundle(stage)
    output = ROOT / "dist/Grand-Theft-Walk-Suite.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                info = zipfile.ZipInfo("Grand Theft Walk/" + path.relative_to(stage).as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes())
    os.replace(temporary, output)
    digest = sha256(output)
    output.with_suffix(".zip.sha256").write_text(digest + "  " + output.name + "\n", encoding="ascii")
    if not keep_stage:
        cleanup_stage(stage)
    print(json.dumps({"archive": str(output), "sha256": digest, "bytes": output.stat().st_size,
                      "staging": str(stage) if keep_stage else None, "bundle_id": bundle_id, "files": len(files)}, indent=2))
    return output, stage if keep_stage else None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-stage", action="store_true", help="Keep the unpacked build for debugging")
    build(keep_stage=parser.parse_args().keep_stage)
