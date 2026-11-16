"""Central entry point for installation checks and managed sessions."""

import argparse
import json
import subprocess
import sys

from gtw.games import ACTIVE_GAMES, ROOT, get_game
from gtw.runtime import atomic_json
from gtw.setup import component_mismatches, sha256, under


def definition(game, root=ROOT):
    suite = root / "suite-manifest.json"
    if suite.is_file():
        manifest = json.loads(suite.read_text(encoding="utf-8"))
        if manifest.get("format") != 1:
            raise ValueError("Unsupported suite component manifest")
        return manifest.get("games", {}).get(game)
    # Development checkout: use the same pinned source manifests as the builder.
    installer = root / game / "installer"
    path = installer / "manifest.json"
    if not path.is_file():
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("format") != 1:
        raise ValueError("Unsupported game component manifest")
    files = list(manifest["files"])
    if game == "san_andreas":
        camera = installer / "first-person.json"
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
    return [sys.executable, str(ROOT / profile.id / "launch.py")], ROOT / profile.id


def start_session(game, baseline=False, seconds=120):
    if baseline and not 10 <= seconds <= 300:
        raise ValueError("Baseline test duration must be between 10 and 300 seconds.")
    command, directory = session_command(game, baseline)
    import ctypes

    from gtw.supervisor import supervise
    from gtw.winmemory import api, game_pids, k32, w
    api(k32, "CreateMutexW", [ctypes.c_void_p, w.BOOL, w.LPCWSTR], w.HANDLE)
    mutex = k32.CreateMutexW(None, False, "Local\\GrandTheftWalkManager")
    if not mutex or ctypes.get_last_error() == 183:
        if mutex:
            k32.CloseHandle(mutex)
        raise RuntimeError("Grand Theft Walk's central manager already has an active session.")
    try:
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
    finally:
        k32.CloseHandle(mutex)


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
