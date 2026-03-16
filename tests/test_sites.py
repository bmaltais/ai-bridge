"""
End-to-end smoke test: start the proxy, send a minimal prompt to each site, report results.
Restarts the proxy between each site so only one browser session is active at a time.

Usage:
    uv run python tests/test_sites.py
    uv run python tests/test_sites.py perplexity x-ai   # run specific sites only
"""

import asyncio
import subprocess
import sys
import time
import threading
from pathlib import Path

import httpx

PROXY_URL = "http://127.0.0.1:8080"
SITES = ["use-ai", "perplexity", "x-ai", "chatgpt"]
PROMPT = "Reply with the word PONG and nothing else."
PER_SITE_TIMEOUT = 150  # > server-side response_timeout_s (120s)


def _stream_logs(proc, log_lines):
    for line in iter(proc.stdout.readline, b""):
        decoded = line.decode(errors="replace").rstrip()
        log_lines.append(decoded)


async def wait_for_health(client: httpx.AsyncClient, attempts: int = 30) -> bool:
    for _ in range(attempts):
        try:
            r = await client.get(f"{PROXY_URL}/health", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


async def test_site(client: httpx.AsyncClient, site: str) -> tuple[bool, str]:
    try:
        r = await client.post(
            f"{PROXY_URL}/v1/proxy",
            json={"site": site, "prompt": PROMPT},
            timeout=httpx.Timeout(PER_SITE_TIMEOUT, connect=5),
        )
        if r.status_code == 200:
            text = r.json().get("text", "") or ""
            text = text.strip()
            return (True, text[:120]) if text else (False, "empty response")
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except httpx.TimeoutException:
        return False, f"client timed out after {PER_SITE_TIMEOUT}s"
    except Exception as exc:
        return False, str(exc) or repr(exc)


async def run_one_site(site: str, root: Path) -> tuple[bool, str, float]:
    """Start proxy, test one site, stop proxy. Returns (ok, detail, elapsed)."""
    log_lines: list[str] = []
    proc = subprocess.Popen(
        [sys.executable, "-m", "proxy.main"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    log_thread = threading.Thread(target=_stream_logs, args=(proc, log_lines), daemon=True)
    log_thread.start()

    ok, detail, elapsed = False, "proxy did not start", 0.0
    try:
        async with httpx.AsyncClient() as client:
            if not await wait_for_health(client):
                return False, "proxy did not start", 0.0

            t0 = time.monotonic()
            ok, detail = await test_site(client, site)
            elapsed = time.monotonic() - t0
    finally:
        proc.terminate()
        proc.wait()

    if not ok:
        await asyncio.sleep(0.2)
        site_logs = [ln for ln in log_lines if site in ln.lower() or "ERROR" in ln or "WARNING" in ln]
        if site_logs:
            print(f"\n    [relevant proxy logs for {site}]")
            for ln in site_logs[-25:]:
                print(f"    {ln}")

    return ok, detail, elapsed


async def main():
    target_sites = sys.argv[1:] or SITES
    root = Path(__file__).parent.parent
    results = {}

    for site in target_sites:
        print(f"  Testing {site}...", end=" ", flush=True)
        ok, detail, elapsed = await run_one_site(site, root)
        status = "PASS" if ok else "FAIL"
        print(f"{status} ({elapsed:.1f}s)  {detail}")
        results[site] = ok
        if not ok:
            print()
        # Brief pause between sites so OS can release the port
        await asyncio.sleep(2)

    print()
    passed = sum(results.values())
    total = len(results)
    print(f"Results: {passed}/{total} sites passed")
    if passed < total:
        failed = [s for s, ok in results.items() if not ok]
        print(f"Failed: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
