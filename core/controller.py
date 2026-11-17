"""Shared walking behavior, controller lifecycle, missions and command execution."""
import logging
import math
import random
import time
import uuid

from core.controls import (
    MOUSE_UNITS_PER_STEP,
    HighJump,
    Hotkeys,
    Keys,
    ViewControl,
    can_control,
)
from core.missions import MissionSystem
from core.respawn import Respawn
from core.navigation import SidewalkNavigator, SmoothTurn, angle_delta, heading_to
from core.runtime import (
    DEFAULTS,
    DIRECTIONS,
    MODES,
    atomic_json,
    read_controls,
    request_expired,
)

logger = logging.getLogger(__name__)
SECONDS_PER_STEP = 1.0
WALK_SPEED_MULTIPLIER = 0.75


def distance(a, b):
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


class Walking:
    def __init__(self, graph=None, navigation_error=None, focused=None, bindings=None):
        bindings = bindings if bindings is not None else {"forward": "w", "back": "s", "walk": "f2"}
        self.keys = Keys(bindings, focused=focused)
        self.walk_modifier = {"walk"} if "walk" in bindings else set()
        self.origin = None
        self.progress_at = 0.0
        self.reverse_until = 0.0
        self.turn_after_reverse = 0
        self.recoveries = 0
        self.mode = None
        self.navigator = SidewalkNavigator(graph) if graph is not None else None
        self.navigation_error = navigation_error
        self.smooth_turn = SmoothTurn()
        self.manual_turn = 0
        self.manual_direction = None
        self.manual_until = 0.0
        self.turn_direction = 0
        self.aligning = False
        self.route_target = None
        self.best_target_distance = None
        self.approach_at = 0.0

    def steer(self, direction, mode, state, amount=1):
        if type(amount) is not int or not 1 <= amount <= 100:
            return False
        usable = can_control(state)
        if not usable:
            return False
        self.set_mode(mode)
        if mode == "sidewalk":
            if self.navigator is None or direction not in ("left", "right", "forward"):
                return False
            self.navigator.turn = direction
        elif direction in ("left", "right"):
            self.manual_turn += (
                (-1 if direction == "left" else 1) * amount * MOUSE_UNITS_PER_STEP
            )
        elif direction in ("forward", "back"):
            self.reverse_until = 0.0
            self.manual_direction = direction
            self.manual_until = time.monotonic() + amount * SECONDS_PER_STEP
        else:
            return False
        return True

    def stop(self):
        self.keys.release()
        self.origin = None
        self.reverse_until = 0.0
        self.smooth_turn.reset()
        self.manual_turn = 0
        self.manual_direction = None
        self.manual_until = 0.0
        self.turn_direction = 0
        self.aligning = False
        self.route_target = None
        self.best_target_distance = None

    def set_mode(self, mode):
        if mode != self.mode:
            self.stop()
            if self.navigator:
                self.navigator.reset()
            self.mode = mode

    def backing_up(self, now):
        return (
            self.manual_direction == "back"
            and now < self.manual_until
            or now < self.reverse_until
        )

    def tick(self, state, enabled, mode="free"):
        self.set_mode(mode)
        usable = can_control(state) and state["ped_state"] in (0, 1)
        if not usable:
            self.stop()
            return "waiting for active on-foot gameplay"
        now = time.monotonic()
        if self.manual_direction and now >= self.manual_until:
            self.manual_direction = None
            self.keys.release()
            self.origin = None
        manual = mode == "free" and (self.manual_direction or self.manual_turn)
        if not enabled and not manual:
            self.stop()
            return "stopped"
        position = state["position"]
        if self.reverse_until:
            if now < self.reverse_until:
                self.keys.set(self.walk_modifier | {"back"})
                return "backing away from obstacle"
            self.keys.release()
            if mode == "free":
                self.manual_turn = self.turn_after_reverse
            elif self.navigator:
                self.navigator.avoid_target(now)
            self.reverse_until = 0.0
            self.origin, self.progress_at = position, now
        status = "walking freely"
        desired_turn = 0.0
        if mode == "sidewalk":
            if self.navigator is None:
                self.stop()
                return "sidewalk paths unavailable; switch to free roam: " + str(
                    self.navigation_error
                )
            waypoint = self.navigator.waypoint(
                (position["x"], position["y"], position["z"]),
                state["heading"],
                state["interior"],
                now,
            )
            if waypoint is None:
                self.stop()
                return self.navigator.message
            facing = state.get("camera_heading")
            if facing is None:
                facing = state["heading"]
            error = angle_delta(
                heading_to((position["x"], position["y"]), waypoint), facing
            )
            if abs(error) > 2.6 and self.turn_direction:
                error = math.copysign(abs(error), self.turn_direction)
            elif abs(error) > 0.04:
                self.turn_direction = 1 if error > 0 else -1
            else:
                self.turn_direction = 0
            desired_turn = -error * 140 if abs(error) > 0.04 else 0.0
            status = self.navigator.message
            self.aligning = abs(error) > (0.25 if self.aligning else 1.0)
            remaining = math.hypot(
                waypoint[0] - position["x"], waypoint[1] - position["y"]
            )
            if (
                self.route_target != self.navigator.target
                or self.best_target_distance is None
            ):
                self.route_target = self.navigator.target
                self.best_target_distance, self.approach_at = remaining, now
            elif remaining < self.best_target_distance - 0.4:
                self.best_target_distance, self.approach_at = remaining, now
            elif not self.aligning and now - self.approach_at > 8:
                self.navigator.avoid_target(now)
                self.stop()
                return "rejoining pedestrian path after no route progress"
        if mode == "free":
            turn, self.manual_turn = self.smooth_turn.nudge(self.manual_turn, now)
        else:
            turn = self.smooth_turn.step(desired_turn, now)
        if turn:
            self.keys.turn(turn)
        if mode == "free" and (self.manual_direction or not enabled):
            if self.manual_direction:
                self.keys.set(self.walk_modifier | {self.manual_direction})
                return f"stepping {self.manual_direction}"
            self.keys.release()
            return "turning" if self.manual_turn else "stopped"
        if mode == "sidewalk" and self.aligning:
            self.keys.set(self.walk_modifier)
            self.origin, self.progress_at, self.approach_at = position, now, now
            return "aligning with pedestrian path"
        self.keys.set(self.walk_modifier | {"forward"})
        if self.origin is None or distance(position, self.origin) >= 0.8:
            self.origin, self.progress_at = position, now
        elif now - self.progress_at > 4.0:
            self.keys.release()
            self.recoveries += 1
            self.reverse_until = now + 0.9
            self.turn_after_reverse = random.choice((-1, 1)) * random.randint(200, 550)
            self.smooth_turn.reset()
            return "obstacle recovery"
        return status


class Controller:
    def __init__(self, mem, args, backend, graph_loader, autosave_factory=None):
        self.backend = backend
        self.capabilities = backend.CAPABILITIES
        self.mem = mem
        self.game = backend.Game(mem)
        self.state_dir = args.state_dir
        self.control_path = self.state_dir / "control.json"
        self.session = uuid.uuid4().hex
        self.startup_ready = False
        self.protection = backend.RuntimeChanges(mem) if self.capabilities.protection else None
        self.respawn = Respawn(self.game, self.protection, self.session) if self.capabilities.respawn else None
        self.world = backend.RuntimeChanges(mem) if self.capabilities.unlock_map else None
        self.peaceful = backend.RuntimeChanges(mem) if self.capabilities.peaceful else None
        self.bindings = backend.RuntimeChanges(mem) if (
            self.capabilities.walk_binding or self.capabilities.jump
        ) else None
        self.navigation_error = None
        try:
            graph = graph_loader(mem)
        except (OSError, ValueError) as error:
            graph, self.navigation_error = None, str(error)
        self.movement = Walking(graph, self.navigation_error, focused=mem.foreground,
                                bindings=getattr(backend, "WALK_KEYS", None))
        destinations = []
        if graph:
            try:
                destinations = backend.load_destinations(mem.exe.parent, graph, backend.read_game_text)
            except (OSError, ValueError) as error:
                logger.warning("Mission locations unavailable: %s", error)
        self.missions = MissionSystem(self.state_dir, graph, destinations)
        self.pace = backend.WalkingPace(
            mem, getattr(backend, "WALK_SPEED_MULTIPLIER", WALK_SPEED_MULTIPLIER)
        ) if self.capabilities.walking_pace else None
        self.autosave = autosave_factory(mem, self.state_dir) if self.capabilities.autosave else None
        self.camera = backend.FirstPersonCamera(mem) if self.capabilities.first_person else None
        self.jump = HighJump(self.game, self.bindings, binder=backend.bind_jump_key) if self.capabilities.jump else None
        self.view = ViewControl(focused=mem.foreground) if self.capabilities.view else None
        self.hotkeys = Hotkeys(("f5", "f6", "f7", "f8", "f9", "f11", "f12"))
        self.settings = dict(DEFAULTS, walking=args.walk)
        previous = {}
        read_controls(self.control_path, previous)
        for key in ("mode", "first_person"):
            if key in previous:
                self.settings[key] = previous[key]
        if getattr(args, "mode", None) in MODES:
            self.settings["mode"] = args.mode
        self.limit_settings()
        atomic_json(self.control_path, self.settings)
        self.saved_at = self.world_at = self.peace_at = self.camera_at = 0.0
        self.bindings_at = float("-inf")
        self.barriers = self.interiors = self.neutral_npcs = 0
        self.camera_error = self.camera_request = None
        self.last_message = self.last_steering = None
        self.report = {}
        logger.info(
            "Controller ready. Return to the game.\n"
            "%sF5 sidewalk/free | F6 walk/pause | F7 left | F8 right | F9 stop | F12 exit\n"
            "Available features: %s",
            "F11 first-person | " if self.capabilities.first_person else "",
            ", ".join(name.replace("_", " ") for name, enabled in self.capabilities.report().items() if enabled) or "basic walking",
        )
        logger.info(
            "Navigation: %s (%s pedestrian nodes)",
            self.settings["mode"],
            len(graph.nodes) if graph else 0,
        )

    def limit_settings(self):
        for name in ("protection", "peaceful", "unlock_map", "autosave", "first_person"):
            if not getattr(self.capabilities, name):
                self.settings[name] = False
        for name in ("jump", "view", "respawn"):
            if not getattr(self.capabilities, name):
                self.settings.pop(name, None)

    def stop(self):
        self.settings["walking"] = False
        self.movement.stop()
        if self.jump:
            self.jump.cancel()
            self.jump.last_request = self.settings.get("jump", {}).get("id")
        if self.view:
            self.view.cancel()
            self.view.last_request = self.settings.get("view", {}).get("id")
        self.last_steering = self.settings.get("steering", {}).get("id")

    def handle_steering(self, state):
        request = self.settings.get("steering")
        if not isinstance(request, dict) or request.get("id") == self.last_steering:
            return
        direction = request.get("direction")
        if request_expired(request):
            self.last_steering = request.get("id")
        elif direction == "stop":
            self.stop()
        elif direction == "walk":
            self.settings["walking"] = True
            self.movement.stop()
            self.last_steering = request.get("id")
        elif direction in DIRECTIONS and self.movement.steer(
            direction, self.settings["mode"], state, request.get("amount", 1)
        ):
            self.last_steering = request.get("id")

    def handle_hotkeys(self, state):
        for key in self.hotkeys.pressed():
            if key == "f9":
                self.stop()
            elif key == "f12":
                self.settings["quit"] = True
            elif state.get("ready") and state["foreground"] and not state["paused"]:
                if key == "f11" and self.capabilities.first_person:
                    self.settings["first_person"] = not self.settings["first_person"]
                elif key == "f5":
                    self.settings["mode"] = (
                        "free" if self.settings["mode"] == "sidewalk" else "sidewalk"
                    )
                elif key == "f6":
                    self.settings["walking"] = not self.settings["walking"]
                    self.movement.stop()
                elif key in ("f7", "f8"):
                    self.movement.steer(
                        "left" if key == "f7" else "right",
                        self.settings["mode"],
                        state,
                        25,
                    )
            atomic_json(self.control_path, self.settings)

    def update_game(self, state, now, saving):
        if saving:
            return
        if self.capabilities.walk_binding and now - self.bindings_at > 0.75:
            self.backend.bind_walk_key(self.game, self.bindings)
            self.bindings_at = now
        requested_camera = (
            self.settings["first_person"],
            not self.movement.backing_up(now),
        )
        if (
            self.camera is not None
            and state["foreground"]
            and not state["paused"]
            and (self.camera_request != requested_camera or now - self.camera_at > 0.75)
        ):
            self.camera.apply(requested_camera[0], rotate_player=requested_camera[1])
            self.camera_at, self.camera_request = now, requested_camera
            if (
                self.settings["first_person"]
                and self.camera.error
                and self.camera.error != self.camera_error
            ):
                logger.warning("Camera: %s", self.camera.error)
            self.camera_error = self.camera.error
        if self.settings["peaceful"]:
            if now - self.peace_at > 0.75:
                self.neutral_npcs = self.backend.apply_peaceful_mode(self.game, self.peaceful)
                self.peace_at = now
        elif self.peaceful is not None and self.peaceful.entries:
            self.peaceful.restore()
            self.neutral_npcs = 0
        if (
            self.settings["protection"]
            and state["health"] > 0
            and state["player_state"] == 0
            and not state.get("in_water", False)
        ):
            self.backend.apply_protection(self.game, self.protection, state)
        elif self.protection is not None and self.protection.entries:
            self.protection.restore()
        if self.settings["unlock_map"]:
            if now - self.world_at > 0.75:
                self.barriers = self.backend.unlock_regions(self.game, self.world)
                self.interiors = self.backend.unlock_interiors(self.game, self.world)
                self.world_at = now
        elif self.world is not None and self.world.entries:
            self.world.restore()
            self.barriers = self.interiors = 0

    def write_report(self, status):
        movement = self.movement
        self.report = self.game.snapshot()
        if self.report.get("ready"):
            self.report["location"] = self.game.location(self.report)
            if movement.navigator and self.report["interior"] == 0:
                position = self.report["position"]
                nearby = movement.navigator.graph.nearest(
                    (position["x"], position["y"], position["z"]), radius=8
                )
                if nearby:
                    self.report["location"]["road"] = (
                        "near a pedestrian crossing"
                        if any(crossing for _, crossing in nearby.links)
                        else "near a pedestrian path"
                    )
        self.report.update(
            controller="running",
            game=self.mem.profile.id,
            protocol=1,
            session=self.session,
            startup_ready=self.startup_ready,
            walking=status,
            settings=self.settings,
            barrier_entities=self.barriers,
            interior_portals=self.interiors,
            neutral_npcs=self.neutral_npcs,
            obstacle_recoveries=movement.recoveries,
            updated=time.time(),
            capabilities=self.capabilities.report(),
            autosave=self.autosave.report() if self.autosave else {"supported": False},
            camera=self.camera.report() if self.camera else {"supported": False},
        )
        navigation = (
            movement.navigator.report()
            if movement.navigator
            else {"error": self.navigation_error}
        )
        navigation.update(
            aligning=movement.aligning,
            best_target_distance=movement.best_target_distance,
        )
        self.report["navigation"] = navigation
        self.report["missions"] = self.missions.report()
        atomic_json(self.state_dir / "state.json", self.report)

    def tick(self, now):
        read_controls(self.control_path, self.settings)
        self.limit_settings()
        if self.settings["quit"]:
            return False
        state = self.game.snapshot()
        self.startup_ready = self.startup_ready or bool(can_control(state))
        # Set the mode before consuming the next junction preference.
        self.movement.set_mode(self.settings["mode"])
        self.handle_steering(state)
        self.handle_hotkeys(state)
        if self.settings["quit"]:
            return False
        respawn_request = self.settings.get("respawn")
        respawn_pending = self.respawn and (self.respawn.active or self.respawn.requested(respawn_request))
        saving = self.autosave.tick(state, now, self.settings["autosave"] and not respawn_pending) if self.autosave else False
        if self.respawn and self.respawn.tick(respawn_request, state, now, saving=saving):
            self.movement.stop()
            if self.jump:
                self.jump.cancel()
                self.jump.last_request = self.settings.get("jump", {}).get("id")
            if self.view:
                self.view.cancel()
                self.view.last_request = self.settings.get("view", {}).get("id")
            if self.pace:
                self.pace.tick(state, False)
            if now - self.saved_at > 0.75:
                self.write_report("respawning")
                self.saved_at = now
            return True
        if self.jump:
            self.jump.tick(self.settings.get("jump"), state, now, saving=saving)
        if self.view:
            self.view.tick(self.settings.get("view"), state, now, saving=saving)
        holding = self.missions.tick(state, now, can_control(state), saving)
        if self.movement.navigator:
            active = self.missions.data["active"]
            self.movement.navigator.set_destination(
                tuple(active["node"]) if active else None
            )
        if state.get("ready"):
            self.update_game(state, now, saving)
            if holding and self.settings["mode"] == "sidewalk":
                self.movement.stop()
                status = "staying inside mission area"
            else:
                status = self.movement.tick(
                    state, self.settings["walking"], self.settings["mode"]
                )
        else:
            self.movement.stop()
            status = state["reason"]
        if self.pace:
            self.pace.tick(
                state,
                not saving
                and can_control(state)
                and state["standing"]
                and not state["in_air"]
                and bool(self.movement.keys.held & {"forward", "back"}),
            )
        if status != self.last_message:
            logger.info(status)
            self.last_message = status
        if now - self.saved_at > 0.75:
            self.write_report(status)
            self.saved_at = now
        return True

    def close(self):
        if self.view:
            self.view.cancel()
        try:
            try:
                if self.jump:
                    self.jump.cancel()
            finally:
                self.movement.stop()
        finally:
            failures = []
            if self.autosave:
                self.autosave.close()
            if self.mem.alive():
                try:
                    if self.pace:
                        self.pace.tick(self.game.snapshot(), False)
                except OSError as error:
                    failures.append(str(error))
                try:
                    if self.camera:
                        self.camera.close()
                except OSError as error:
                    failures.append(str(error))
                for changes in (
                    self.bindings,
                    self.protection,
                    self.world,
                    self.peaceful,
                ):
                    if changes is not None:
                        failures.extend(changes.restore())
            self.report.update(
                controller="stopped",
                updated=time.time(),
                restore_errors=failures,
                autosave=self.autosave.report() if self.autosave else {"supported": False},
            )
            atomic_json(self.state_dir / "state.json", self.report)
            logger.info(
                "Controller stopped; held keys released and temporary settings restored."
            )

