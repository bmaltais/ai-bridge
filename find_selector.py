"""Find the most stable last_ai_msg selector from the current conversation."""
import json
import urllib.request

BASE = "http://127.0.0.1:8080"


def eval_js(js: str) -> object:
    payload = json.dumps({"js": js}).encode()
    req = urllib.request.Request(
        f"{BASE}/debug/eval/x-grok",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode()).get("result")


# Find the innermost element(s) containing ONLY the AI response text
print("=== Innermost response containers ===")
print(json.dumps(eval_js("""() => {
  const target = 'Hello AstroPepe';
  return Array.from(document.querySelectorAll('div, span, p'))
    .filter(el => {
      const t = (el.innerText||'').trim();
      return t.startsWith(target) && t.length < 100 && el.offsetParent !== null;
    })
    .map(el => ({
      tag: el.tagName,
      cls: el.className,
      parentCls: (el.parentElement?.className||'').substring(0,80),
      grandparentCls: (el.parentElement?.parentElement?.className||'').substring(0,60),
      text: (el.innerText||'').substring(0,80)
    }));
}"""), indent=2))

# Check if there are stable data-* attributes anywhere near the response
print("\n=== data-* attributes in conversation area ===")
print(json.dumps(eval_js("""() => {
  const attrs = new Set();
  document.querySelectorAll('*').forEach(el => {
    for (const a of el.attributes) {
      if (a.name.startsWith('data-') && a.name !== 'data-id') attrs.add(a.name + '=' + a.value.substring(0,30));
    }
  });
  return Array.from(attrs).slice(0, 40);
}"""), indent=2))

# Check what unique classes appear ONLY in the Grok response area
print("\n=== Count of r-3pj75a on page ===")
print(eval_js("""() => document.querySelectorAll('.r-3pj75a').length"""))

print("\n=== Count of r-bnwqim on page ===")
print(eval_js("""() => document.querySelectorAll('.r-bnwqim').length"""))

print("\n=== Count of r-11niif6 on page ===")
print(eval_js("""() => document.querySelectorAll('.r-11niif6').length"""))

# Try the /debug/find-text endpoint
print("\n=== /debug/find-text ===")
req = urllib.request.Request(f"{BASE}/debug/find-text/x-grok")
with urllib.request.urlopen(req, timeout=15) as r:
    print(json.dumps(json.loads(r.read().decode()), indent=2))
