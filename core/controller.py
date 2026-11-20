"""Shared walking behavior, controller lifecycle, missions and command execution."""

import logging
import math
import time
import uuid
from collections import deque

from core.controls import (
    HighJump,
    Hotkeys,
    Keys,
    MOUSE_UNITS_PER_STEP,
    NativeInput,
    Respawn,
    ViewControl,
    can_control,
)
from core.exploration import ExploreNavigator
from core.files import atomic_json
from core.missions import MissionSystem
from core.navigation import SmoothTurn, angle_delta, heading_to
from core.runtime import DEFAULTS, DIRECTIONS, MODES, control_request, read_controls, request_expired
from core.terrain import NativeTerrain, SwimNavigator, TerrainNavigator, WaterAvoidance


logger = logging.getLogger(__name__)
SECONDS_PER_STEP = 1.0
WALK_SPEED_MULTIPLIER = 0.75
RECOVERY_ESCAPE_RADIUS = 8.0
JUMP_LANDING_GRACE = 3.0
JUMP_GROUND_SETTLE = .6
TERRAIN_WAIT_LIMIT = 1.


def distance(a, b):
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


class Walking:
    def __init__(self, graph=None, navigation_error=None, focused=None, bindings=None,
                 turn_multiplier=1, exploration_path=None):
        bindings = bindings if bindings is not None else {"forward": "w", "back": "s", "walk": "f2"}
        self.keys = Keys(bindings, focused=focused)
        self.walk_modifier = {"walk"} if "walk" in bindings else set()
        self.origin = None
        self.progress_at = 0.0
        self.recovery_target = None
        self.recovery_goal = None
        self.recovery_side = 0
        self.recovery_best_remaining = math.inf
        self.recovery_progress_at = 0.0
        self.last_waypoint = None
        self.recovery_anchor = None
        self.recovery_attempts = 0
        self.recoveries = 0
        self.jump_supported = False
        self.recovery_jump_setup = None
        self.recovery_jump_request = None
        self.recovery_jump_started = None
        self.recovery_jump_airborne = False
        self.recovery_jump_id = None
        self.recovery_jump_failed = False
        self.jump_seen_id = None
        self.jump_takeoff_event = False
        self.jump_takeoff_until = 0.0
        self.jump_landing_pending = False
        self.jump_route_origin = None
        self.jump_landing_since = None
        self.jump_grounded_since = None
        self.jump_grace_until = 0.0
        # A local jump budget survives pauses and temporary animation states.
        self.jump_sites = deque(maxlen=32)
        self.jump_budget_used = 0
        self.mode = None
        self.navigator = ExploreNavigator(graph, memory_path=exploration_path)
        self.navigation_error = navigation_error
        # Smooth in final mouse units so III/VC's sensitivity compensation
        # does not quantize every small correction into a four-unit jump.
        self.smooth_turn = SmoothTurn(speed=80 * turn_multiplier,
                                      acceleration=120 * turn_multiplier)
        self.turn_multiplier = turn_multiplier
        self.manual_turn = 0
        self.manual_direction = None
        self.manual_until = 0.0
        self.turn_direction = 0
        self.aligning = False
        self.terrain = None
        self.terrain_wait_since = None
        self.free_walk_until = 0.0
        self.route_retry_at = 0.0
        self.swim = None
        self.water_guard = None

    def steer(self, direction, mode, state, amount=1):
        if type(amount) is not int or not 1 <= amount <= 100:
            return False
        usable = can_control(state)
        if not usable:
            return False
        self.set_mode(mode)
        if mode in ("mission", "explore"):
            if self.navigator is None or direction not in ("left", "right", "forward"):
                return False
            self.navigator.turn = direction
            self.free_walk_until = 0.0
        elif direction in ("left", "right"):
            self.manual_turn += (
                (-1 if direction == "left" else 1) * amount * MOUSE_UNITS_PER_STEP
                * self.turn_multiplier
            )
        elif direction in ("forward", "back"):
            self.recovery_target = None
            self.manual_direction = direction
            self.manual_until = time.monotonic() + amount * SECONDS_PER_STEP
        else:
            return False
        return True

    def stop(self, preserve_recovery=False):
        self.keys.release()
        self.free_walk_until = 0.0
        self.origin = None
        self.terrain_wait_since = None
        self.recovery_target = None
        if not preserve_recovery:
            self.recovery_anchor = None
            self.recovery_attempts = 0
            self.recovery_goal = None
            self.recovery_side = 0
            self.jump_takeoff_event = False
            self.jump_takeoff_until = self.jump_grace_until = 0.0
            self.jump_landing_pending = False
            self.jump_route_origin = None
            self.jump_landing_since = None
            self.jump_grounded_since = None
        if self.terrain:
            self.terrain.reset()
        if self.swim:
            self.swim.observed_at = None
        self.recovery_jump_setup = None
        self.recovery_jump_request = self.recovery_jump_started = None
        self.recovery_jump_airborne = False
        self.recovery_jump_id = None
        self.recovery_jump_failed = False
        self.smooth_turn.reset()
        self.manual_turn = 0
        self.manual_direction = None
        self.manual_until = 0.0
        self.turn_direction = 0
        self.aligning = False

    def set_mode(self, mode):
        if mode != self.mode:
            self.stop()
            self.last_waypoint = None
            self.jump_sites.clear()
            self.jump_budget_used = 0
            if self.navigator:
                self.navigator.reset()
                self.navigator.exploring = mode == "explore"
                if mode != "mission":
                    self.navigator.set_destination(None)
            self.mode = mode

    def backing_up(self, now):
        return (
            (self.manual_direction == "back" and now < self.manual_until)
            or (self.recovery_jump_setup is not None and self.recovery_jump_setup[0] == "back")
        )

    def recover(self, position, now, state=None):
        self.free_walk_until = 0.0
        self.terrain_wait_since = None
        if self.terrain:
            self.terrain.reset()
        self.recoveries += 1
        if self.recovery_anchor is None:
            self.recovery_anchor = dict(position)
            self.recovery_goal = self.last_waypoint
        self.recovery_attempts += 1
        self.recovery_jump_setup = None
        self.recovery_jump_request = self.recovery_jump_started = None
        self.recovery_jump_airborne = False
        self.recovery_jump_id = None
        self.recovery_jump_failed = False
        # Remember returns to an obstacle, rather than refilling the budget
        # whenever a short detour crosses the recovery escape radius.
        self.jump_budget_used = sum(now - when < 300 and distance(position, origin) < 12
                                    and abs(position["z"] - origin["z"]) <= 3
                                    for origin, when in self.jump_sites)
        # Try clearing the obstacle before giving up this approach. Alternate
        # jumps with replanning so exploration cannot retry one wall forever.
        attempt_jump = self.recovery_attempts % 2 == 1
        grounded = state is None or (state.get("standing", True) and not state.get("in_air", False))
        if self.jump_supported and attempt_jump and grounded and self.jump_budget_used < 2:
            self.jump_sites.append((dict(position), now))
            self.jump_budget_used += 1
            self.recovery_jump_setup = ("back", now, dict(position))
            self.keys.set(self.walk_modifier | {"back"})
            self.recovery_target = None
            self.origin, self.progress_at = dict(position), now
            self.aligning = False
            self.smooth_turn.reset()
            if self.mode == "explore":
                self.navigator.progress_target = None
            return "obstacle recovery: backing up for jump"
        if self.mode == "explore":
            self.navigator.avoid_target(now, retreat=False)
            self.recovery_target = None
            self.origin = None
            self.aligning = False
            self.turn_direction = 0
            if self.navigator.returning:
                return "exploring: blocked approach; backtracking before another direction"
            if self.navigator.route_abandoned:
                return "exploring: route abandoned; choosing another direction"
            return "exploring: blocked approach; choosing another direction"
        self.plan_detour(position, now, state)
        self.origin, self.progress_at = dict(position), now
        self.aligning = False
        return "obstacle recovery: steering around obstacle"

    def plan_detour(self, position, now, state):
        """Probe a short corridor, favoring known paths and a consistent side.

        These are movement attempts, not collision raycasts. Failed bearings
        remain excluded while feedback from actual walking guides the detour.
        """
        origin = tuple(position[axis] for axis in ('x', 'y', 'z'))
        facing = (state or {}).get('camera_heading')
        if facing is None:
            facing = (state or {}).get('heading', self.navigator.course or 0)
        failed = self.recovery_target or self.last_waypoint
        bearing = heading_to(origin, failed) if failed is not None else facing
        self.navigator.failed_approaches.append((origin, bearing, now + 120))
        if self.mode == 'mission':
            self.navigator.avoid_target(now)
        options = []
        for step in range(16):
            direction = facing + step * math.tau / 16
            target = (origin[0] - math.sin(direction) * 6,
                      origin[1] + math.cos(direction) * 6, origin[2])
            if self.navigator.approach_blocked(origin, target, now):
                continue
            side = 1 if angle_delta(direction, bearing) > 0 else -1
            score = abs(angle_delta(direction, facing)) * 2
            if self.recovery_side and side != self.recovery_side:
                score += 3
            if self.recovery_goal is not None:
                score += math.dist(target[:2], self.recovery_goal[:2]) * .2
            node = self.navigator.graph.nearest(target, self.navigator.avoided, radius=5)
            if node is not None and abs(target[2] - node.position[2]) <= 3:
                score -= 2
            options.append((score, target, side))
        if options:
            _, self.recovery_target, self.recovery_side = min(options)
        else:
            self.recovery_target = self.navigator.escape_waypoint(origin, facing, now)
        self.recovery_best_remaining = math.dist(origin, self.recovery_target)
        self.recovery_progress_at = now

    def jump_feedback(self, request_id, result):
        if request_id is not None and request_id != self.jump_seen_id and result == 'accepted':
            self.jump_seen_id = request_id
            self.jump_takeoff_event = True
        if request_id == self.jump_seen_id and result in ('rejected', 'failed', 'cancelled'):
            self.jump_takeoff_event = False
            self.jump_takeoff_until = 0.0
        if request_id == self.recovery_jump_id and request_id is not None:
            if result in ("rejected", "failed", "cancelled"):
                self.recovery_jump_failed = True
            elif result == "airborne":
                self.recovery_jump_airborne = True

    def suspend_progress(self, position, now):
        """Exclude action time without forgiving the route's accumulated stalls."""
        resume_at = max(now, self.jump_grace_until)
        self.origin, self.progress_at = dict(position), resume_at
        self.recovery_progress_at = resume_at
        if self.navigator is not None:
            self.navigator.progress_at = resume_at
            self.navigator.journey_observed_at = None
            self.navigator.watchdogs_paused_until = max(now + .1, resume_at)

    def jump_motion(self, state, now):
        if self.jump_takeoff_event:
            self.jump_takeoff_event = False
            self.jump_takeoff_until = now + 2.1
        vertical_motion = (abs(state.get('vertical_speed', 0)) > .03
                           and (self.jump_landing_pending or self.recovery_jump_started is not None
                                or now < self.jump_takeoff_until))
        if state.get('in_air', False) or vertical_motion:
            if self.jump_route_origin is None:
                self.jump_route_origin = (self.navigator.last_position
                    or tuple(state['position'][axis] for axis in ('x', 'y', 'z')))
            self.jump_landing_pending = True
            self.jump_landing_since = None
            self.jump_grounded_since = None
            self.recovery_jump_airborne = True
            self.suspend_progress(state['position'], now)
            self.keys.set(self.walk_modifier | {'forward'})
            return 'obstacle recovery: jumping'
        if self.jump_landing_pending:
            if self.jump_landing_since is None:
                self.jump_landing_since = now
            landed = state.get('standing', True) and state['ped_state'] in (0, 1)
            if not landed:
                self.jump_grounded_since = None
            elif self.jump_grounded_since is None:
                self.jump_grounded_since = now
            settled = landed and now - self.jump_grounded_since >= JUMP_GROUND_SETTLE
            if not settled and now - self.jump_landing_since < 8:
                self.suspend_progress(state['position'], now)
                self.keys.set(self.walk_modifier | {'forward'})
                return 'obstacle recovery: recovering from landing'
            self.jump_landing_pending = False
            self.jump_grace_until = now + JUMP_LANDING_GRACE
        if now < self.jump_takeoff_until and self.recovery_jump_started is None:
            self.suspend_progress(state['position'], now)
            self.keys.set(self.walk_modifier | {'forward'})
            return 'jumping'
        if now < self.jump_grace_until:
            self.suspend_progress(state['position'], now)
            self.keys.set(self.walk_modifier | {'forward'})
            return 'obstacle recovery: recovering from landing'
        if self.jump_route_origin is not None:
            if self.mode == 'explore':
                position = tuple(state['position'][axis] for axis in ('x', 'y', 'z'))
                self.navigator.resume_after_jump(self.jump_route_origin, position,
                                                 state['heading'], now)
                self.recovery_target = self.last_waypoint = None
                self.recovery_jump_started = self.recovery_jump_request = None
                self.recovery_jump_failed = False
                self.aligning = False
                self.smooth_turn.reset()
                if self.terrain:
                    self.terrain.reset()
            self.jump_route_origin = None
        return None

    def prepare_recovery_jump(self, position, now, state=None):
        self.suspend_progress(position, now)
        phase, started, origin = self.recovery_jump_setup
        if phase == "back":
            # Bound the retreat even when another obstacle blocks the way.
            if distance(position, origin) < 1.5 and now - started < 1.5:
                self.keys.set(self.walk_modifier | {"back"})
                return "obstacle recovery: backing up for jump"
            self.recovery_jump_setup = ("forward", now, dict(position))
        elif now - started >= 0.25 or distance(position, origin) >= 0.5:
            # Request only after forward input has had time to turn the ped
            # around. Keep the approach short so takeoff precedes the wall.
            self.recovery_jump_setup = None
            if state is not None and (not state.get("standing", True) or state.get("in_air", False)):
                if now - started < 3:
                    self.recovery_jump_setup = (phase, started, origin)
                    self.keys.set(self.walk_modifier | {"forward"})
                    return "obstacle recovery: waiting for footing before jump"
                return self.recover(position, now, state)
            self.recovery_jump_request = control_request()
            self.recovery_jump_id = self.recovery_jump_request["id"]
            self.recovery_jump_started = now
            self.origin, self.progress_at = dict(position), now
            self.keys.set(self.walk_modifier | {"forward"})
            return "obstacle recovery: jumping"
        self.keys.set(self.walk_modifier | {"forward"})
        return "obstacle recovery: approaching jump"

    def swim_motion(self, state, enabled, now):
        if self.swim is None:
            return None
        was_swimming = self.swim.active
        usable = self.mode == 'explore' and enabled and can_control(state)
        if not self.swim.observe(state, now, usable):
            if was_swimming and not self.swim.active:
                self.navigator.reset()
                if self.terrain:
                    self.terrain.reset()
            return None
        self.recovery_jump_setup = self.recovery_jump_request = None
        self.recovery_jump_started = self.recovery_jump_id = None
        self.recovery_jump_failed = self.jump_landing_pending = False
        self.jump_takeoff_event = False
        self.jump_grace_until = self.jump_takeoff_until = 0.
        position = state['position']
        self.suspend_progress(position, now)
        target = self.swim.waypoint(tuple(position[axis] for axis in ('x', 'y', 'z')), now)
        if target is False:
            self.keys.set(self.walk_modifier)
        else:
            if target is not None:
                facing = state.get('camera_heading')
                facing = state['heading'] if facing is None else facing
                error = angle_delta(heading_to((position['x'], position['y']), target), facing)
                amount = self.smooth_turn.step(-error * 140 * self.turn_multiplier, now)
                self.keys.turn(amount)
                if abs(error) > 1.2:
                    self.keys.set(self.walk_modifier)
                    return 'swimming: aligning toward shore'
            self.keys.set(self.walk_modifier | {'forward'})
        return self.swim.status

    def begin_free_walk(self, state, now):
        self.free_walk_until, self.route_retry_at = now + 10, now + 1
        self.navigator.reset()
        self.navigator.course = state.get('camera_heading', state['heading'])
        self.terrain.reset()
        self.terrain_wait_since = None
        self.recovery_target = self.last_waypoint = None
        self.aligning = False
        self.smooth_turn.reset()
        self.origin, self.progress_at = dict(state['position']), now
        self.keys.set(self.walk_modifier | {'forward'})
        return 'exploring: walking freely'

    def tick(self, state, enabled, mode="free"):
        self.set_mode(mode)
        now = time.monotonic()
        if self.water_guard and mode == 'explore' and enabled and can_control(state):
            guarded = self.water_guard.tick(self, state, now)
            if guarded is not None:
                return guarded
        swimming = self.swim_motion(state, enabled, now)
        if swimming is not None:
            return swimming
        if can_control(state) and enabled:
            jumping = self.jump_motion(state, now)
            if jumping is not None:
                return jumping
        if self.recovery_jump_failed and can_control(state) and enabled:
            return self.recover(state["position"], now, state)
        if self.recovery_jump_started is not None and can_control(state) and enabled:
            age = now - self.recovery_jump_started
            airborne = state.get("in_air", False)
            self.recovery_jump_airborne |= airborne
            if age < 10 and (airborne or (not self.recovery_jump_airborne and age < 2.1)):
                self.suspend_progress(state['position'], now)
                self.keys.set(self.walk_modifier | {"forward"})
                return "obstacle recovery: jumping"
            self.recovery_jump_request = self.recovery_jump_started = None
            self.recovery_jump_id = None
            self.recovery_target = None
            if mode == "explore":
                self.navigator.progress_target = None
            self.suspend_progress(state['position'], now)
        usable = can_control(state) and state["ped_state"] in (0, 1)
        if not usable:
            transient = (state.get("ready") and state.get("health", 0) > 0
                         and state.get("player_state") == 0 and not state.get("in_vehicle"))
            self.stop(preserve_recovery=bool(transient and mode in ("explore", "mission")))
            return "waiting for active on-foot gameplay"
        if self.manual_direction and now >= self.manual_until:
            self.manual_direction = None
            self.keys.release()
            self.origin = None
        manual = mode == "free" and (self.manual_direction or self.manual_turn)
        if not enabled and not manual:
            self.stop()
            return "stopped"
        position = state["position"]
        if self.recovery_jump_setup is not None:
            return self.prepare_recovery_jump(position, now, state)
        # A short sidestep followed by returning to the same obstacle is still
        # the same stuck episode. Reset only after actually leaving the area.
        if (self.recovery_anchor is not None
                and distance(position, self.recovery_anchor) >= RECOVERY_ESCAPE_RADIUS):
            self.recovery_anchor = None
            self.recovery_attempts = 0
            self.recovery_goal = None
            self.recovery_side = 0
        if self.recovery_target is not None and distance(
                position, dict(zip(('x', 'y'), self.recovery_target))) <= 1.5:
            self.recovery_target = None
            self.origin, self.progress_at = dict(position), now
        if self.recovery_target is not None:
            remaining = math.dist(tuple(position[axis] for axis in ('x', 'y', 'z')),
                                  self.recovery_target)
            if remaining < self.recovery_best_remaining - .5:
                self.recovery_best_remaining, self.recovery_progress_at = remaining, now
            elif now - self.recovery_progress_at > 8:
                return self.recover(position, now, state)
        status = "walking freely"
        desired_turn = 0.0
        detouring = self.recovery_target is not None
        if mode == 'explore' and 0 < self.free_walk_until <= now:
            self.navigator.resume_after_free_walk(
                tuple(position[axis] for axis in ('x', 'y', 'z')), state['heading'], now)
            self.free_walk_until = 0
        freely = mode == 'explore' and now < self.free_walk_until and not detouring
        if freely and self.terrain and self.terrain.enabled and now >= self.route_retry_at:
            self.route_retry_at = now + .5
            point = tuple(position[axis] for axis in ('x', 'y', 'z'))
            target = self.navigator.join_forward_path(point, state['heading'], now)
            if target is not None and self.terrain.route(point, target, now) is not None:
                self.free_walk_until, freely = 0, False
        if freely:
            self.navigator.observe(tuple(position[axis] for axis in ('x', 'y', 'z')), now)
            status = 'exploring: walking freely'
        if (mode in ("mission", "explore") and not freely) or detouring:
            if self.navigator is None:
                self.navigator = ExploreNavigator()
            waypoint = self.recovery_target if detouring else self.navigator.waypoint(
                (position["x"], position["y"], position["z"]),
                state["heading"],
                state["interior"],
                now,
            )
            if waypoint is None:
                if mode == "explore" and self.navigator.recovery_requested:
                    self.navigator.recovery_requested = False
                    return self.recover(position, now, state)
                if mode == 'explore':
                    waypoint = self.navigator.off_path(
                        tuple(position[axis] for axis in ('x', 'y', 'z')), state['heading'], now)
                if waypoint is None:
                    self.plan_detour(position, now, state)
                    waypoint = self.recovery_target
                    detouring = True
            if not detouring:
                self.last_waypoint = waypoint
            if mode == 'explore' and self.terrain and self.terrain.enabled:
                safe = self.terrain.route(tuple(position[axis] for axis in ('x', 'y', 'z')),
                                          waypoint, now)
                if safe is None:
                    self.keys.set(self.walk_modifier)
                    self.smooth_turn.step(0, now)
                    if self.terrain_wait_since is None:
                        self.terrain_wait_since = now
                    self.navigator.watchdogs_paused_until = now + 2.5
                    self.navigator.journey_observed_at = None
                    self.navigator.progress_at = now
                    timed_out = now - self.terrain_wait_since >= TERRAIN_WAIT_LIMIT
                    if timed_out or self.terrain.blocked:
                        # Try ordinary walking before treating a sensor verdict
                        # as a real obstacle. The movement watchdog stays active.
                        return self.begin_free_walk(state, now)
                    return 'exploring: ' + self.terrain.status
                if self.terrain_wait_since is not None:
                    delay = now - self.terrain_wait_since
                    self.progress_at += delay
                    self.recovery_progress_at += delay
                    self.terrain_wait_since = None
                waypoint = safe
                # A valid detour can initially lead away from the graph goal.
                # Keep the physical movement watchdog active, while excluding
                # this planned detour from the straight-line goal watchdogs.
                self.navigator.watchdogs_paused_until = now + .2
                self.navigator.journey_observed_at = None
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
            status = ("obstacle recovery: walking around obstacle" if detouring else
                      self.terrain.status if mode == 'explore' and self.terrain and self.terrain.enabled
                      else self.navigator.message)
            self.aligning = abs(error) > (0.85 if self.aligning else 1.35)
        if mode == "free" and not detouring:
            turn, self.manual_turn = self.smooth_turn.nudge(self.manual_turn, now)
        else:
            turn = self.smooth_turn.step(desired_turn * self.turn_multiplier, now)
        if turn:
            self.keys.turn(turn)
        if mode == "free" and (self.manual_direction or not enabled):
            if self.manual_direction:
                self.keys.set(self.walk_modifier | {self.manual_direction})
                return f"stepping {self.manual_direction}"
            self.keys.release()
            return "turning" if self.manual_turn else "stopped"
        if (mode in ("mission", "explore") or detouring) and self.aligning:
            self.keys.set(self.walk_modifier)
            self.origin, self.progress_at = position, now
            self.navigator.progress_at = now
            self.navigator.watchdogs_paused_until = now + .1
            self.navigator.journey_observed_at = None
            if mode == "explore" and (self.navigator.local_target is not None or self.navigator.returning):
                if self.navigator.returning:
                    return "exploring open ground: aligning for backtracking"
                return "exploring open ground: aligning with route"
            return "aligning with pedestrian path"
        self.keys.set(self.walk_modifier | {"forward"})
        if self.origin is None or distance(position, self.origin) >= 0.8:
            self.origin, self.progress_at = position, now
        elif now - self.progress_at > (2.0 if mode == "explore" else 4.0):
            return self.recover(position, now, state)
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
        self.display = backend.RuntimeChanges(mem) if self.capabilities.hide_hud else None
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
                                bindings=getattr(backend, "WALK_KEYS", None),
                                turn_multiplier=getattr(backend, "TURN_MULTIPLIER", 1),
                                exploration_path=self.state_dir / "exploration.json")
        sensor = NativeTerrain(mem)
        self.movement.water_guard = WaterAvoidance(sensor)
        self.movement.navigator.open_terrain_chance = .15
        self.movement.terrain = TerrainNavigator(sensor)
        if mem.profile.id == 'san_andreas':
            self.movement.swim = SwimNavigator(sensor, self.movement.navigator.graph)
        destinations = []
        if graph:
            try:
                destinations = backend.load_destinations(mem.exe.parent, graph, backend.read_game_text)
            except (OSError, ValueError) as error:
                logger.warning("Mission locations unavailable: %s", error)
        self.missions = MissionSystem(self.state_dir, graph, destinations,
                                      defaults=getattr(backend, "MISSION_DEFAULTS", None))
        self.pace = backend.WalkingPace(
            mem, getattr(backend, "WALK_SPEED_MULTIPLIER", WALK_SPEED_MULTIPLIER)
        ) if self.capabilities.walking_pace else None
        self.autosave = autosave_factory(mem, self.state_dir) if self.capabilities.autosave else None
        self.camera = backend.FirstPersonCamera(mem) if self.capabilities.first_person else None
        self.jump = HighJump(self.game, self.bindings, binder=backend.bind_jump_key) if self.capabilities.jump else None
        self.movement.jump_supported = self.jump is not None
        self.view = ViewControl(focused=mem.foreground,
                                look_multiplier=getattr(backend, "LOOK_MULTIPLIER", 1)) if self.capabilities.view else None
        self.input = NativeInput(mem) if getattr(args, "background", False) else None
        if self.input:
            self.movement.keys = self.input.keys(self.movement.keys.bindings)
            if self.jump:
                self.jump.keys = self.input.keys(self.jump.keys.bindings)
            if self.view:
                self.view.keys = self.input.keys()
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
        self.report_write_blocked = False
        logger.info(
            "Controller ready. Background operation: %s.\n"
            "%sF5 free/explore/mission | F6 walk/pause | F7 left | F8 right | F9 stop | F12 exit\n"
            "Available features: %s",
            "enabled" if self.input else "disabled",
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
            if not state.get("foreground"):
                continue  # Function keys in another application belong to it.
            if key == "f9":
                self.stop()
            elif key == "f12":
                self.settings["quit"] = True
            elif state.get("ready") and state["foreground"] and not state["paused"]:
                if key == "f11" and self.capabilities.first_person:
                    self.settings["first_person"] = not self.settings["first_person"]
                elif key == "f5":
                    self.settings["mode"] = (
                        MODES[(MODES.index(self.settings["mode"]) + 1) % len(MODES)]
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
        if self.display is not None:
            self.backend.hide_hud(self.game, self.display)
        if self.capabilities.walk_binding and now - self.bindings_at > 0.75:
            self.backend.bind_walk_key(self.game, self.bindings)
            self.bindings_at = now
        requested_camera = (
            self.settings["first_person"],
            not self.movement.backing_up(now),
        )
        if (
            self.camera is not None
            and (state["foreground"] or state.get("background_control"))
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
            background=self.input.report() if self.input else {"enabled": False},
        )
        navigation = (
            movement.navigator.report()
            if movement.navigator
            else {"error": self.navigation_error}
        )
        navigation.update(
            aligning=movement.aligning,
            terrain=movement.terrain.report() if movement.terrain else {'enabled': False},
            swimming=movement.swim.report() if movement.swim else {'supported': False},
        )
        self.report["navigation"] = navigation
        self.report["missions"] = self.missions.report()
        self.publish_report()

    def publish_report(self):
        try:
            atomic_json(self.state_dir / "state.json", self.report)
        except PermissionError as error:
            if not getattr(self, "report_write_blocked", False):
                logger.warning("Status file is locked; continuing gameplay and retrying the next report: %s", error)
            self.report_write_blocked = True
        else:
            if getattr(self, "report_write_blocked", False):
                logger.info("Status file unlocked; report publication resumed")
            self.report_write_blocked = False

    def tick(self, now):
        result = self._tick(now)
        if self.input:
            self.input.publish()
        return result

    def _tick(self, now):
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
            recovery_jump = self.movement.recovery_jump_request
            request = recovery_jump if isinstance(recovery_jump, dict) else self.settings.get("jump")
            self.jump.tick(request, state, now, saving=saving,
                           auto_climb=self.settings['walking'] and self.capabilities.ledge_drop)
            self.movement.jump_feedback(self.jump.last_request, self.jump.result)
            if isinstance(recovery_jump, dict) and self.jump.last_request == recovery_jump["id"]:
                self.movement.recovery_jump_request = None
        if self.view:
            self.view.tick(self.settings.get("view"), state, now, saving=saving)
        holding = self.missions.tick(state, now, can_control(state), saving)
        if self.movement.navigator:
            active = self.missions.data["active"]
            self.movement.navigator.set_destination(
                tuple(active["node"])
                if active and self.settings["mode"] == "mission" else None
            )
        if state.get("ready"):
            self.update_game(state, now, saving)
            if holding and self.settings["mode"] == "mission":
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
        if status != getattr(self, 'pending_message', None):
            self.pending_message, self.message_since = status, now
        if status != self.last_message and (self.last_message is None or now - self.message_since >= .3):
            logger.info(status)
            self.last_message = status
        if now - self.saved_at > 0.75:
            self.write_report(status)
            self.saved_at = now
        return True

    def close(self):
        if self.movement.navigator:
            self.movement.navigator.save_memory(time.monotonic(), force=True)
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
            if self.input:
                try:
                    self.input.close()
                except OSError as error:
                    failures.append(str(error))
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
                    self.display,
                ):
                    if changes is not None:
                        failures.extend(changes.restore())
            self.report.update(
                controller="stopped",
                updated=time.time(),
                restore_errors=failures,
                autosave=self.autosave.report() if self.autosave else {"supported": False},
            )
            self.publish_report()
            logger.info(
                "Controller stopped; held keys released and temporary settings restored."
            )
