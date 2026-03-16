"""
Kill the ai-bridge proxy on session end.

Reads ~/.claude/ai-bridge/server.pid and kills that process.
Run via Claude Code SessionEnd hook — safe to call even if the proxy isn't running.
"""

import subprocess
import sys
from pathlib import Path

PID_FILE = Path.home() / ".claude" / "ai-bridge" / "server.pid"

if not PID_FILE.exists():
    sys.exit(0)

try:
    pid = int(PID_FILE.read_text().strip())
except Exception:
    PID_FILE.unlink(missing_ok=True)
    sys.exit(0)

if sys.platform == "win32":
    subprocess.call(
        ["powershell", "-Command", f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
else:
    subprocess.call(["kill", "-TERM", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

PID_FILE.unlink(missing_ok=True)
print(f"ai-bridge proxy (PID {pid}) stopped.")
