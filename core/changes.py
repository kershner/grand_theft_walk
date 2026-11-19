"""Reversible masked writes, guarded by each game's allocation identities."""


class RuntimeChanges:
    def __init__(self, memory):
        self.mem = memory
        self.entries = {}

    def valid_guard(self, guard):
        return guard is None

    def patch(self, address, after, mask=None, guard=None):
        if not self.valid_guard(guard):
            return
        mask = bytes([255] * len(after)) if mask is None else mask
        if len(mask) != len(after):
            raise ValueError("Patch mask must match the write size")
        current = self.mem.read(address, len(after))
        key = (address, len(after))
        entry = self.entries.get(key)
        if entry is None or entry[3] != guard:
            self.entries[key] = (current, after, mask, guard)
        value = bytes((old & ~bits) | (new & bits) for old, new, bits in zip(current, after, mask))
        if value != current:
            self.mem.write(address, value)

    def restore(self):
        failures = []
        for (address, size), (before, after, mask, guard) in reversed(list(self.entries.items())):
            try:
                if not self.valid_guard(guard):
                    continue
                current = self.mem.read(address, size)
                if any((now & bits) != (ours & bits) for now, ours, bits in zip(current, after, mask)):
                    continue
                value = bytes((now & ~bits) | (old & bits) for now, old, bits in zip(current, before, mask))
                self.mem.write(address, value)
            except OSError as error:
                failures.append(str(error))
        self.entries.clear()
        return failures
