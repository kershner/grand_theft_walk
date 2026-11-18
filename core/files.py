"""Safe paths, file hashing, and atomic JSON/file operations."""

import hashlib
import json
import os
import shutil
import stat
import time
import uuid
from pathlib import Path, PureWindowsPath


def _retry_file_access(operation, *args, **kwargs):
    # Windows can briefly deny access while another process replaces the file.
    for attempt in range(6):
        try:
            return operation(*args, **kwargs)
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.005 * 2**attempt)


def read_json(path):
    return json.loads(_retry_file_access(path.read_text, encoding="utf-8"))


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
        _retry_file_access(temporary.write_bytes, data)
        _retry_file_access(temporary.replace, path)
    finally:
        _retry_file_access(temporary.unlink, missing_ok=True)


def atomic_json(path, value):
    atomic_bytes(path, (json.dumps(value, indent=2) + "\n").encode("utf-8"))


def put_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    finally:
        _retry_file_access(temporary.unlink, missing_ok=True)

