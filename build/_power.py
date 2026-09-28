"""
_power.py — Keep Windows from throttling the Studio's helper processes.

WHY
───
Windows gives a process a quality-of-service level from the state of the
window it belongs to, and hands the same level to that process's
descendants: High while the window is focused, Medium while it is
visible but not focused, Low while it is minimized or fully covered. On
battery, Low also confines the work to the CPU's efficiency cores
(Microsoft Learn, "Quality of Service"). The Studio window is usually
not the focused one, because you are typing in your editor, and may be
covered by it; every preview then runs in the Node server, the Python
worker, Chromium and Sass at that level. Confinement to efficiency cores
measured 2–3× slower here.

WHAT THIS DOES
──────────────
It opts each of those processes out of execution-speed throttling:
SetProcessInformation(ProcessPowerThrottling) with ControlMask =
PROCESS_POWER_THROTTLING_EXECUTION_SPEED and StateMask = 0, which asks
the system never to throttle that process for power. It is applied to a
root process (the engine passes the Node server's pid) and every process
below it, found by walking the process table, because Chromium starts
its renderers itself and Sass runs its own compiler process.

What it deliberately does not do: raise or lower priority classes (a
lower priority can itself pull a process's QoS down), or set affinity
masks. Both would fight the scheduler instead of informing it.

UNVERIFIED
──────────
Written against the documented API and tested here only for its
non-Windows no-op and its process-tree logic. Whether it changes
preview times on your machine, on battery, with the window covered or
minimized, has not been measured. Chromium may also set power
throttling on its own renderers; if it does, it can override this.

Windows 10 1709 or later has the API; on anything else, or off Windows,
every call reports what it could not do rather than raising.
"""

import sys

# processthreadsapi.h
PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
PROCESS_INFORMATION_CLASS_POWER_THROTTLING = 4   # ProcessPowerThrottling
PROCESS_SET_INFORMATION = 0x0200
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = -1


def descendants(parents, root):
    """`root` and every process below it, from {pid: parent_pid}.

    Breadth-first, each pid once, so a cycle in stale parent ids (a
    parent pid reused after its process exited) cannot loop.
    """
    children = {}
    for pid, parent in parents.items():
        if pid != parent:
            children.setdefault(parent, []).append(pid)
    out, seen, queue = [], {root}, [root]
    while queue:
        pid = queue.pop(0)
        out.append(pid)
        for child in sorted(children.get(pid, ())):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return out


def _process_parents():
    """{pid: parent_pid} for every process, from a Toolhelp snapshot."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wintypes.HANDLE(INVALID_HANDLE_VALUE).value:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        parents = {}
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            parents[entry.th32ProcessID] = entry.th32ParentProcessID
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        return parents
    finally:
        kernel32.CloseHandle(snap)


def _opt_out(pids):
    """Apply the opt-out to each pid. Returns (applied, failed)."""
    import ctypes
    from ctypes import wintypes

    class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
        _fields_ = [
            ("Version", wintypes.ULONG),
            ("ControlMask", wintypes.ULONG),
            ("StateMask", wintypes.ULONG),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.SetProcessInformation.restype = wintypes.BOOL
    kernel32.SetProcessInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    state = PROCESS_POWER_THROTTLING_STATE(
        PROCESS_POWER_THROTTLING_CURRENT_VERSION,
        PROCESS_POWER_THROTTLING_EXECUTION_SPEED,   # the policy being set…
        0,                                          # …to "never throttle"
    )
    applied, failed = [], []
    for pid in pids:
        handle = kernel32.OpenProcess(PROCESS_SET_INFORMATION, False, pid)
        if not handle:
            failed.append({"pid": pid, "error": ctypes.get_last_error()})
            continue
        try:
            ok = kernel32.SetProcessInformation(
                handle, PROCESS_INFORMATION_CLASS_POWER_THROTTLING,
                ctypes.byref(state), ctypes.sizeof(state))
            if ok:
                applied.append(pid)
            else:
                failed.append({"pid": pid, "error": ctypes.get_last_error()})
        finally:
            kernel32.CloseHandle(handle)
    return applied, failed


def opt_out_tree(root):
    """Opt `root` and its descendants out of power throttling.

    Returns {"supported", "applied": [pids], "failed": [{pid, error}]};
    never raises. Off Windows, supported is False and nothing is done.
    """
    if sys.platform != "win32":
        return {"supported": False, "applied": [], "failed": [],
                "reason": f"not Windows ({sys.platform})"}
    try:
        pids = descendants(_process_parents(), int(root))
        applied, failed = _opt_out(pids)
        return {"supported": True, "applied": applied, "failed": failed}
    except (OSError, AttributeError, ValueError) as err:
        # AttributeError: a kernel32 without SetProcessInformation (older
        # than Windows 8) — nothing to opt out of.
        return {"supported": False, "applied": [], "failed": [], "reason": str(err)}
