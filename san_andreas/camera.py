"""Version-checked control of ZMenu San Andreas's first-person camera."""
import hashlib

MODULE = "ZMenuSA.asi"
MODULE_SHA256 = "931718c7ef9eef64933ec60cbf7b4f8a5a59216bff6933770f4449a9def280d0"
FIRST_PERSON = 0x582EF2
ROTATE_PLAYER = 0x582F38


class FirstPersonCamera:
    def __init__(self, memory):
        self.mem = memory
        self.bound = None
        self.original = None
        self.enabled = None
        self.error = None

    def apply(self, enabled):
        try:
            module = self.mem.module(MODULE)
            if module is None:
                raise RuntimeError("First-person mod is not installed or loaded")
            if self.bound != module:
                if (module["size"] <= ROTATE_PLAYER
                        or hashlib.sha256(module["path"].read_bytes()).hexdigest() != MODULE_SHA256):
                    raise RuntimeError("Unsupported first-person mod version")
                current = tuple(self.mem.byte(module["base"] + offset) for offset in (FIRST_PERSON, ROTATE_PLAYER))
                if any(value not in (0, 1) for value in current):
                    raise RuntimeError("Invalid first-person camera flags")
                self.bound, self.original = module, current
            base = module["base"]
            values = ((ROTATE_PLAYER, 1), (FIRST_PERSON, 1)) if enabled else (
                (FIRST_PERSON, 0), (ROTATE_PLAYER, self.original[1]))
            for offset, value in values:
                if self.mem.byte(base + offset) != value:
                    self.mem.write(base + offset, bytes((value,)))
            self.enabled, self.error = enabled, None
            return True
        except (OSError, RuntimeError, ValueError) as error:
            self.error = str(error)
            return False

    def close(self):
        if self.bound is not None and self.mem.module(MODULE) == self.bound:
            for offset, value in zip((FIRST_PERSON, ROTATE_PLAYER), self.original):
                self.mem.write(self.bound["base"] + offset, bytes((value,)))

    def report(self):
        return {"available": self.bound is not None, "first_person": self.enabled, "error": self.error}
