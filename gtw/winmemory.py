"""Windows process discovery, module enumeration, and process-memory access."""

import ctypes as c
import hashlib
import struct
import subprocess
import sys
from ctypes import wintypes as w
from pathlib import Path

from gtw.games import get_game

k32 = c.WinDLL("kernel32", use_last_error=True)
u32 = c.WinDLL("user32", use_last_error=True)


class ProcessEntry(c.Structure):
    _fields_ = [
        ("dwSize", w.DWORD),
        ("cntUsage", w.DWORD),
        ("th32ProcessID", w.DWORD),
        ("th32DefaultHeapID", c.c_size_t),
        ("th32ModuleID", w.DWORD),
        ("cntThreads", w.DWORD),
        ("th32ParentProcessID", w.DWORD),
        ("pcPriClassBase", w.LONG),
        ("dwFlags", w.DWORD),
        ("szExeFile", w.WCHAR * 260),
    ]


class ModuleEntry(c.Structure):
    _fields_ = [
        ("dwSize", w.DWORD),
        ("th32ModuleID", w.DWORD),
        ("th32ProcessID", w.DWORD),
        ("GlblcntUsage", w.DWORD),
        ("ProccntUsage", w.DWORD),
        ("modBaseAddr", c.c_void_p),
        ("modBaseSize", w.DWORD),
        ("hModule", w.HMODULE),
        ("szModule", w.WCHAR * 256),
        ("szExePath", w.WCHAR * 260),
    ]


def api(dll, name, args, result):
    function = getattr(dll, name)
    function.argtypes, function.restype = args, result
    return function


def elevate(arguments, directory):
    launch = api(
        c.windll.shell32,
        "ShellExecuteW",
        [w.HWND, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, c.c_int],
        c.c_void_p,
    )
    result = launch(
        None, "runas", sys.executable, subprocess.list2cmdline(arguments), str(directory), 1
    )
    if not result or result <= 32:
        raise RuntimeError("Windows administrator approval was cancelled.")


api(k32, "CreateToolhelp32Snapshot", [w.DWORD, w.DWORD], w.HANDLE)
api(k32, "Process32FirstW", [w.HANDLE, c.POINTER(ProcessEntry)], w.BOOL)
api(k32, "Process32NextW", [w.HANDLE, c.POINTER(ProcessEntry)], w.BOOL)
api(k32, "Module32FirstW", [w.HANDLE, c.POINTER(ModuleEntry)], w.BOOL)
api(k32, "Module32NextW", [w.HANDLE, c.POINTER(ModuleEntry)], w.BOOL)
api(k32, "OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
api(k32, "CloseHandle", [w.HANDLE], w.BOOL)
api(k32, "QueryFullProcessImageNameW", [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)], w.BOOL)
api(
    k32,
    "ReadProcessMemory",
    [w.HANDLE, c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)],
    w.BOOL,
)
api(
    k32,
    "WriteProcessMemory",
    [w.HANDLE, c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)],
    w.BOOL,
)
api(k32, "GetExitCodeProcess", [w.HANDLE, c.POINTER(w.DWORD)], w.BOOL)
api(u32, "GetForegroundWindow", [], w.HWND)
api(u32, "GetWindowThreadProcessId", [w.HWND, c.POINTER(w.DWORD)], w.DWORD)


def game_pids(game="san_andreas"):
    profile = get_game(game)
    snapshot = k32.CreateToolhelp32Snapshot(2, 0)
    if snapshot == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    entry = ProcessEntry()
    entry.dwSize = c.sizeof(entry)
    try:
        ok = k32.Process32FirstW(snapshot, c.byref(entry))
        while ok:
            if entry.szExeFile.lower() == profile.executable:
                yield entry.th32ProcessID
            ok = k32.Process32NextW(snapshot, c.byref(entry))
    finally:
        k32.CloseHandle(snapshot)


class Memory:
    def __init__(self, writable=False, pid=None, game="san_andreas"):
        self.profile = get_game(game)
        self.handle = None
        if writable and not self.profile.verified_executable:
            raise RuntimeError(f"Memory writes are not enabled for {self.profile.title}.")
        expected_executable = self.profile.verified_executable or (
            self.profile.inspection_executable if not writable else None
        )
        if not expected_executable:
            raise RuntimeError(f"No inspected executable is configured for {self.profile.title}.")
        matches = [pid] if pid else list(game_pids(game))
        if len(matches) != 1:
            raise RuntimeError(f"Start exactly one {self.profile.title} instance and load into the world.")
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
            if (self.exe.name.casefold() != self.profile.executable
                or hashlib.sha256(self.exe.read_bytes()).hexdigest() != expected_executable):
                raise RuntimeError(
                    f"Unsupported executable for {self.profile.title}; a verified build is required."
                )
        except Exception:
            self.close()
            raise

    def alive(self):
        code = w.DWORD()
        return bool(k32.GetExitCodeProcess(self.handle, c.byref(code)) and code.value == 259)

    def module(self, name):
        snapshot = k32.CreateToolhelp32Snapshot(0x18, self.pid)
        if snapshot == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        entry = ModuleEntry()
        entry.dwSize = c.sizeof(entry)
        try:
            ok = k32.Module32FirstW(snapshot, c.byref(entry))
            while ok:
                if entry.szModule.casefold() == name.casefold():
                    return {
                        "base": entry.modBaseAddr,
                        "size": entry.modBaseSize,
                        "path": Path(entry.szExePath),
                    }
                ok = k32.Module32NextW(snapshot, c.byref(entry))
            return None
        finally:
            k32.CloseHandle(snapshot)

    def close(self):
        if self.handle:
            k32.CloseHandle(self.handle)
            self.handle = None

    def read(self, address, length):
        if not 0x10000 <= address < 0x80000000 or not 0 < length <= 16 * 1024 * 1024:
            raise ValueError(f"Invalid 32-bit memory read: {address:#x}, {length}")
        data, transferred = c.create_string_buffer(length), c.c_size_t()
        if (
            not k32.ReadProcessMemory(self.handle, address, data, length, c.byref(transferred))
            or transferred.value != length
        ):
            raise c.WinError(c.get_last_error())
        return data.raw

    def write(self, address, data):
        if not self.writable:
            raise RuntimeError("This process handle is read-only.")
        if not 0x10000 <= address < 0x80000000 or not 0 < len(data) <= 4096:
            raise ValueError("Invalid 32-bit memory write.")
        buffer, transferred = c.create_string_buffer(data), c.c_size_t()
        if not k32.WriteProcessMemory(
            self.handle, address, buffer, len(data), c.byref(transferred)
        ) or transferred.value != len(data):
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
