"""
CLI helper for querying the ai-bridge proxy.

Fixes the Windows bash curl quoting issue — JSON encoding is handled by Python,
so no shell quoting quirks.

Usage:
    uv run python proxy/send.py <site> <prompt> [--model MODEL] [--chat-url URL] [--host HOST] [--port PORT]

Examples:
    uv run python proxy/send.py grok "Write a short report on Claude Code"
    uv run python proxy/send.py perplexity "What is 2+2?" --model sonar
    uv run python proxy/send.py grok "Continue our discussion" --chat-url https://grok.com/c/abc123
    uv run python proxy/send.py chatgpt "Hello" --port 9090
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_SKILL_ROOT = Path(__file__).parent.parent


def _is_healthy(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _start_bridge(host: str, port: int) -> bool:
    """Start the bridge as a detached background process; wait up to 20s."""
    print("ai-bridge not running — starting it...", file=sys.stderr)
    log_dir = Path.home() / ".claude" / "ai-bridge"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_f = open(log_dir / "proxy.log", "a")
    env = {**os.environ, "WATCHDOG_PID": "1"}  # PID 1 disables watchdog → persistent bridge
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    subprocess.Popen(
        ["uv", "run", "python", "-m", "proxy.main"],
        cwd=str(_SKILL_ROOT),
        stdout=log_f,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        creationflags=flags,
    )
    deadline = time.time() + 20
    interval = 0.5
    while time.time() < deadline:
        if _is_healthy(host, port):
            print(f"ai-bridge ready at http://{host}:{port}", file=sys.stderr)
            return True
        time.sleep(interval)
        interval = min(interval * 1.3, 2.0)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a prompt to the ai-bridge proxy")
    parser.add_argument("site", help="Site name (grok, perplexity, chatgpt, …)")
    parser.add_argument("prompt", help="Prompt to send")
    parser.add_argument("--model", default=None, help="Model alias (optional)")
    parser.add_argument("--chat-url", default=None, dest="chat_url",
                        help="Resume an existing chat by URL (e.g. https://grok.com/c/<uuid>)")
    parser.add_argument("--host", default="127.0.0.1", help="Proxy host (default: 127.0.0.1)")
    parser.add_argument("--port", default=8080, type=int, help="Proxy port (default: 8080)")
    args = parser.parse_args()

    payload: dict = {"site": args.site, "prompt": args.prompt}
    if args.model:
        payload["model"] = args.model
    if args.chat_url:
        payload["chat_url"] = args.chat_url

    if not _is_healthy(args.host, args.port):
        if not _start_bridge(args.host, args.port):
            print(
                f"Failed to start ai-bridge on {args.host}:{args.port}.\n"
                f"Try manually: cd {_SKILL_ROOT} && uv run python -m proxy.main",
                file=sys.stderr,
            )
            sys.exit(1)

    url = f"http://{args.host}:{args.port}/v1/proxy"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode())
            text = body.get("text") or body.get("response") or json.dumps(body, indent=2)
            sys.stdout.buffer.write((text + "\n").encode("utf-8"))
            if body.get("chat_url"):
                print(f"[chat_url] {body['chat_url']}", file=sys.stderr)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()
        print(f"HTTP {exc.code}: {detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"Connection error: {exc.reason}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
