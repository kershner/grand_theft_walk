"""Synthetic profiles used only by protocol compatibility tests."""

from unittest.mock import patch
from games import GAMES, GameProfile


def legacy_game_profile():
    return patch.dict(GAMES, {"gta_iii": GameProfile("gta_iii", "GTA III", "gta3.exe", "Grand Theft Auto III")})
