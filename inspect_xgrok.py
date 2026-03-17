"""Inspect x.com/i/grok DOM to discover last_ai_msg selector."""
import json
import urllib.request

BASE = "http://127.0.0.1:8080"


def eval_js(js: str) -> str:
    payload = json.dumps({"js": js}).encode()
    req = urllib.request.Request(
        f"{BASE}/debug/eval/x-grok",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode())
            return json.dumps(body.get("result"), indent=2)
    except Exception as e:
        return f"ERROR: {e}"


# 1. data-testid elements
print("=== data-testid elements ===")
print(eval_js("""() => Array.from(document.querySelectorAll('[data-testid]'))
  .filter(el => el.offsetParent !== null)
  .map(el => ({tag: el.tagName, id: el.getAttribute('data-testid'), text: (el.innerText||'').substring(0,60)}))
  .slice(0, 40)
"""))

# 2. Message/response containers
print("\n=== message container candidates ===")
print(eval_js("""() =>
  ['[class*="message"]','[class*="response"]','[class*="answer"]','[class*="bubble"]',
   '[class*="turn"]','[class*="conversation"]','[class*="assistant"]','[class*="grok"]']
  .map(sel => {
    const els = document.querySelectorAll(sel);
    return {sel, count: els.length,
      sample: els.length > 0 ? (els[els.length-1].innerText||'').substring(0,60) : null};
  })
  .filter(x => x.count > 0)
"""))

# 3. role-based elements with text
print("\n=== role=article/region/main ===")
print(eval_js("""() =>
  Array.from(document.querySelectorAll('[role="article"],[role="region"],[role="main"],[role="presentation"]'))
  .filter(el => (el.innerText||'').trim().length > 20 && el.offsetParent !== null)
  .map(el => ({tag: el.tagName, role: el.getAttribute('role'),
    cls: el.className.substring(0,60), text: (el.innerText||'').substring(0,60)}))
  .slice(0, 20)
"""))

# 4. Check /inspect endpoint directly (built-in selector discovery)
print("\n=== /inspect endpoint ===")
req = urllib.request.Request(f"{BASE}/inspect/x-grok")
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        print(json.dumps(json.loads(r.read().decode()), indent=2))
except Exception as e:
    print(f"ERROR: {e}")
