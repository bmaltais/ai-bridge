"""Diagnose selector issues for failing sites."""
import asyncio, httpx, subprocess, sys
from pathlib import Path

PROXY_URL = "http://127.0.0.1:8080"
SITES = ["perplexity", "x-ai", "chatgpt"]

async def wait_for_health(client, attempts=30):
    for _ in range(attempts):
        try:
            r = await client.get(f"{PROXY_URL}/health", timeout=2)
            if r.status_code == 200: return True
        except Exception: pass
        await asyncio.sleep(1)
    return False

async def main():
    root = Path(__file__).parent.parent
    proc = subprocess.Popen([sys.executable, "-m", "proxy.main"], cwd=root,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        async with httpx.AsyncClient() as client:
            if not await wait_for_health(client):
                print("proxy did not start"); proc.terminate(); sys.exit(1)
            print("Proxy ready.\n")
            for site in SITES:
                print(f"=== {site} ===")
                r = await client.get(f"{PROXY_URL}/debug/selectors/{site}", timeout=90)
                if r.status_code == 200:
                    print(r.json().get("diagnosis", "")[:3000])
                else:
                    print("HTTP", r.status_code, r.text[:500])
                print()
    finally:
        proc.terminate(); proc.wait()

asyncio.run(main())
