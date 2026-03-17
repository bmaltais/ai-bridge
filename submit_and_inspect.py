"""Submit the pending message and poll DOM to discover last_ai_msg selector."""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8080"


def eval_js(js: str, timeout: int = 15) -> object:
    payload = json.dumps({"js": js}).encode()
    req = urllib.request.Request(
        f"{BASE}/debug/eval/x-grok",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode())
            return body.get("result")
    except Exception as e:
        return f"ERROR: {e}"


print("Step 1: click Grok submit button...")
result = eval_js("""() => {
  const btn = document.querySelector('button[aria-label="Grok something"]');
  if (!btn) return 'NOT FOUND';
  btn.click();
  return 'clicked';
}""")
print(" ->", result)

print("Step 2: poll for response elements (30s max)...")
candidates = [
    '[data-testid*="grok"]',
    '[data-testid*="message"]',
    '[data-testid*="response"]',
    '[class*="grok"]',
    '[class*="message"]',
    '[class*="response"]',
    '[class*="answer"]',
    '[class*="assistant"]',
    'article',
    '[role="article"]',
]

for i in range(30):
    time.sleep(1)
    found = eval_js("""() => {
  const sels = [
    '[data-testid*="grok"]','[data-testid*="message"]','[data-testid*="response"]',
    '[class*="messageContent"]','[class*="assistant"]','[class*="grokMsg"]',
    'article','[role="article"]'
  ];
  return sels.map(sel => {
    const els = document.querySelectorAll(sel);
    return {sel, count: els.length,
      text: els.length > 0 ? (els[els.length-1].innerText||'').substring(0,80) : null};
  }).filter(x => x.count > 0);
}""")
    print(f"  [{i+1}s] {found}")
    if found and isinstance(found, list) and len(found) > 0:
        texts = [x.get("text","") for x in found if x.get("text")]
        if any(len(t) > 10 for t in texts):
            print("\nResponse detected! Breaking...")
            break

print("\nStep 3: broader DOM snapshot after response...")
snapshot = eval_js("""() => {
  // Get all elements with substantial text (likely response containers)
  return Array.from(document.querySelectorAll('*'))
    .filter(el => {
      if (!el.offsetParent) return false;
      const t = (el.innerText||'').trim();
      return t.length > 30 && t.length < 400 && el.children.length <= 2;
    })
    .map(el => {
      const cls = el.className || '';
      const testid = el.getAttribute('data-testid') || '';
      return {
        tag: el.tagName,
        testid,
        cls: cls.substring(0,80),
        text: (el.innerText||'').substring(0,80)
      };
    })
    .slice(0, 30);
}""")
print(json.dumps(snapshot, indent=2))

print("\nStep 4: page URL after submit...")
print(eval_js("() => window.location.href"))
