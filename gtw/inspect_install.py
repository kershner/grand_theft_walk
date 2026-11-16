"""Read-only installation inventory for choosing and verifying game profiles.

Use --game to select the profile to inspect.
An observed executable hash is evidence, not approval for memory writes.
"""

import argparse
import json
from pathlib import Path

from gtw.games import GAMES, get_game
from gtw.setup import sha256


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", choices=GAMES, required=True)
    parser.add_argument("--game-dir", type=Path)
    parser.add_argument("--output", type=Path, help="Write the report outside the game installation")
    args = parser.parse_args()
    try:
        report = inspect_install(args.game, args.game_dir)
        rendered = json.dumps(report, indent=2) + "\n"
        if args.output:
            installation = Path(report["install_dir"])
            if args.output.resolve().is_relative_to(installation):
                parser.error("The inventory output must be outside the game installation.")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
            print(f"Wrote {args.game} inventory: {args.output}")
        else:
            print(rendered, end="")
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
