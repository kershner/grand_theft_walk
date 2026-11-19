"""Synthetic profiles used only by protocol compatibility tests."""

from unittest.mock import patch
from games import GAMES, GameProfile


def legacy_game_profile():
    return patch.dict(GAMES, {"synthetic_legacy": GameProfile("synthetic_legacy", "GTA III", "gta3.exe", "Grand Theft Auto III")})
