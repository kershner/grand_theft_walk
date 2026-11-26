"""Central entry point for installation checks and managed sessions."""

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

from core.files import write_state, component_mismatches, sha256, under
from core.winmemory import supervise
from games import ACTIVE_GAMES, ROOT, get_game


logger = logging.getLogger(__name__)


def validate_components(status, title):
    """Allow edited/rebuilt components, but require a complete installation."""
    if status["setup_matches"]:
        return
    mismatches = status.get("mismatches", [])
    blocked = [entry["path"] for entry in mismatches
               if entry.get("actual") is None or entry.get("expected") is None]
    if status.get("setup_error") or status.get("recovery_pending") or blocked or not mismatches:
        reason = status.get("setup_error") or ", ".join(blocked[:5]) or "manifest/recovery state"
        raise RuntimeError(f"{title}'s mod setup is incomplete ({reason}). Run suite repair.")
    logger.warning("%s has changed mod files: %s. Continuing with runtime compatibility checks.",
                   title, ", ".join(entry["path"] for entry in mismatches))


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
    background = installer / "background.json"
    if background.is_file():
        files += json.loads(background.read_text(encoding="utf-8"))["files"]
    camera = installer / "first-person.json"
    if game in ACTIVE_GAMES:
        if not camera.is_file():
            return None
        files += json.loads(camera.read_text(encoding="utf-8"))["files"]
    files += [{"path": name, "sha256": None} for name in manifest.get("disable", [])]
    return {
        "files": files,
        "required_files": ["data/script/main.scm", "models/gta3.img", "anim/anim.img"] if game == "san_andreas" else ["data/main.scm", "models/gta3.img", "anim/ped.ifp"],
        "required_hashes": {"data/timecycp.dat": manifest["ps2_timecycle_sha256"]} if "ps2_timecycle_sha256" in manifest else manifest.get("required_hashes", {}),
    }


def component_status(game, directory, root=ROOT):
    profile = definition(game, root)
    if not profile or not profile.get("files"):
        return {"setup_matches": False, "launch_ready": False, "component_count": 0, "mismatches": [],
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
            "launch_ready": not pending and all(entry["actual"] is not None and entry["expected"] is not None
                                                for entry in mismatches),
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
        "controller_ready": bool(matches and components["launch_ready"] and profile.controller_ready and profile.verified_executable),
    }


def session_command(game, baseline=False):
    profile = get_game(game)
    status = installation_status(game)
    if not status["executable_matches"]:
        raise RuntimeError(f"{profile.title} does not match its pinned classic executable profile.")
    validate_components(status, profile.title)
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
        path = profile.state_dir / "manager"
        return supervise(command, directory, seconds if baseline else None,
                         report, lambda value: write_state(path, value))


def read_game_text(path):
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def file_record(path):
    return {"size": path.stat().st_size, "sha256": sha256(path)}


def inspect_install(game, directory=None):
    profile = get_game(game)
    directory = Path(directory) if directory is not None else profile.install_dir
    executable = directory / profile.executable
    if not executable.is_file():
        raise FileNotFoundError(f"Game executable not found: {executable}")
    paths = {executable}
    # Inventory binaries/configuration and bootstrap/path data, without saves or credentials.
    for folder in (directory, directory / "scripts"):
        if folder.is_dir():
            paths.update(
                path for path in folder.iterdir()
                if path.is_file() and path.suffix.lower() in {".asi", ".dll", ".ini"}
            )
    for relative in ("version.txt", "data/timecyc.dat", "data/main.scm",
                     "data/script/main.scm", "data/gta.dat", "data/gta_vc.dat",
                     "data/gta3.dat", "data/paths/paths.ipl"):
        path = directory / relative
        if path.is_file():
            paths.add(path)
    files = {
        path.relative_to(directory).as_posix(): file_record(path)
        for path in sorted(paths)
    }
    # Only archive visual settings. Other mods' INIs can contain account tokens.
    configs = {
        path.relative_to(directory).as_posix(): read_game_text(path)
        for path in sorted(paths)
        if path.suffix.lower() == ".ini"
        and (path.stem.lower().startswith("skygfx")
             or path.stem.lower().startswith("silentpatch")
             or path.stem.lower().endswith(".widescreenfix"))
    }
    version = directory / "version.txt"
    return {
        "format": 1,
        "game": profile.id,
        "title": profile.title,
        "install_dir": str(directory.resolve()),
        "reported_version": read_game_text(version).strip() if version.exists() else None,
        "verified_executable": bool(
            profile.verified_executable
            and files[profile.executable]["sha256"] == profile.verified_executable
        ),
        "files": files,
        "configuration": configs,
    }


def main(argv=None, default_game=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action")
    subparsers.add_parser("status", help="Show all game profiles and readiness")
    inspect = subparsers.add_parser("inspect", help="Inventory a game installation without changing it")
    inspect.add_argument("game", choices=ACTIVE_GAMES)
    inspect.add_argument("--game-dir", type=Path)
    inspect.add_argument("--output", type=Path)
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
        elif args.action == "inspect":
            report = inspect_install(args.game, args.game_dir)
            rendered = json.dumps(report, indent=2) + "\n"
            if args.output:
                if args.output.resolve().is_relative_to(Path(report['install_dir'])):
                    raise ValueError('The inventory output must be outside the game installation.')
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(rendered, encoding='utf-8')
            else:
                print(rendered, end='')
        else:
            start_session(args.game, baseline=args.action == "test", seconds=getattr(args, "seconds", 120))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
