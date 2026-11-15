"""Windows process-memory access and keyboard/mouse input."""
import ctypes as c
from ctypes import wintypes as w
import hashlib
from pathlib import Path
import struct

EXPECTED_EXE = "a559aa772fd136379155efa71f00c47aad34bbfeae6196b0fe1047d0645cbd26"
k32 = c.WinDLL("kernel32", use_last_error=True)
u32 = c.WinDLL("user32", use_last_error=True)


class ProcessEntry(c.Structure):
    _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                ("th32DefaultHeapID", c.c_size_t), ("th32ModuleID", w.DWORD),
                ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
                ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD), ("szExeFile", w.WCHAR * 260)]


def api(dll, name, args, result):
    function = getattr(dll, name)
    function.argtypes, function.restype = args, result
    return function


api(k32, "CreateToolhelp32Snapshot", [w.DWORD, w.DWORD], w.HANDLE)
api(k32, "Process32FirstW", [w.HANDLE, c.POINTER(ProcessEntry)], w.BOOL)
api(k32, "Process32NextW", [w.HANDLE, c.POINTER(ProcessEntry)], w.BOOL)
api(k32, "OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
api(k32, "CloseHandle", [w.HANDLE], w.BOOL)
api(k32, "QueryFullProcessImageNameW", [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)], w.BOOL)
api(k32, "ReadProcessMemory", [w.HANDLE, c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)], w.BOOL)
api(k32, "WriteProcessMemory", [w.HANDLE, c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)], w.BOOL)
api(k32, "GetExitCodeProcess", [w.HANDLE, c.POINTER(w.DWORD)], w.BOOL)
api(u32, "GetForegroundWindow", [], w.HWND)
api(u32, "GetWindowThreadProcessId", [w.HWND, c.POINTER(w.DWORD)], w.DWORD)
api(u32, "GetAsyncKeyState", [c.c_int], c.c_short)


def game_pids():
    snapshot = k32.CreateToolhelp32Snapshot(2, 0)
    if snapshot == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    entry = ProcessEntry()
    entry.dwSize = c.sizeof(entry)
    try:
        ok = k32.Process32FirstW(snapshot, c.byref(entry))
        while ok:
            if entry.szExeFile.lower() == "gta_sa.exe":
                yield entry.th32ProcessID
            ok = k32.Process32NextW(snapshot, c.byref(entry))
    finally:
        k32.CloseHandle(snapshot)


class Memory:
    def __init__(self, writable=False, pid=None):
        matches = [pid] if pid else list(game_pids())
        if len(matches) != 1:
            raise RuntimeError("Start exactly one San Andreas instance and load into the world.")
        self.pid, self.writable = matches[0], writable
        self.handle = k32.OpenProcess(0x1010 | (0x28 if writable else 0), False, self.pid)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        try:
            size = w.DWORD(32768)
            name = c.create_unicode_buffer(size.value)
            if not k32.QueryFullProcessImageNameW(self.handle, 0, name, c.byref(size)):
                raise c.WinError(c.get_last_error())
            self.exe = Path(name.value)
            if hashlib.sha256(self.exe.read_bytes()).hexdigest() != EXPECTED_EXE:
                raise RuntimeError("Unsupported executable: this profile requires the verified modded 1.0 build.")
            self.signature = self.read(0x400000, 64).hex()
        except Exception:
            self.close()
            raise

    def alive(self):
        code = w.DWORD()
        return bool(k32.GetExitCodeProcess(self.handle, c.byref(code)) and code.value == 259)

    def close(self):
        if self.handle:
            k32.CloseHandle(self.handle)
            self.handle = None

    def read(self, address, length):
        if not 0x10000 <= address < 0x80000000 or not 0 < length <= 16 * 1024 * 1024:
            raise ValueError(f"Invalid 32-bit memory read: {address:#x}, {length}")
        data, transferred = c.create_string_buffer(length), c.c_size_t()
        if not k32.ReadProcessMemory(self.handle, address, data, length, c.byref(transferred)) or transferred.value != length:
            raise c.WinError(c.get_last_error())
        return data.raw

    def write(self, address, data):
        if not self.writable:
            raise RuntimeError("This process handle is read-only.")
        if not 0x10000 <= address < 0x80000000 or not 0 < len(data) <= 4096:
            raise ValueError("Invalid 32-bit memory write.")
        buffer, transferred = c.create_string_buffer(data), c.c_size_t()
        if not k32.WriteProcessMemory(self.handle, address, buffer, len(data), c.byref(transferred)) or transferred.value != len(data):
            raise c.WinError(c.get_last_error())

    def unpack(self, address, fmt):
        return struct.unpack("<" + fmt, self.read(address, struct.calcsize("<" + fmt)))

    def u32(self, address):
        return self.unpack(address, "I")[0]

    def byte(self, address):
        return self.read(address, 1)[0]

    def foreground(self):
        pid = w.DWORD()
        u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), c.byref(pid))
        return pid.value == self.pid


class MouseInput(c.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", c.c_size_t)]


class KeyInput(c.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", c.c_size_t)]


class HardwareInput(c.Structure):
    _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]


class InputUnion(c.Union):
    _fields_ = [("mi", MouseInput), ("ki", KeyInput), ("hi", HardwareInput)]


class Input(c.Structure):
    _fields_ = [("type", w.DWORD), ("data", InputUnion)]


api(u32, "SendInput", [w.UINT, c.POINTER(Input), c.c_int], w.UINT)


class Keys:
    # Physical scan codes: W, S and Left Alt (the default pedestrian walk modifier).
    SCANS = {"forward": 0x11, "back": 0x1F, "walk": 0x38}

    def __init__(self):
        self.held = set()

    def set(self, desired):
        desired = set(desired)
        for key in sorted(self.held - desired):
            self._send(key, False)
            self.held.remove(key)
        for key in sorted(desired - self.held, key=lambda name: name != "walk"):
            self._send(key, True)
            self.held.add(key)

    def _send(self, name, down):
        event = Input(type=1, data=InputUnion(ki=KeyInput(0, self.SCANS[name], 0x8 | (0 if down else 0x2), 0, 0)))
        if u32.SendInput(1, c.byref(event), c.sizeof(Input)) != 1:
            raise RuntimeError("Windows rejected game input. Run controller and game at the same privilege level.")

    def turn(self, amount):
        event = Input(type=0, data=InputUnion(mi=MouseInput(int(amount), 0, 0, 0x1, 0, 0)))
        if u32.SendInput(1, c.byref(event), c.sizeof(Input)) != 1:
            raise RuntimeError("Windows rejected mouse input.")

    def release(self):
        self.set(set())
