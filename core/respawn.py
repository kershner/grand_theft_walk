"""One-shot voted death requests and recovery through the native respawn cycle."""

import logging

from core.runtime import request_expired

logger = logging.getLogger(__name__)


class Respawn:
    def __init__(self, game, protection, session):
        self.game = game
        self.protection = protection
        self.session = session
        self.last_request = None
        self.active = False
        self.seen_dead = False
        self.started_at = 0.0

    def requested(self, request):
        return bool(
            isinstance(request, dict) and request.get("id")
            and request["id"] != self.last_request
            and request.get("session") == self.session
            and not request_expired(request)
        )

    def tick(self, request, state, now, saving=False):
        if self.active:
            living = bool(state.get("ready") and state.get("health", 0) > 0
                          and state.get("player_state") == 0)
            self.seen_dead = self.seen_dead or not living
            if self.seen_dead and living:
                self.active = False
                logger.info("Player respawned; normal controls resumed")
            elif now - self.started_at >= 30:
                self.active = False
                logger.warning("Native respawn is taking longer than expected; controls resumed")
            return self.active
        if not self.requested(request):
            return False
        if not state.get("ready"):
            return False
        if state.get("health", 0) <= 0 or state.get("player_state") != 0:
            self.last_request = request["id"]
            return False  # An existing native death already satisfies this request.
        if saving or not (state.get("foreground") or state.get("background_control")) or state.get("paused") or state.get("cutscene"):
            return False
        self.last_request = request["id"]
        if self.protection:
            failures = self.protection.restore()
            if failures:
                logger.error("Respawn cancelled: could not suspend protection: %s", failures)
                return False
        if not self.game.kill_player(state):
            logger.warning("Respawn cancelled: the current player changed")
            return False
        self.active, self.seen_dead, self.started_at = True, False, now
        logger.info("Voted respawn: player health cleared; waiting for native respawn")
        return True
