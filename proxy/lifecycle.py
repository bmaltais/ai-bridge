"""
Process lifecycle management for the ai-bridge proxy.

Handles PID file tracking, port conflict detection, process termination,
and the watchdog thread that auto-exits when a parent process dies.
"""

import _thread
import logging
import os
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

PID_FILE = Path.home() / ".claude" / "ai-bridge" / "server.pid"


# ---------------------------------------------------------------------------
# Port / health helpers
# ---------------------------------------------------------------------------


def port_in_use(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


def is_healthy(host: str, port: int) -> bool:
    try:
        import urllib.request

        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------


def read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None


def write_pid() -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def pid_alive(pid: int) -> bool:
    """Return True if the given PID is still running."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        return exit_code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True  # process exists, we just lack permission to signal it
    except OSError:
        return False


def kill_pid(pid: int) -> None:
    import subprocess

    if sys.platform == "win32":
        subprocess.call(
            ["powershell", "-Command", f"Stop-Process -Id {pid} -Force"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        subprocess.call(
            ["kill", "-9", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


# ---------------------------------------------------------------------------
# Watchdog thread
# ---------------------------------------------------------------------------


def start_watchdog(watchdog_pid: int) -> None:
    """Start a daemon thread that exits the proxy when watchdog_pid dies."""

    def _watch():
        log.info(
            "Watchdog monitoring PID %d — proxy will exit when that process ends.",
            watchdog_pid,
        )
        while True:
            time.sleep(10)
            if not pid_alive(watchdog_pid):
                log.info("Watchdog PID %d is gone — shutting down proxy.", watchdog_pid)
                PID_FILE.unlink(missing_ok=True)
                _thread.interrupt_main()
                return

    t = threading.Thread(target=_watch, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Startup guard: detect and resolve port conflicts
# ---------------------------------------------------------------------------


def resolve_port_conflict(host: str, port: int) -> None:
    """Check for port conflicts and resolve them.

    If the port is occupied by a healthy ai-bridge, exits 0 (reuse it).
    If the port is occupied by a frozen ai-bridge, kills it.
    If the port is occupied by something else, logs error and exits 1.
    Raises SystemExit in all conflict cases; returns normally when port is free.
    """
    if not port_in_use(host, port):
        return  # port is free, nothing to do

    saved_pid = read_pid()
    if is_healthy(host, port):
        log.info(
            "ai-bridge already running and healthy at http://%s:%d — reusing it.",
            host,
            port,
        )
        sys.exit(0)

    # Port occupied but not healthy — find the owning process.
    current_pid: int | None = None
    if sys.platform == "win32":
        try:
            import subprocess

            current_pid = int(
                subprocess.check_output(
                    [
                        "powershell",
                        "-Command",
                        f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
                        f"-ErrorAction SilentlyContinue).OwningProcess",
                    ],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
            )
        except Exception:
            current_pid = None

    if saved_pid and current_pid and saved_pid == current_pid:
        log.warning(
            "Port %d has a frozen ai-bridge process (PID %d) — killing it.",
            port,
            saved_pid,
        )
        kill_pid(saved_pid)
        time.sleep(1)
    else:
        log.error(
            "Port %d is occupied by another application (PID %s). "
            "Change PORT in .env to avoid conflicts.",
            port,
            current_pid or "unknown",
        )
        sys.exit(1)
