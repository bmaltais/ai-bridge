"""
ai-bridge status monitor — lightweight tkinter GUI.

Shows bridge status, port, uptime, and provides a Stop button.
Launched alongside the bridge by send.py auto-start.
Auto-closes 3 seconds after the bridge stops.

Only one instance runs at a time (PID file guard).
"""

import os
import signal
import subprocess
import sys
import tkinter as tk
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

PID_FILE = Path.home() / ".claude" / "ai-bridge" / "server.pid"
TRAY_PID_FILE = Path.home() / ".claude" / "ai-bridge" / "tray.pid"

# Colors (Catppuccin Mocha-inspired)
BG = "#1e1e2e"
FG = "#cdd6f4"
FG_DIM = "#6c7086"
GREEN = "#a6e3a1"
RED = "#f38ba8"
SURFACE = "#313244"


class BridgeMonitor:
    def __init__(self, host: str = "127.0.0.1", port: int = 8080):
        self.host = host
        self.port = port
        self.start_time = datetime.now()
        self.request_count = 0

        self.root = tk.Tk()
        self.root.title("ai-bridge")
        self.root.configure(bg=BG)
        self.root.geometry("240x120")
        self.root.resizable(False, False)

        # Remove default icon, use a minimal titlebar
        try:
            self.root.iconbitmap(default="")
        except tk.TclError:
            pass

        # Position bottom-right of screen
        self.root.update_idletasks()
        x = self.root.winfo_screenwidth() - 260
        y = self.root.winfo_screenheight() - 180
        self.root.geometry(f"+{x}+{y}")

        # Start minimized to taskbar — visible but not in the way
        self.root.iconify()

        # Main frame
        frame = tk.Frame(self.root, bg=BG, padx=12, pady=8)
        frame.pack(fill="both", expand=True)

        # Header row: name + status dot
        header = tk.Frame(frame, bg=BG)
        header.pack(fill="x")
        tk.Label(
            header, text="ai-bridge", font=("Segoe UI", 11, "bold"), bg=BG, fg=FG
        ).pack(side="left")
        self.status_dot = tk.Label(
            header, text="\u25cf", font=("Segoe UI", 12), bg=BG, fg=GREEN
        )
        self.status_dot.pack(side="right")

        # Info row
        info = tk.Frame(frame, bg=BG)
        info.pack(fill="x", pady=(2, 0))
        tk.Label(info, text=f":{port}", font=("Consolas", 9), bg=BG, fg=FG_DIM).pack(
            side="left"
        )
        self.uptime_var = tk.StringVar(value="0s")
        tk.Label(
            info, textvariable=self.uptime_var, font=("Consolas", 9), bg=BG, fg=FG_DIM
        ).pack(side="right")

        # Stop button
        self.stop_btn = tk.Button(
            frame,
            text="Stop ai-bridge",
            command=self.stop_bridge,
            bg=SURFACE,
            fg=FG,
            activebackground=RED,
            activeforeground=BG,
            font=("Segoe UI", 9),
            relief="flat",
            padx=10,
            pady=2,
            cursor="hand2",
        )
        self.stop_btn.pack(side="bottom", fill="x", pady=(8, 0))

        self._poll()

    def _poll(self):
        healthy = False
        try:
            with urllib.request.urlopen(
                f"http://{self.host}:{self.port}/health", timeout=2
            ) as r:
                healthy = r.status == 200
        except Exception:
            pass

        if healthy:
            self.status_dot.configure(fg=GREEN)
            elapsed = datetime.now() - self.start_time
            self.uptime_var.set(self._fmt(elapsed))
        else:
            self.status_dot.configure(fg=RED)
            self.uptime_var.set("stopped")
            self.root.after(3000, self.root.destroy)
            return

        self.root.after(5000, self._poll)

    @staticmethod
    def _fmt(td: timedelta) -> str:
        s = int(td.total_seconds())
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m {s % 60}s"
        return f"{s // 3600}h {(s % 3600) // 60}m"

    def stop_bridge(self):
        pid = None
        try:
            pid = int(PID_FILE.read_text().strip())
        except Exception:
            pass
        if pid:
            if sys.platform == "win32":
                subprocess.call(
                    ["powershell", "-Command", f"Stop-Process -Id {pid} -Force"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                os.kill(pid, signal.SIGTERM)
        self.status_dot.configure(fg=RED)
        self.uptime_var.set("stopped")
        self.stop_btn.configure(state="disabled", text="Stopping...")
        self.root.after(1500, self.root.destroy)

    def run(self):
        self.root.mainloop()


def _pid_alive(pid: int) -> bool:
    """Check if a PID is still running."""
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return exit_code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_singleton() -> bool:
    """Ensure only one tray instance runs. Returns True if we acquired the lock."""
    try:
        old_pid = int(TRAY_PID_FILE.read_text().strip())
        if _pid_alive(old_pid):
            return False  # another tray is already running
    except (FileNotFoundError, ValueError):
        pass
    TRAY_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRAY_PID_FILE.write_text(str(os.getpid()))
    return True


def main():
    import argparse

    p = argparse.ArgumentParser(description="ai-bridge status monitor")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    args = p.parse_args()

    if not _acquire_singleton():
        return  # another tray is already running

    try:
        BridgeMonitor(host=args.host, port=args.port).run()
    finally:
        TRAY_PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
