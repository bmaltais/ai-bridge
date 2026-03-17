#!/usr/bin/env python3
"""
Ensure ai-bridge is running. If already up, return immediately.
If not, start a new instance and wait for it to become ready.

Usage (standalone):
    uv run python proxy/ensure.py [--port 8080] [--timeout 30]

Usage (as a library):
    from proxy.ensure import ensure_running
    ok = ensure_running(port=8080, timeout=30)   # True = bridge is up

Exit codes: 0 = bridge is up, 1 = failed to start within timeout.

Why this exists:
    Callers (dialogue.py, send.py, scripts) should never need to check health,
    start the bridge manually, or wait for it. This module is the single entry
    point: call ensure_running() before any /v1/proxy request and the bridge
    will be up, whether it was already running or just started.
"""

import argparse
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

_BRIDGE_ROOT = Path(__file__).parent.parent  # ai-bridge skill dir
_LOG_DIR = Path.home() / ".claude" / "ai-bridge"
_LOG_FILE = _LOG_DIR / "proxy.log"
_DEFAULT_PORT = 8080
_POLL_INTERVAL_S = 0.5  # seconds between health checks while waiting for startup
_START_TIMEOUT_S = 30  # seconds to wait for bridge to start before giving up


def _health_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/health"


def _is_running(port: int) -> bool:
    """Return True if bridge responds to /health on the given port."""
    try:
        with urllib.request.urlopen(_health_url(port), timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def _start_bridge(port: int) -> subprocess.Popen:
    """Spawn the bridge server as a detached background process."""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_f = open(_LOG_FILE, "a")
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["WATCHDOG_PID"] = "1"  # PID 1 disables watchdog → persistent bridge

    # DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP: survive parent exit on Windows.
    # Falls back to close_fds=True on non-Windows.
    popen_kwargs: dict = {
        "cwd": str(_BRIDGE_ROOT),
        "stdout": log_f,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "env": env,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        popen_kwargs["start_new_session"] = True

    return subprocess.Popen(
        ["uv", "run", "python", "-m", "proxy.main"],
        **popen_kwargs,
    )


def ensure_running(
    port: int = _DEFAULT_PORT,
    timeout: int = _START_TIMEOUT_S,
) -> bool:
    """
    Ensure the bridge is up on `port`.

    Returns True immediately if already running.
    If not running, starts it and polls until ready or `timeout` seconds elapse.
    Returns False only if the bridge could not start within `timeout`.
    """
    if _is_running(port):
        return True

    print(f"ai-bridge not running on port {port} — starting...", file=sys.stderr)
    _start_bridge(port)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL_S)
        if _is_running(port):
            elapsed = timeout - (deadline - time.monotonic())
            print(
                f"ai-bridge ready on port {port} (started in {elapsed:.1f}s)",
                file=sys.stderr,
            )
            return True

    print(
        f"ai-bridge failed to start within {timeout}s — check {_LOG_FILE}",
        file=sys.stderr,
    )
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description="Ensure ai-bridge is running")
    ap.add_argument(
        "--port", type=int, default=_DEFAULT_PORT, help=f"Port (default: {_DEFAULT_PORT})"
    )
    ap.add_argument(
        "--timeout",
        type=int,
        default=_START_TIMEOUT_S,
        help=f"Seconds to wait for startup (default: {_START_TIMEOUT_S})",
    )
    args = ap.parse_args()

    ok = ensure_running(port=args.port, timeout=args.timeout)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
