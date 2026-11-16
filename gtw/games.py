"""Game profiles, backend capabilities, and isolated runtime locations."""

import json
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Capabilities:
    protection: bool = False
    peaceful: bool = False
    unlock_map: bool = False
    walk_binding: bool = False
    walking_pace: bool = False
    first_person: bool = False
    jump: bool = False
    autosave: bool = False
    view: bool = False

    def report(self):
        return asdict(self)


@dataclass(frozen=True)
class GameProfile:
    id: str
    title: str
    executable: str
    install_folder: str
    verified_executable: str | None = None
    inspection_executable: str | None = None
    controller_ready: bool = False

    @property
    def state_dir(self):
        return ROOT / self.id / "runtime"

    @property
    def install_dir(self):
        locations = ROOT / "runtime/installations.json"
        if locations.exists():
            configured = json.loads(locations.read_text(encoding="utf-8")).get(self.id)
            if configured:
                path = Path(configured)
                if not path.is_absolute():
                    raise ValueError(f"Configured game path must be absolute: {self.id}")
                return path
        return Path("C:/Program Files/Rockstar Games") / self.install_folder


def load_profile(game, root=ROOT):
    data = json.loads((root / game / "profile.json").read_text(encoding="utf-8"))
    if data.pop("format", None) != 1 or data.get("id") != game:
        raise ValueError(f"Invalid game profile: {game}")
    return GameProfile(**data)


ACTIVE_GAMES = ("san_andreas", "vice_city")
GAMES = {game: load_profile(game) for game in ACTIVE_GAMES}
# Legacy chat-protocol fixture; excluded from managed sessions.
GAMES["gta_iii"] = GameProfile("gta_iii", "GTA III", "gta3.exe", "Grand Theft Auto III")


def get_game(game="san_andreas"):
    try:
        return GAMES[game]
    except KeyError as error:
        raise ValueError(f"Unknown game: {game}. Choose {', '.join(GAMES)}.") from error
