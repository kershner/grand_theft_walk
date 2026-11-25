"""Windows process ownership, discovery, memory access, and reversible writes."""

import ctypes as c
import hashlib
import struct
import subprocess
import sys
import time
from contextlib import contextmanager
from ctypes import wintypes as w
from pathlib import Path

from games import ROOT, get_game


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
api(k32, "CreateMutexW", [c.c_void_p, w.BOOL, w.LPCWSTR], w.HANDLE)
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


@contextmanager
def process_mutex(name, message):
    handle = k32.CreateMutexW(None, False, name)
    if not handle or c.get_last_error() == 183:
        if handle:
            k32.CloseHandle(handle)
        raise RuntimeError(message)
    try:
        yield
    finally:
        k32.CloseHandle(handle)


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


def module_export(memory, module, symbol, length):
    """Resolve a data export from the loaded PE32 image, including rebuilt mods."""
    base, size = module["base"], module["size"]

    def read(rva, count):
        if not 0 <= rva <= size - count or count <= 0:
            raise RuntimeError("Invalid native bridge export bounds")
        return memory.read(base + rva, count)

    if read(0, 2) != b"MZ":
        raise RuntimeError("Invalid native bridge DOS header")
    pe = struct.unpack("<I", read(0x3C, 4))[0]
    header = read(pe, 128)
    if (header[:4] != b"PE\0\0" or struct.unpack_from("<H", header, 4)[0] != 0x14C
            or struct.unpack_from("<H", header, 24)[0] != 0x10B):
        raise RuntimeError("Native bridge must be a PE32 image")
    exports, export_size = struct.unpack_from("<II", header, 120)
    if not exports or export_size < 40:
        raise RuntimeError("Native bridge exports are missing")
    directory = read(exports, 40)
    functions, names, addresses, name_table, ordinals = struct.unpack_from("<5I", directory, 20)
    if not 0 < names <= functions <= 4096:
        raise RuntimeError("Invalid native bridge export table")
    targets = struct.unpack(f"<{functions}I", read(addresses, functions * 4))
    strings = struct.unpack(f"<{names}I", read(name_table, names * 4))
    indexes = struct.unpack(f"<{names}H", read(ordinals, names * 2))
    accepted = (symbol.encode("ascii"), ("_" + symbol).encode("ascii"))
    for name, index in zip(strings, indexes):
        if read(name, min(64, size - name)).split(b"\0", 1)[0] not in accepted:
            continue
        if index >= functions:
            raise RuntimeError("Invalid native bridge export ordinal")
        target = targets[index]
        if not target or exports <= target < exports + export_size:
            raise RuntimeError("Native bridge data export is forwarded or missing")
        read(target, length)
        return base + target
    raise RuntimeError(f"Native bridge export {symbol} is missing")


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


class RuntimeChanges:
    """Reversible masked writes guarded by each game's allocation identities."""

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


class BasicLimits(c.Structure):
    _fields_ = [
        ("process_time", c.c_longlong), ("job_time", c.c_longlong),
        ("flags", w.DWORD), ("min_working_set", c.c_size_t),
        ("max_working_set", c.c_size_t), ("active_processes", w.DWORD),
        ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD),
    ]


class ExtendedLimits(c.Structure):
    _fields_ = [
        ("basic", BasicLimits), ("io_counters", c.c_ulonglong * 6),
        ("process_memory", c.c_size_t), ("job_memory", c.c_size_t),
        ("peak_process_memory", c.c_size_t), ("peak_job_memory", c.c_size_t),
    ]


class ProcessJob:
    """Windows closes only our child tree if the supervisor exits or fails."""

    def __init__(self):
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self.kernel.SetInformationJobObject.restype = w.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = w.BOOL
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.CloseHandle.restype = w.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)):
            error = c.WinError(c.get_last_error())
            self.close()
            raise error

    def add(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise c.WinError(c.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def watch(process, seconds, pressed, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + seconds if seconds is not None else None
    armed = False
    while process.poll() is None:
        down = pressed()
        if not down:
            armed = True
        reason = "f12" if armed and down else "timeout" if deadline is not None and clock() >= deadline else None
        if reason:
            process.kill()
            process.wait(timeout=5)
            return reason
        sleep(0.05)
    return "exited"


def supervise(command, directory, seconds, report, publish):
    user32 = c.WinDLL("user32", use_last_error=True)
    user32.GetAsyncKeyState.argtypes = [c.c_int]
    user32.GetAsyncKeyState.restype = c.c_short
    job = ProcessJob()  # Set up recovery before starting anything.
    process = None
    try:
        process = spawn(job, command, directory)
        report.update(status="running", pid=process.pid)
        publish(report)
        reason = watch(process, seconds, lambda: bool(user32.GetAsyncKeyState(0x7B) & 0x8000))
        report.update(status="stopped", reason=reason, returncode=process.returncode)
        publish(report)
        return report
    finally:
        stop_processes(job, [process] if process is not None else [])


def spawn(job, command, directory=ROOT):
    process = subprocess.Popen(command, cwd=directory)
    try:
        job.add(process)
    except BaseException:
        process.kill()
        process.wait(timeout=5)
        raise
    return process


def stop_processes(job, processes):
    """Close our job and reap every child, even if one child is slow to stop."""
    job.close()
    for process in processes:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
