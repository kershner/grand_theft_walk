"""Offline, transactional installation and repair of pinned game profiles.

Only paths explicitly listed in suite-manifest.json are owned by this installer.
Original files and repair checkpoints stay on the destination computer.
"""

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path, PureWindowsPath

STATE_DIR = ".gtw-suite"


def sha256(path):
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"Expected a file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def component_mismatches(definition, directory):
    """Use the same installed-file checks for setup and launch readiness."""
    mismatches = []
    expected_files = [(entry["path"], entry["sha256"]) for entry in definition["files"]]
    expected_files += list(definition.get("required_hashes", {}).items())
    for name, expected in expected_files:
        actual = sha256(under(directory, name))
        if actual != expected:
            mismatches.append({"path": name, "expected": expected, "actual": actual})
    for name in definition.get("required_files", []):
        if not under(directory, name).is_file():
            mismatches.append({"path": name, "expected": "original game data", "actual": None})
    return mismatches


def under(root, name):
    """Reject traversal, drive paths, alternate streams and linked parents."""
    root = Path(root).absolute()
    windows = PureWindowsPath(name)
    if (not name or windows.is_absolute() or windows.drive or ":" in name
            or ".." in windows.parts or not windows.parts
            or any(part.endswith((" ", ".")) for part in windows.parts)):
        raise ValueError(f"Invalid package path: {name}")
    path = root.joinpath(*windows.parts)
    if not path.resolve().is_relative_to(root.resolve()) or path == root:
        raise ValueError(f"Path escapes installation: {name}")
    current = path
    while current != root.parent:
        if current.exists() or current.is_symlink():
            attributes = current.lstat()
            if stat.S_ISLNK(attributes.st_mode) or getattr(attributes, "st_file_attributes", 0) & 0x400:
                raise ValueError(f"Linked paths cannot be installed over: {current}")
        if current == root:
            break
        current = current.parent
    return path


def atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path, value):
    atomic_bytes(path, (json.dumps(value, indent=2) + "\n").encode("utf-8"))


def put_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def verify_bundle(root):
    index = json.loads((root / "bundle-index.json").read_text(encoding="utf-8"))
    if index.get("format") != 1 or not isinstance(index.get("files"), dict):
        raise ValueError("Unsupported or damaged bundle index")
    for name, expected in index["files"].items():
        if sha256(under(root, name)) != expected:
            raise ValueError(f"Archive file missing or damaged: {name}. Extract the ZIP again.")
    manifest = json.loads((root / "suite-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or not set(manifest.get("games", {})) <= {"san_andreas", "vice_city"}:
        raise ValueError("Unsupported suite manifest")
    return manifest


@contextlib.contextmanager
def open_bundle(root):
    """A development checkout uses its ZIP; an extracted suite uses itself."""
    root = root.absolute()
    if (root / "bundle-index.json").is_file():
        yield root
        return
    archive = root / "dist/Grand-Theft-Walk-Suite.zip"
    checksum = archive.with_suffix(".zip.sha256")
    if not archive.is_file() or not checksum.is_file():
        raise ValueError("The setup archive is missing. Build it with build_suite.py first.")
    if sha256(archive) != checksum.read_text(encoding="ascii").split()[0]:
        raise ValueError("The setup ZIP failed its checksum. Restore or rebuild the archive.")
    scratch = under(root, "build")
    scratch.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="setup-", dir=scratch) as temporary:
            stage = Path(temporary)
            if stage.resolve().parent != scratch.resolve():
                raise ValueError("Unexpected setup staging path")
            with zipfile.ZipFile(archive) as source:
                for entry in source.infolist():
                    target = under(stage, entry.filename)
                    if (entry.external_attr >> 16) & 0xF000 == 0xA000:
                        raise ValueError("Linked archive entries are not supported.")
                    if entry.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with source.open(entry) as incoming, target.open("wb") as outgoing:
                            shutil.copyfileobj(incoming, outgoing)
            yield stage / "Grand Theft Walk"
    finally:
        try:
            scratch.rmdir()
        except OSError:
            pass


class Installer:
    def __init__(self, bundle, directory, game, definition):
        self.bundle = Path(bundle).absolute()
        self.directory = Path(directory).absolute()
        self.game = game
        self.definition = definition
        self.state_path = under(self.directory, STATE_DIR + "/installed.json")
        self.pending_path = under(self.directory, STATE_DIR + "/pending.json")
        self.files = {}
        for entry in definition["files"]:
            name = entry["path"]
            under(self.directory, name)
            if name.casefold() in {key.casefold() for key in self.files} or PureWindowsPath(name).parts[0] == STATE_DIR:
                raise ValueError(f"Duplicate/reserved managed path: {name}")
            self.files[name] = entry

    def payload(self, entry):
        if entry["sha256"] is None:
            return None
        path = under(self.bundle, entry["payload"])
        if sha256(path) != entry["sha256"]:
            raise ValueError(f"Payload missing or damaged: {entry['path']}")
        return path

    def state(self):
        if not self.state_path.exists():
            return None
        record = json.loads(self.state_path.read_text(encoding="utf-8"))
        if (record.get("format") != 1 or record.get("game") != self.game
                or record.get("directory") != str(self.directory)
                or record.get("component_id") != self.definition["component_id"]
                or record.get("status") not in {"installed", "restored"}
                or len(record.get("entries", [])) != len(self.files)
                or {entry["path"] for entry in record.get("entries", [])} != set(self.files)):
            raise ValueError("This backup record belongs to another setup or game directory.")
        for entry in record["entries"]:
            if entry["after"] != self.files[entry["path"]]["sha256"]:
                raise ValueError("The backup record belongs to a different component version.")
            if entry["before"] is not None:
                backup = self.backup_path(entry["original"])
                if sha256(backup) != entry["before"]:
                    raise ValueError(f"Original backup missing or damaged: {entry['path']}")
        return record

    def backup_path(self, name):
        if not isinstance(name, str) or not name.replace("\\", "/").startswith(STATE_DIR + "/"):
            raise ValueError("Backup path is outside the suite backup directory.")
        return under(self.directory, name)

    def check(self):
        for entry in self.files.values():
            self.payload(entry)
        mismatches = component_mismatches(self.definition, self.directory)
        return {"game": self.game, "directory": str(self.directory), "ok": not mismatches,
                "mismatches": mismatches, "recovery_pending": self.pending_path.exists()}

    def recover(self):
        if not self.pending_path.exists():
            return
        pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
        if (pending.get("game") != self.game or pending.get("directory") != str(self.directory)
                or pending.get("component_id") != self.definition["component_id"]):
            raise ValueError("Interrupted setup record belongs to another installation.")
        changes = pending["changes"]
        if len({entry["path"] for entry in changes}) != len(changes):
            raise ValueError("Duplicate interrupted setup entries")
        for entry in changes:
            if entry["path"] not in self.files:
                raise ValueError("Interrupted setup contains an unmanaged path.")
            if entry["before"] is not None and sha256(self.backup_path(entry["backup"])) != entry["before"]:
                raise ValueError(f"Recovery checkpoint damaged: {entry['path']}")
        current_record = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else None
        committed = current_record == pending["next_state"] and all(
            sha256(under(self.directory, entry["path"])) == entry["after"] for entry in changes
        )
        if not committed:
            # Check the whole transaction before restoring anything.
            for entry in changes:
                if sha256(under(self.directory, entry["path"])) not in {entry["before"], entry["after"]}:
                    raise ValueError(f"File changed after setup was interrupted: {entry['path']}. Recovery checkpoint preserved.")
            for entry in reversed(changes):
                target = under(self.directory, entry["path"])
                if entry["before"] is None:
                    target.unlink(missing_ok=True)
                else:
                    put_file(self.backup_path(entry["backup"]), target)
            if pending["previous_state"] is None:
                self.state_path.unlink(missing_ok=True)
            else:
                atomic_json(self.state_path, pending["previous_state"])
        self.pending_path.unlink()

    def transaction(self, desired, previous, next_state, action):
        changes = []
        transaction_id = uuid.uuid4().hex
        for name, (expected, source) in desired.items():
            target = under(self.directory, name)
            before = sha256(target)
            if before == expected:
                continue
            backup = f"{STATE_DIR}/transactions/{transaction_id}/{name}"
            if before is not None:
                saved = under(self.directory, backup)
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(target, saved)
                if sha256(saved) != before:
                    raise ValueError(f"Repair checkpoint failed verification: {name}")
            changes.append({"path": name, "before": before, "after": expected,
                            "backup": backup, "source": str(source) if source else None})
        pending = {"format": 1, "game": self.game, "directory": str(self.directory),
                   "component_id": self.definition["component_id"], "action": action,
                   "previous_state": previous, "next_state": next_state, "changes": changes}
        atomic_json(self.pending_path, pending)
        try:
            for entry in changes:
                target = under(self.directory, entry["path"])
                if sha256(target) != entry["before"]:
                    raise ValueError(f"File changed during setup: {entry['path']}")
                if entry["after"] is None:
                    target.unlink(missing_ok=True)
                else:
                    put_file(Path(entry["source"]), target)
                if sha256(target) != entry["after"]:
                    raise ValueError(f"Installed checksum mismatch: {entry['path']}")
            atomic_json(self.state_path, next_state)
        except Exception:
            self.recover()
            raise
        self.pending_path.unlink()
        return {"game": self.game, "action": action, "changed": len(changes),
                "backup_record": str(self.state_path)}

    def apply(self, action):
        if action not in {"install", "repair", "restore"}:
            raise ValueError(f"Unknown setup action: {action}")
        self.recover()
        previous = self.state()
        if action == "restore":
            if previous is None:
                raise ValueError("No suite backup exists for this installation.")
            desired = {
                entry["path"]: (entry["before"], self.backup_path(entry["original"]) if entry["before"] else None)
                for entry in previous["entries"]
            }
            next_state = dict(previous, status="restored")
        else:
            for name in self.definition["required_files"]:
                if not under(self.directory, name).is_file():
                    raise ValueError(f"Install the original game first; missing {name}")
            for name, expected in self.definition.get("required_hashes", {}).items():
                if sha256(under(self.directory, name)) != expected:
                    raise ValueError(f"Unsupported original game data: {name}")
            desired = {name: (entry["sha256"], self.payload(entry) if entry["sha256"] else None)
                       for name, entry in self.files.items()}
            if previous is not None and previous["status"] == "installed":
                if action == "install" and not self.check()["ok"]:
                    raise ValueError("Installed files changed. Run Setup.cmd -Action Repair to back up changes and restore the pinned setup.")
                next_state = previous
            else:
                for name, entry in self.files.items():
                    current = sha256(under(self.directory, name))
                    if current not in {*entry["accepted_before"], entry["sha256"]}:
                        raise ValueError(f"Unrecognized existing file: {name}. Nothing was overwritten.")
                original_id = uuid.uuid4().hex
                entries = []
                for name, entry in self.files.items():
                    target = under(self.directory, name)
                    before = sha256(target)
                    original = f"{STATE_DIR}/originals/{original_id}/{name}"
                    if before is not None:
                        saved = under(self.directory, original)
                        saved.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(target, saved)
                        if sha256(saved) != before:
                            raise ValueError(f"Original backup failed verification: {name}")
                    entries.append({"path": name, "before": before, "after": entry["sha256"], "original": original})
                next_state = {"format": 1, "game": self.game, "directory": str(self.directory),
                              "component_id": self.definition["component_id"], "status": "installed", "entries": entries}
        return self.transaction(desired, previous, next_state, action)


def ensure_closed():
    import ctypes as c

    from gtw.winmemory import ProcessEntry, k32
    snapshot = k32.CreateToolhelp32Snapshot(2, 0)
    if snapshot == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    entry = ProcessEntry()
    entry.dwSize = c.sizeof(entry)
    try:
        ok = k32.Process32FirstW(snapshot, c.byref(entry))
        while ok:
            if entry.szExeFile.casefold() in {"launcher.exe", "gta_sa.exe", "gta-sa.exe", "gta-vc.exe"}:
                raise RuntimeError(f"Close {entry.szExeFile} before installing, repairing or restoring the suite.")
            ok = k32.Process32NextW(snapshot, c.byref(entry))
    finally:
        k32.CloseHandle(snapshot)


def run_setup(args, bundle):
    manifest = verify_bundle(bundle)
    if args.action != "check":
        ensure_closed()
    results = []
    locations = {}
    config = args.bundle / "runtime/installations.json"
    existing = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    for game, definition in manifest["games"].items():
        if args.game not in ("all", game):
            continue
        configured = args.san_andreas_dir if game == "san_andreas" else args.vice_city_dir
        directory = configured or (Path(existing[game]) if existing.get(game) else None) or Path("C:/Program Files/Rockstar Games") / definition["install_folder"]
        if not (directory / definition["executable"]).is_file() and not (directory / STATE_DIR / "installed.json").is_file():
            if args.game != "all" or configured is not None:
                raise ValueError(f"Game executable not found in {directory}")
            print(f"Skipping {definition['title']}: not installed at {directory}")
            continue
        installer = Installer(bundle, directory, game, definition)
        result = installer.check() if args.action == "check" else installer.apply(args.action)
        results.append(result)
        locations[game] = str(directory.absolute())
    if not results:
        raise ValueError("No installed games found. Supply the game paths to Setup.ps1.")
    if args.action in {"install", "repair"}:
        atomic_json(config, existing | locations)
    print(json.dumps(results, indent=2))
    return not (args.action == "check" and any(not result["ok"] or result["recovery_pending"] for result in results))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "check", "repair", "restore"))
    parser.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--game", choices=("all", "san_andreas", "vice_city"), default="all")
    parser.add_argument("--san-andreas-dir", type=Path)
    parser.add_argument("--vice-city-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        with open_bundle(args.bundle) as bundle:
            if not run_setup(args, bundle):
                parser.exit(1)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        parser.exit(1, f"Setup stopped: {error}\n")


if __name__ == "__main__":
    main()
