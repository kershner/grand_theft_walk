import struct


class MemoryFixture:
    def __init__(self):
        self.data = {}

    def read(self, address, length):
        return bytes(self.data.get(address + i, 0) for i in range(length))

    def write(self, address, data):
        self.data.update((address + i, value) for i, value in enumerate(data))

    def unpack(self, address, fmt):
        return struct.unpack("<" + fmt, self.read(address, struct.calcsize("<" + fmt)))

    def u32(self, address):
        return self.unpack(address, "I")[0]

    def byte(self, address):
        return self.read(address, 1)[0]
