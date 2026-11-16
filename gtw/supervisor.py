"""Own a launched process tree, with an independent F12/timeout recovery loop."""

import ctypes as c
import subprocess
import time
from ctypes import wintypes as w


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
        process = subprocess.Popen(command, cwd=directory)
        job.add(process)
        report.update(status="running", pid=process.pid)
        publish(report)
        reason = watch(process, seconds, lambda: bool(user32.GetAsyncKeyState(0x7B) & 0x8000))
        report.update(status="stopped", reason=reason, returncode=process.returncode)
        publish(report)
        return report
    finally:
        job.close()
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
