"""Build the personal offline suite ZIP from hash-pinned, Git-ignored payloads.

Uses the existing venv. No Python runtime, virtualenv, credentials or live game
state is copied. The extracted setup may install missing pinned Python packages
using installed Python. Run: venv/Scripts/python.exe build/build_suite.py
"""

import argparse
import configparser
import hashlib
import json
import os
import shutil
import sys
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.runtime import DEFAULTS
from core.files import sha256, under
from build.setup.setup import verify_bundle

GAMES = ("san_andreas", "vice_city")


def validate_graphics_capacity(game, payload):
    """Reject the reproduced SA maximum-distance load-crash configuration."""
    if game != "san_andreas":
        return
    distances = configparser.ConfigParser()
    distances.read(payload / "scripts/SALodLights.cfg", encoding="utf-8")
    maximum = any(distances.getfloat("DISTANCE", key, fallback=0) >= value
                  for key, value in (("ViewDistance", 3000), ("DrawDistance", 5),
                                     ("DetailDistance", 4)))
    if not maximum:
        return
    limits = configparser.ConfigParser()
    limits.read(payload / "scripts/III.VC.SA.LimitAdjuster.ini", encoding="utf-8")
    if not limits.has_section("SALIMITS"):
        raise ValueError("Maximum-distance San Andreas needs the scenery limit adjuster configuration.")
    # Live load exceeded the native 2500 dummy / 13000 building limits, then
    # exhausted single-link nodes. Keep the verified headroom in future builds.
    for key, minimum in (("Buildings", 32767), ("Dummys", 32767), ("PtrNodeSingle", 200000)):
        capacity = limits.get("SALIMITS", key, fallback="unlimited").strip().casefold()
        if capacity != "unlimited" and int(capacity) < minimum:
            raise ValueError(f"Maximum-distance San Andreas requires {key} >= {minimum} or unlimited; got {capacity}.")


def copy_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def add_component(stage, game):
    source = ROOT / "games" / game / "installer"
    base = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    manifests = [(base, source / "payload")]
    camera = source / "first-person.json"
    if game in ("san_andreas", "vice_city"):
        manifests.append((json.loads(camera.read_text(encoding="utf-8")), source / "payload/first_person"))
    graphics = source / "graphics.json"
    if graphics.is_file():
        validate_graphics_capacity(game, source / "payload/graphics")
        manifests.append((json.loads(graphics.read_text(encoding="utf-8")), source / "payload/graphics"))
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
            relative = f"games/{game}/installer/payload/{name}"
            copy_file(original, under(stage, relative))
            accepted = entry.get("accepted_before", [entry.get("clean_sha256")])
            if game == "vice_city" and name.lower() == "ddraw.dll":
                accepted.append(None)  # A fresh original game has no startup fix.
            files.append({"path": name, "payload": relative, "sha256": entry["sha256"],
                          "accepted_before": accepted})
    originals = {
        "MTLX.dll": "470f523814110ca249dbb84134194ddba48f0c76a85815ae16ef28fd956d62c7",
        "index.bin": "f584192253849361915898ee621e03248fab1e7af33b9030742336b8ad73fc45",
    } if game == "san_andreas" else {}
    for name in base.get("disable", []):
        expected = base.get("disabled_hashes", {}).get(name, originals.get(name))
        if expected is None:
            raise ValueError(f"Missing known hash for retired component: {game}/{name}")
        files.append({"path": name, "payload": None, "sha256": None, "accepted_before": [None, expected]})
    profile = json.loads((ROOT / "games" / game / "profile.json").read_text(encoding="utf-8"))
    fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:16]
    definition = {
        "component_id": f"{game}-{fingerprint}", "title": profile["title"],
        "executable": profile["executable"], "install_folder": profile["install_folder"],
        "controller_ready": profile["controller_ready"], "files": files,
        "controller_defaults": DEFAULTS,
        "required_files": ["models/gta3.img", "anim/anim.img", "data/script/main.scm"] if game == "san_andreas" else ["models/gta3.img", "anim/ped.ifp", "data/main.scm"],
        "required_hashes": {"data/timecycp.dat": base["ps2_timecycle_sha256"]} if game == "san_andreas" else {},
        "previous_versions": base.get("previous_versions", []),
    }
    installer = stage / "games" / game / "installer"
    installer.mkdir(parents=True, exist_ok=True)
    (installer / "manifest.json").write_text(json.dumps(definition, indent=2) + "\n", encoding="utf-8")
    if (source / "documentation").is_dir():
        for path in (source / "documentation").rglob("*"):
            if path.is_file():
                copy_file(path, installer / "documentation" / path.relative_to(source / "documentation"))
    records = installer / "documentation/source-manifests"
    records.mkdir(parents=True, exist_ok=True)
    for filename in ("manifest.json", "first-person.json", "graphics.json", "downloads.lock.json"):
        if (source / filename).is_file():
            copy_file(source / filename, records / filename)
    return definition


def cleanup_stage(stage):
    build_root = (ROOT / "build/staging").resolve()
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
    stage = ROOT / "build/staging" / ("suite-" + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    # Explicit inclusion lists keep runtime state and private local files out.
    for name in ("core", "games", "chat", "streaming", "tests"):
        folder = ROOT / name
        for path in folder.rglob("*.py"):
            parts = path.relative_to(folder).parts
            if any(part in {"runtime", "installer", "__pycache__"} for part in parts):
                continue
            copy_file(path, stage / name / path.relative_to(folder))
    # The packaged regression tests also exercise the builder's validation helpers.
    for name in ("Start Grand Theft Walk.cmd", "requirements.txt"):
        copy_file(ROOT / name, stage / name)
    for name in ("__init__.py", "build_suite.py", "setup/__init__.py", "setup/setup.py",
                 "setup/Setup.cmd", "setup/Setup.ps1", "native/build.ps1",
                 "native/save_bridge.c", "native/save_request.c", "native/save_shared.h"):
        copy_file(ROOT / "build" / name, stage / "build" / name)
    for game in GAMES:
        copy_file(ROOT / "games" / game / "profile.json", stage / "games" / game / "profile.json")
        sources = ROOT / "games" / game / "docs/SOURCES.txt"
        if sources.exists():
            copy_file(sources, stage / "games" / game / "docs/SOURCES.txt")
        else:
            path = stage / "games" / game / "docs/SOURCES.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Component origins and hashes: ../installer/documentation/\nController is not yet ready; memory writes are disabled.\n", encoding="utf-8")
    for path in (ROOT / "chat/templates").rglob("*"):
        if path.is_file():
            copy_file(path, stage / "chat/templates" / path.relative_to(ROOT / "chat/templates"))
    native = ROOT / "core/native"
    native_manifest = json.loads((native / "manifest.json").read_text(encoding="utf-8"))
    for name, expected in native_manifest["files"].items():
        if sha256(native / name) != expected:
            raise ValueError(f"Native helper failed verification: {name}")
    for name in ("manifest.json", *native_manifest["files"]):
        copy_file(native / name, stage / "core/native" / name)
    definitions = {game: add_component(stage, game) for game in GAMES}
    manifest = {"format": 1, "suite_version": "2026.11-sa-vc-max-distance1", "uses_existing_venv": True, "games": definitions}
    (stage / "suite-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    # The distributable keeps a convenient root entry point; implementation lives in build/setup.
    (stage / "Setup.cmd").write_text(
        '@echo off\nsetlocal DisableDelayedExpansion\ncall "%~dp0build\\setup\\Setup.cmd" %*\n',
        encoding="ascii",
    )
    copy_file(ROOT / "README.md", stage / "README.md")
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
