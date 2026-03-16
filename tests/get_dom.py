"""Get DOM snapshots from all failing sites."""
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
                print(f"\n{'='*60}\n=== INSPECT: {site} ===\n{'='*60}")
                r = await client.get(f"{PROXY_URL}/inspect/{site}", timeout=90)
                if r.status_code == 200:
                    data = r.json()
                    print(f"URL: {data.get('url')}")
                    print(f"Title: {data.get('title')}")
                    print(f"\nHeuristic suggestions: {data.get('suggestions')}")
                    dom = data.get("dom", {})
                    print(f"\nBUTTONS ({len(dom.get('buttons',[]))} found):")
                    for b in dom.get("buttons", [])[:15]:
                        print(f"  text={b.get('text')!r} type={b.get('type')} aria={b.get('ariaLabel')!r} testid={b.get('dataTestid')!r} classes={b.get('classes','')[:60]!r}")
                    print(f"\nINPUTS ({len(dom.get('inputs',[]))} found):")
                    for i in dom.get("inputs", [])[:10]:
                        print(f"  <{i.get('tag')} type={i.get('type')} placeholder={i.get('placeholder')!r} role={i.get('role')} aria={i.get('ariaLabel')!r} classes={i.get('classes','')[:60]!r}>")
                    print(f"\nCONTENTEDITABLE ({len(dom.get('contenteditable',[]))} found):")
                    for c in dom.get("contenteditable", [])[:5]:
                        print(f"  role={c.get('role')} aria={c.get('ariaLabel')!r} placeholder={c.get('placeholder')!r} classes={c.get('classes','')[:80]!r}")
                    print(f"\nDATA-ROLES ({len(dom.get('dataRoles',[]))} found):")
                    for d in dom.get("dataRoles", [])[:10]:
                        print(f"  <{d.get('tag')} data-role={d.get('dataRole')!r} data-message-role={d.get('dataMessageRole')!r} text={d.get('textPreview','')[:60]!r}>")
                    print(f"\nCHAT LINKS ({len(dom.get('chatLinks',[]))} found):")
                    for l in dom.get("chatLinks", [])[:5]:
                        print(f"  href={l.get('href')!r} text={l.get('text')!r}")
                else:
                    print("HTTP", r.status_code, r.text[:500])
    finally:
        proc.terminate(); proc.wait()

asyncio.run(main())
