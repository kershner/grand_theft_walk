"""Live walking controller, navigation modes, hotkeys, and runtime game settings."""
import argparse
import ctypes
import json
import math
from pathlib import Path
import random
import subprocess
import sys
import time
import uuid

from winmemory import Memory, k32, api, w
from controls import Keys, Hotkeys
from game import Game, RuntimeChanges, apply_protection, unlock_regions, unlock_interiors, apply_peaceful_mode, bind_walk_key
from navigation import PedestrianGraph, SidewalkNavigator, SmoothTurn, angle_delta, heading_to
from saves import Autosave, atomic_json
from camera import FirstPersonCamera

MODES = ("sidewalks", "free")
DEFAULTS = {"walking": False, "protection": True, "unlock_map": True, "quit": False,
            "mode": "sidewalks", "peaceful": True, "autosave": True, "first_person": False}
api(k32, "CreateMutexW", [ctypes.c_void_p, w.BOOL, w.LPCWSTR], w.HANDLE)


def distance(a, b):
    return math.hypot(a["x"]-b["x"], a["y"]-b["y"])


def request_admin(args):
    if ctypes.windll.shell32.IsUserAnAdmin():
        return False
    if args.elevated:
        raise RuntimeError("Windows did not grant access to the running game.")
    launch = ctypes.windll.shell32.ShellExecuteW
    launch.argtypes = [w.HWND, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, ctypes.c_int]
    launch.restype = ctypes.c_void_p
    parameters = [str(Path(__file__).resolve()), "run", "--state-dir", str(args.state_dir), "--elevated"]
    if args.walk:
        parameters.append("--walk")
    if args.seconds:
        parameters += ["--seconds", str(args.seconds)]
    if getattr(args, "mode", None) in MODES:
        parameters += ["--mode", args.mode]
    print("Accept the Windows administrator prompt so Python can control the running game.", flush=True)
    result = launch(None, "runas", sys.executable, subprocess.list2cmdline(parameters), str(Path(__file__).parent), 1)
    if not result or result <= 32:
        raise RuntimeError("Windows administrator approval was cancelled.")
    return True


class Walking:
    def __init__(self, graph=None, navigation_error=None, focused=None):
        self.keys = Keys({"forward": "w", "back": "s", "walk": "f2"}, focused=focused)
        self.origin = None
        self.progress_at = 0.
        self.reverse_until = 0.
        self.turn_after_reverse = 0
        self.recoveries = 0
        self.mode = None
        self.navigator = SidewalkNavigator(graph) if graph is not None else None
        self.navigation_error = navigation_error
        self.smooth_turn = SmoothTurn()
        self.manual_turn = 0
        self.turn_direction = 0
        self.aligning = False
        self.route_target = None
        self.best_target_distance = None
        self.approach_at = 0.

    def steer(self, direction, mode, state):
        usable = (state.get("ready") and state["foreground"] and not state["paused"]
                  and not state["controls_disabled"] and not state["in_vehicle"]
                  and state["health"] > 0 and state["player_state"] == 0)
        if not usable:
            return False
        if mode == "sidewalks":
            if self.navigator is None:
                return False
            self.navigator.turn = direction
        elif direction in ("left", "right"):
            self.manual_turn = max(-500, min(500, self.manual_turn + (-250 if direction == "left" else 250)))
        return True

    def stop(self):
        self.keys.release()
        self.origin = None
        self.reverse_until = 0.
        self.smooth_turn.reset()
        self.manual_turn = 0
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

    def tick(self, state, enabled, mode="free"):
        self.set_mode(mode)
        usable = (state.get("ready") and state["foreground"] and not state["paused"]
                  and not state["controls_disabled"] and state["player_state"] == 0
                  and state["health"] > 0 and not state["in_vehicle"] and state["ped_state"] in (0, 1))
        if not enabled or not usable:
            self.stop()
            return "stopped" if not enabled else "waiting for active on-foot gameplay"
        now = time.monotonic()
        position = state["position"]
        if self.reverse_until:
            if now < self.reverse_until:
                self.keys.set({"walk", "back"})
                return "backing away from obstacle"
            self.keys.release()
            if mode == "free":
                self.manual_turn = self.turn_after_reverse
            elif self.navigator:
                self.navigator.avoid_target(now)
            self.reverse_until = 0.
            self.origin, self.progress_at = position, now
        status = "walking freely"
        desired_turn = self.manual_turn * 2.
        if mode == "sidewalks":
            if self.navigator is None:
                self.stop()
                return "sidewalk paths unavailable; switch to free roam: " + str(self.navigation_error)
            waypoint = self.navigator.waypoint(
                (position["x"], position["y"], position["z"]), state["heading"], state["interior"], now)
            if waypoint is None:
                self.stop()
                return self.navigator.message
            facing = state.get("camera_heading")
            if facing is None:
                facing = state["heading"]
            error = angle_delta(heading_to((position["x"], position["y"]), waypoint), facing)
            if abs(error) > 2.6 and self.turn_direction:
                error = math.copysign(abs(error), self.turn_direction)
            elif abs(error) > .04:
                self.turn_direction = 1 if error > 0 else -1
            else:
                self.turn_direction = 0
            desired_turn = -error * 140 if abs(error) > .04 else 0.
            status = self.navigator.message
            self.aligning = abs(error) > (.25 if self.aligning else 1.)
            remaining = math.hypot(waypoint[0] - position["x"], waypoint[1] - position["y"])
            if self.route_target != self.navigator.target or self.best_target_distance is None:
                self.route_target = self.navigator.target
                self.best_target_distance, self.approach_at = remaining, now
            elif remaining < self.best_target_distance - .4:
                self.best_target_distance, self.approach_at = remaining, now
            elif not self.aligning and now - self.approach_at > 8:
                self.navigator.avoid_target(now)
                self.stop()
                return "rejoining pedestrian path after no route progress"
        turn = self.smooth_turn.step(desired_turn, now)
        if mode == "free":
            # Consume the queued nudge without overshooting its requested amount.
            if turn * self.manual_turn > 0:
                turn = int(math.copysign(min(abs(turn), abs(self.manual_turn)), turn))
                self.manual_turn -= turn
            elif not self.manual_turn:
                turn = 0
                self.smooth_turn.reset()
        if turn:
            self.keys.turn(turn)
        if mode == "sidewalks" and self.aligning:
            self.keys.set({"walk"})
            self.origin, self.progress_at, self.approach_at = position, now, now
            return "aligning with pedestrian path"
        self.keys.set({"walk", "forward"})
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


def run(args):
    try:
        mem = Memory(writable=True)
    except PermissionError:
        if request_admin(args):
            return
        raise
    mutex = k32.CreateMutexW(None, False, "Local\\SanAndreasWalker-" + str(mem.pid))
    if not mutex or ctypes.get_last_error() == 183:
        mem.close()
        if mutex:
            k32.CloseHandle(mutex)
        raise RuntimeError("A walker controller is already attached to this game.")
    game = Game(mem)
    protection, world, peaceful = RuntimeChanges(mem), RuntimeChanges(mem), RuntimeChanges(mem)
    bindings = RuntimeChanges(mem)
    try:
        graph, navigation_error = PedestrianGraph.load(mem.exe.parent), None
    except (OSError, ValueError) as error:
        graph, navigation_error = None, str(error)
    movement = Walking(graph, navigation_error, focused=mem.foreground)
    autosave = Autosave(mem, args.state_dir)
    camera = FirstPersonCamera(mem)
    settings = dict(DEFAULTS, walking=args.walk)
    try:
        previous = json.loads((args.state_dir / "control.json").read_text())
        if previous.get("mode") in MODES:
            settings["mode"] = previous["mode"]
        if isinstance(previous.get("first_person"), bool):
            settings["first_person"] = previous["first_person"]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    if getattr(args, "mode", None) in MODES:
        settings["mode"] = args.mode
    atomic_json(args.state_dir / "control.json", settings)
    hotkeys = Hotkeys(("f5", "f6", "f7", "f8", "f9", "f11", "f12"))
    started = time.monotonic()
    saved_at, barriers_at = 0., 0.
    barriers, interiors = 0, 0
    peace_at, neutral_npcs = 0., 0
    bindings_at = float("-inf")
    camera_at, camera_error, camera_request = 0., None, None
    print("Controller ready. Return to the game.\n"
          "F11 first-person | F5 sidewalks/free | F6 walk/pause | F7 left | F8 right | F9 stop | F12 exit\n"
          "Protection, peaceful mode, map and interior access enabled. Autosave every 60 seconds.", flush=True)
    print(f"Navigation: {settings['mode']} ({len(graph.nodes) if graph else 0} pedestrian nodes)", flush=True)
    last_message = None
    last_steering = None
    report = {}
    try:
        while mem.alive():
            now = time.monotonic()
            if args.seconds and now - started >= args.seconds:
                break
            try:
                requested = json.loads((args.state_dir / "control.json").read_text())
                settings.update({key: value for key,value in requested.items()
                                 if key in DEFAULTS and isinstance(DEFAULTS[key], bool) and isinstance(value, bool)})
                if requested.get("mode") in MODES:
                    settings["mode"] = requested["mode"]
                steering = requested.get("steering")
                if isinstance(steering, dict):
                    settings["steering"] = steering
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            if settings["quit"]:
                break
            state = game.snapshot()
            # Apply mode changes before processing a queued junction preference.
            movement.set_mode(settings["mode"])
            steering = settings.get("steering")
            if isinstance(steering, dict) and steering.get("id") != last_steering:
                last_steering = steering.get("id")
                direction, issued = steering.get("direction"), steering.get("issued")
                if isinstance(issued, (int, float)) and 0 <= time.time() - issued <= 10:
                    if direction == "stop":
                        settings["walking"] = False
                        movement.stop()
                    elif direction in ("left", "right", "forward"):
                        if direction == "forward":
                            settings["walking"] = True
                        movement.steer(direction, settings["mode"], state)
            for key in hotkeys.pressed():
                if key == "f9":
                    settings["walking"] = False
                elif key == "f12":
                    settings["quit"] = True
                elif state.get("ready") and state["foreground"] and not state["paused"]:
                    if key == "f11":
                        settings["first_person"] = not settings["first_person"]
                    elif key == "f5":
                        settings["mode"] = "free" if settings["mode"] == "sidewalks" else "sidewalks"
                    elif key == "f6":
                        settings["walking"] = not settings["walking"]
                    elif key in ("f7", "f8") and not state["controls_disabled"] and not state["in_vehicle"]:
                        movement.steer("left" if key == "f7" else "right", settings["mode"], state)
                atomic_json(args.state_dir / "control.json", settings)
            if settings["quit"]:
                break
            saving = autosave.tick(state, now, settings["autosave"])
            if state.get("ready"):
                if not saving and now - bindings_at > .75:
                    bind_walk_key(game, bindings)
                    bindings_at = now
                if (not saving and state["foreground"] and not state["paused"]
                        and (camera_request != settings["first_person"] or now - camera_at > .75)):
                    camera.apply(settings["first_person"])
                    camera_at = now
                    camera_request = settings["first_person"]
                    if settings["first_person"] and camera.error and camera.error != camera_error:
                        print(camera.error, flush=True)
                    camera_error = camera.error
                if settings["peaceful"] and not saving:
                    if now - peace_at > .75:
                        neutral_npcs = apply_peaceful_mode(game, peaceful)
                        peace_at = now
                elif not settings["peaceful"] and peaceful.entries and not saving:
                    peaceful.restore()
                    neutral_npcs = 0
                if settings["protection"] and state["health"] > 0 and state["player_state"] == 0 and not saving:
                    apply_protection(game, protection, state)
                elif protection.entries and not saving:
                    protection.restore()
                if settings["unlock_map"] and not saving:
                    if now - barriers_at > 0.75:
                        barriers = unlock_regions(game, world)
                        interiors = unlock_interiors(game, world)
                        barriers_at = now
                elif not settings["unlock_map"] and world.entries and not saving:
                    world.restore()
                    barriers = 0
                    interiors = 0
                status = movement.tick(state, settings["walking"], settings["mode"])
            else:
                movement.stop()
                status = state["reason"]
            if status != last_message:
                print(status, flush=True)
                last_message = status
            if now - saved_at > 0.75:
                # Read again so the status report includes the applied settings.
                report = game.snapshot()
                report.update(controller="running", walking=status, settings=settings,
                              barrier_entities=barriers, interior_portals=interiors,
                              neutral_npcs=neutral_npcs,
                              obstacle_recoveries=movement.recoveries, updated=time.time())
                report["navigation"] = movement.navigator.report() if movement.navigator else {"error": navigation_error}
                report["navigation"].update(aligning=movement.aligning,
                                            best_target_distance=movement.best_target_distance)
                report["autosave"] = autosave.report()
                report["camera"] = camera.report()
                atomic_json(args.state_dir / "state.json", report)
                saved_at = now
            time.sleep(1 / 120)
    finally:
        try:
            movement.stop()
        finally:
            failures = []
            autosave.close()
            if mem.alive():
                try:
                    camera.close()
                except OSError as error:
                    failures.append(str(error))
                failures += bindings.restore() + protection.restore() + world.restore() + peaceful.restore()
            report.update(controller="stopped", updated=time.time(), restore_errors=failures,
                          autosave=autosave.report())
            atomic_json(args.state_dir / "state.json", report)
            k32.CloseHandle(mutex)
            mem.close()
            print("Controller stopped; held keys released and temporary settings restored.", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("status", "run", "walk", "protect", "unlock", "peace", "autosave", "camera", "stop", "mode", "steer"), nargs="?", default="status")
    parser.add_argument("value", nargs="?")
    parser.add_argument("--mode", choices=MODES)
    parser.add_argument("--walk", action="store_true")
    parser.add_argument("--seconds", type=float)
    parser.add_argument("--state-dir", type=Path, default=Path(__file__).resolve().parent / "runtime")
    parser.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.state_dir = args.state_dir.resolve()
    if args.command == "status":
        mem = Memory()
        try:
            state = Game(mem).snapshot()
            print(json.dumps(state, indent=2))
        finally:
            mem.close()
    elif args.command == "run":
        run(args)
    else:
        path = args.state_dir / "control.json"
        if not path.exists():
            raise RuntimeError("Start the controller before sending it commands.")
        settings = json.loads(path.read_text())
        if args.command == "stop":
            settings["quit"] = True
        elif args.command == "mode":
            if args.value not in MODES:
                parser.error("Specify sidewalks or free.")
            settings["mode"] = args.value
        elif args.command == "camera":
            if args.value not in ("first", "third"):
                parser.error("Specify first or third.")
            settings["first_person"] = args.value == "first"
        elif args.command == "steer":
            if args.value not in ("left", "right", "forward", "stop"):
                parser.error("Specify left, right, forward or stop.")
            settings["steering"] = {"id": uuid.uuid4().hex, "direction": args.value, "issued": time.time()}
            if args.value in ("stop", "forward"):
                settings["walking"] = args.value == "forward"
        else:
            if args.value not in ("on", "off"):
                parser.error("Specify on or off.")
            key = {"walk": "walking", "protect": "protection", "unlock": "unlock_map", "peace": "peaceful", "autosave": "autosave"}[args.command]
            settings[key] = args.value == "on"
        atomic_json(path, settings)
        print("Requested:", settings)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print("Controller stopped:", error, file=sys.stderr)
        sys.exit(1)
