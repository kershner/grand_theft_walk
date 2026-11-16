"""Central entry point for installation checks and managed sessions."""

import argparse
import ctypes as c
import json
import subprocess
import sys
import time
from ctypes import wintypes as w

from core.runtime import atomic_json
from core.setup import component_mismatches, sha256, under
from games import ACTIVE_GAMES, ROOT, get_game


def definition(game, root=ROOT):
    suite = root / "suite-manifest.json"
    if suite.is_file():
        manifest = json.loads(suite.read_text(encoding="utf-8"))
        if manifest.get("format") != 1:
            raise ValueError("Unsupported suite component manifest")
        return manifest.get("games", {}).get(game)
    # Development checkout: use the same pinned source manifests as the builder.
    installer = root / "games" / game / "installer"
    path = installer / "manifest.json"
    if not path.is_file():
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("format") != 1:
        raise ValueError("Unsupported game component manifest")
    files = list(manifest["files"])
    graphics = installer / "graphics.json"
    if graphics.is_file():
        files += json.loads(graphics.read_text(encoding="utf-8"))["files"]
    camera = installer / "first-person.json"
    if game in ("san_andreas", "vice_city"):
        if not camera.is_file():
            return None
        files += json.loads(camera.read_text(encoding="utf-8"))["files"]
    files += [{"path": name, "sha256": None} for name in manifest.get("disable", [])]
    return {
        "files": files,
        "required_files": ["data/script/main.scm", "models/gta3.img", "anim/anim.img"] if game == "san_andreas" else ["data/main.scm", "models/gta3.img", "anim/ped.ifp"],
        "required_hashes": {"data/timecycp.dat": manifest["ps2_timecycle_sha256"]} if "ps2_timecycle_sha256" in manifest else {},
    }


def component_status(game, directory, root=ROOT):
    profile = definition(game, root)
    if not profile or not profile.get("files"):
        return {"setup_matches": False, "component_count": 0, "mismatches": [],
                "setup_error": "Pinned component manifest is missing. Install or re-extract the suite."}
    seen = set()
    for entry in profile["files"]:
        name = entry["path"]
        if name.casefold() in seen:
            raise ValueError(f"Duplicate managed component: {name}")
        seen.add(name.casefold())
    mismatches = component_mismatches(profile, directory)
    pending = under(directory, ".gtw-suite/pending.json").exists()
    return {"setup_matches": not mismatches and not pending,
            "component_count": len(profile["files"]), "mismatches": mismatches,
            "recovery_pending": pending}


def installation_status(game):
    profile = get_game(game)
    executable = profile.install_dir / profile.executable
    digest = sha256(executable) if executable.is_file() else None
    expected = profile.verified_executable or profile.inspection_executable
    matches = digest is not None and expected is not None and digest == expected
    components = component_status(game, profile.install_dir)
    return {
        "game": profile.id, "title": profile.title,
        "executable": str(executable), "sha256": digest,
        "executable_matches": matches,
        **components,
        "controller_ready": bool(matches and components["setup_matches"] and profile.controller_ready and profile.verified_executable),
    }


def session_command(game, baseline=False):
    profile = get_game(game)
    status = installation_status(game)
    if not status["executable_matches"]:
        raise RuntimeError(f"{profile.title} does not match its pinned classic executable profile.")
    if not status["setup_matches"]:
        changed = ", ".join(entry["path"] for entry in status["mismatches"][:5])
        raise RuntimeError(f"{profile.title}'s mod setup is incomplete or changed ({changed or 'manifest/recovery state'}). Run Setup.cmd -Action Repair -Game {game} before launching.")
    if baseline:
        return [status["executable"]], profile.install_dir
    if not status["controller_ready"]:
        raise RuntimeError(f"{profile.title}'s controller is not ready. Use 'test' for a bounded baseline launch.")
    return [sys.executable, str(profile.source_dir / "launch.py")], profile.source_dir


def start_session(game, baseline=False, seconds=120):
    if baseline and not 10 <= seconds <= 300:
        raise ValueError("Baseline test duration must be between 10 and 300 seconds.")
    command, directory = session_command(game, baseline)
    from core.winmemory import game_pids, process_mutex

    with process_mutex("Local\\GrandTheftWalkManager",
                       "Grand Theft Walk's central manager already has an active session."):
        for other in ACTIVE_GAMES:
            if list(game_pids(other)):
                raise RuntimeError(f"Close {get_game(other).title} before starting a managed session.")
        profile = get_game(game)
        report = {
            "game": game, "mode": "baseline_test" if baseline else "controller",
            "controller": "disabled" if baseline else "enabled",
            "timeout_seconds": seconds if baseline else None,
        }
        path = profile.state_dir / "manager.json"
        return supervise(command, directory, seconds if baseline else None,
                         report, lambda value: atomic_json(path, value))


class BasicLimits(c.Structure):
    _fields_ = [
        ("process_time", c.c_longlong), ("job_time", c.c_longlong),
        ("flags", w.DWORD), ("min_working_set", c.c_size_t),
        ("max_working_set", c.c_size_t), ("active_processes", w.DWORD),
        ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD),
    ]


class ExtendedLimits(c.Structure):
    _fields_ = [
        ("basic", BasicLimits), ("io_counters", c.c_ulonglong * 6),
        ("process_memory", c.c_size_t), ("job_memory", c.c_size_t),
        ("peak_process_memory", c.c_size_t), ("peak_job_memory", c.c_size_t),
    ]


class ProcessJob:
    """Windows closes only our child tree if the supervisor exits or fails."""

    def __init__(self):
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self.kernel.SetInformationJobObject.restype = w.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = w.BOOL
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.CloseHandle.restype = w.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)):
            error = c.WinError(c.get_last_error())
            self.close()
            raise error

    def add(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise c.WinError(c.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def watch(process, seconds, pressed, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + seconds if seconds is not None else None
    armed = False
    while process.poll() is None:
        down = pressed()
        if not down:
            armed = True
        reason = "f12" if armed and down else "timeout" if deadline is not None and clock() >= deadline else None
        if reason:
            process.kill()
            process.wait(timeout=5)
            return reason
        sleep(0.05)
    return "exited"


def supervise(command, directory, seconds, report, publish):
    user32 = c.WinDLL("user32", use_last_error=True)
    user32.GetAsyncKeyState.argtypes = [c.c_int]
    user32.GetAsyncKeyState.restype = c.c_short
    job = ProcessJob()  # Set up recovery before starting anything.
    process = None
    try:
        process = subprocess.Popen(command, cwd=directory)
        job.add(process)
        report.update(status="running", pid=process.pid)
        publish(report)
        reason = watch(process, seconds, lambda: bool(user32.GetAsyncKeyState(0x7B) & 0x8000))
        report.update(status="stopped", reason=reason, returncode=process.returncode)
        publish(report)
        return report
    finally:
        job.close()
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def main(argv=None, default_game=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action")
    subparsers.add_parser("status", help="Show all game profiles and readiness")
    for action in ("check", "launch", "test"):
        sub = subparsers.add_parser(action)
        sub.add_argument("game", choices=ACTIVE_GAMES, nargs="?" if default_game else None, default=default_game)
        if action == "test":
            sub.add_argument("--seconds", type=int, default=120)
    args = parser.parse_args(argv)
    try:
        if args.action in (None, "status"):
            print(json.dumps([installation_status(game) for game in ACTIVE_GAMES], indent=2))
        elif args.action == "check":
            result = installation_status(args.game)
            print(json.dumps(result, indent=2))
            if not result["executable_matches"] or not result["setup_matches"]:
                parser.exit(1)
        else:
            start_session(args.game, baseline=args.action == "test", seconds=getattr(args, "seconds", 120))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
