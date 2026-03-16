"""
CLI helper for querying the ai-bridge proxy.

Fixes the Windows bash curl quoting issue — JSON encoding is handled by Python,
so no shell quoting quirks.

Usage:
    uv run python proxy/send.py <site> <prompt> [--model MODEL] [--host HOST] [--port PORT]

Examples:
    uv run python proxy/send.py grok "Write a short report on Claude Code"
    uv run python proxy/send.py perplexity "What is 2+2?" --model sonar
    uv run python proxy/send.py chatgpt "Hello" --port 9090
"""

import argparse
import json
import sys
import urllib.error
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a prompt to the ai-bridge proxy")
    parser.add_argument("site", help="Site name (grok, perplexity, chatgpt, …)")
    parser.add_argument("prompt", help="Prompt to send")
    parser.add_argument("--model", default=None, help="Model alias (optional)")
    parser.add_argument("--host", default="127.0.0.1", help="Proxy host (default: 127.0.0.1)")
    parser.add_argument("--port", default=8080, type=int, help="Proxy port (default: 8080)")
    args = parser.parse_args()

    payload: dict = {"site": args.site, "prompt": args.prompt}
    if args.model:
        payload["model"] = args.model

    url = f"http://{args.host}:{args.port}/v1/proxy"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode())
            text = body.get("text") or body.get("response") or json.dumps(body, indent=2)
            sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()
        print(f"HTTP {exc.code}: {detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"Connection error: {exc.reason}", file=sys.stderr)
        print(
            f"Is the proxy running? Start it with:\n"
            f"  uv run python -m proxy.main",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
