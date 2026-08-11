"""Cross-platform process liveness, shared by the parent-death watchdogs.

Lives here rather than in app/main.py so the demucs worker can use it without
importing FastAPI and the whole application: the worker is spawned per device
and its startup time is on the critical path of every separation.
"""

from __future__ import annotations

import os


def process_exists(pid: int) -> bool:
    """Whether a process with this pid is alive.

    Errs on the side of "alive": a permission error means the process is there
    but owned by someone else, and a watchdog must not shoot on ambiguity.
    """
    if pid <= 0:
        return False

    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_INVALID_PARAMETER = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() != ERROR_INVALID_PARAMETER
