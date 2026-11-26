"""SQLite app state, incremental exploration, and static file utilities."""

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path, PureWindowsPath


STATE_ROOT = Path(__file__).resolve().parents[1]
DATABASE = STATE_ROOT / 'runtime' / 'state.sqlite3'
_READY_DATABASES = set()


class StateStore:
    """One transactional database shared by controllers, chat and streaming."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else DATABASE

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        initialize = self.path not in _READY_DATABASES or not self.path.is_file()
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            if initialize:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE IF NOT EXISTS records ('
                           'key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL)')
                _READY_DATABASES.add(self.path)
            with db:
                yield db

    def read(self, key):
        with self.connect() as db:
            row = db.execute('SELECT value FROM records WHERE key=?', (key,)).fetchone()
        if row is None:
            raise FileNotFoundError(key)
        return json.loads(row[0])

    def write(self, key, value):
        with self.connect() as db:
            db.execute('INSERT INTO records VALUES (?,?,?) ON CONFLICT(key) DO UPDATE '
                       'SET value=excluded.value, updated=excluded.updated',
                       (key, json.dumps(value, separators=(',', ':'), allow_nan=False), time.time()))

    def delete(self, key):
        with self.connect() as db:
            db.execute('DELETE FROM records WHERE key=?', (key,))


def state_key(path):
    return os.path.normcase(os.path.abspath(path)).replace(chr(92), '/')


def read_state(path):
    return StateStore().read(state_key(path))


def write_state(path, value):
    StateStore().write(state_key(path), value)


def state_exists(path):
    with StateStore().connect() as db:
        return db.execute('SELECT 1 FROM records WHERE key=?', (state_key(path),)).fetchone() is not None


def delete_state(path):
    StateStore().delete(state_key(path))


def state_paths(directory):
    prefix = state_key(directory) + '/'
    with StateStore().connect() as db:
        return [Path(key) for key, in db.execute('SELECT key FROM records WHERE substr(key,1,?)=? ORDER BY key',
                (len(prefix), prefix)) if '/' not in key[len(prefix):]]


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


class ExplorationStore:
    """Incremental counters and bounded trails, isolated by game in the same DB."""

    def __init__(self, path, trail_limit=4096):
        self.scope = state_key(path)
        self.trail_limit = trail_limit
        with StateStore().connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS counters (scope TEXT, name TEXT, key TEXT, value INTEGER, '
                       'PRIMARY KEY(scope,name,key)) WITHOUT ROWID')
            db.execute('CREATE TABLE IF NOT EXISTS exploration (scope TEXT PRIMARY KEY, edges TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS trail (id INTEGER PRIMARY KEY, scope TEXT, x REAL, y REAL, z REAL)')
            db.execute('CREATE INDEX IF NOT EXISTS trail_scope ON trail(scope,id)')

    def connect(self):
        return sqlite3.connect(DATABASE, timeout=.1)

    def load(self):
        with closing(self.connect()) as db:
            row = db.execute('SELECT edges FROM exploration WHERE scope=?', (self.scope,)).fetchone()
            result = {'recent_edges': json.loads(row[0]) if row else [],
                      'recent_ground': [list(row) for row in db.execute(
                          'SELECT x,y,z FROM (SELECT id,x,y,z FROM trail WHERE scope=? '
                          'ORDER BY id DESC LIMIT ?) ORDER BY id', (self.scope, self.trail_limit))]}
            for name, key, count in db.execute('SELECT name,key,value FROM counters WHERE scope=?', (self.scope,)):
                result.setdefault(name, []).append(json.loads(key) + [count])
            return result

    def save(self, counters, recent_edges, recent_ground):
        batches = {name: set(counter.pending) for name, counter in counters.items()}
        rows, removed = [], []
        for name, keys in batches.items():
            for key in keys:
                encoded = json.dumps(key[0] + key[1] if name.startswith('edge_') else key, separators=(',', ':'))
                value = counters[name][key]
                if value > 0:
                    rows.append((self.scope, name, encoded, value))
                else:
                    removed.append((self.scope, name, encoded))
        with closing(self.connect()) as db, db:
            db.executemany('INSERT INTO counters VALUES (?,?,?,?) ON CONFLICT(scope,name,key) '
                           'DO UPDATE SET value=excluded.value', rows)
            db.executemany('DELETE FROM counters WHERE scope=? AND name=? AND key=?', removed)
            db.execute('INSERT INTO exploration VALUES (?,?) ON CONFLICT(scope) DO UPDATE SET edges=excluded.edges',
                       (self.scope, json.dumps([list(a+b) for a,b in recent_edges])))
            if recent_ground.cleared:
                db.execute('DELETE FROM trail WHERE scope=?', (self.scope,))
            db.executemany('INSERT INTO trail(scope,x,y,z) VALUES (?,?,?,?)',
                           [(self.scope, *point) for point in recent_ground.pending])
            db.execute('DELETE FROM trail WHERE scope=? AND id NOT IN '
                       '(SELECT id FROM trail WHERE scope=? ORDER BY id DESC LIMIT ?)',
                       (self.scope, self.scope, self.trail_limit))
        for name, keys in batches.items():
            counters[name].pending.difference_update(keys)
        recent_ground.pending.clear()
        recent_ground.cleared = False
