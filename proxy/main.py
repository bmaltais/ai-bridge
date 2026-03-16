"""
FastAPI entrypoint for the use.ai proxy.

Exposes:
  POST /v1/messages         — Anthropic Messages API (Claude Code compatible)
  GET  /v1/models           — model listing (Claude Code checks this)
  GET  /health              — liveness check
  POST /v1/proxy            — generic web LLM proxy (any configured site)
  GET  /inspect/{site}      — auto-discover selectors via DOM analysis + LLM
  GET  /debug/selectors     — diagnose selectors on the main browser session
  GET  /debug/selectors/{site} — diagnose selectors on a specific site
  GET  /debug/html/{site}   — dump candidate response elements for a site
"""

import logging
import io
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from proxy import scraper, streaming, translator
from proxy.browser import browser_session
from proxy.config import settings
from proxy.models import MessagesRequest, ModelObject, ModelsResponse
from proxy.scraper import SiteSelectors
from proxy.site_session import SiteSessionManager

_SITES_DIR = Path(__file__).parent / "sites"
site_manager = SiteSessionManager(_SITES_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan: start/stop browser with the server
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Proxy ready on http://%s:%d", settings.host, settings.port)
    log.info("Browser sessions are initialized lazily on first request.")
    yield
    log.info("Shutting down browser sessions...")
    await browser_session.close()
    await site_manager.close_all()


app = FastAPI(title="use.ai proxy", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok", "browser_ready": browser_session.is_ready}


async def _run_diagnosis(page, sel: SiteSelectors | None = None) -> str:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    scraper_log = logging.getLogger("proxy.scraper")
    scraper_log.addHandler(handler)
    try:
        await scraper.diagnose_selectors(page, sel or SiteSelectors())
    finally:
        scraper_log.removeHandler(handler)
    return buf.getvalue()


@app.get("/debug/selectors")
async def debug_selectors():
    """Run selector diagnosis on the main browser session page."""
    if not browser_session.is_ready:
        raise HTTPException(status_code=503, detail="Browser session not ready")
    diagnosis = await _run_diagnosis(browser_session.page)
    return {"diagnosis": diagnosis}


@app.get("/debug/selectors/{site}")
async def debug_selectors_site(site: str):
    """Run selector diagnosis on a specific site's browser page."""
    try:
        site_session = await site_manager.get(site)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if not site_session.is_ready:
        raise HTTPException(status_code=503, detail=f"Session for {site!r} not ready")
    sel = SiteSelectors.from_config(site_session.config)
    diagnosis = await _run_diagnosis(site_session.page, sel)
    return {"site": site, "diagnosis": diagnosis}


@app.get("/debug/html/{site}")
async def debug_html_site(site: str):
    """Dump the inner HTML of candidate response elements for selector debugging."""
    try:
        site_session = await site_manager.get(site)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if not site_session.is_ready:
        raise HTTPException(status_code=503, detail=f"Session for {site!r} not ready")

    page = site_session.page
    candidates = await page.evaluate("""() => {
        const selectors = [
            '[data-role="assistant"]',
            '[data-message-role="assistant"]',
            '.assistant-message',
            '[class*="assistant"]',
            '[class*="message"]',
            '[class*="response"]',
            '[class*="bubble"]',
            '[class*="chat"]',
        ];
        const results = {};
        for (const sel of selectors) {
            const els = document.querySelectorAll(sel);
            if (els.length > 0) {
                const last = els[els.length - 1];
                results[sel] = {
                    count: els.length,
                    lastText: last.innerText?.slice(0, 300),
                    lastClasses: last.className,
                    tagName: last.tagName,
                };
            }
        }
        return results;
    }""")
    return {"site": site, "url": page.url, "candidates": candidates}


class EvalBody(BaseModel):
    js: str


@app.post("/debug/eval/{site}")
async def debug_eval(site: str, body: EvalBody):
    """Run arbitrary JS on a site's browser page and return the result.

    Call: curl -X POST http://127.0.0.1:8080/debug/eval/perplexity \\
            -H 'Content-Type: application/json' \\
            -d '{"js": "() => document.title"}'
    """
    try:
        site_session = await site_manager.get(site)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    result = await site_session.page.evaluate(body.js)
    return {"site": site, "result": result}


@app.post("/debug/dismiss-modal/{site}")
async def debug_dismiss_modal(site: str):
    """Press Escape and try common close-button selectors to dismiss overlays/modals.

    Call: curl -X POST http://127.0.0.1:8080/debug/dismiss-modal/perplexity
    """
    try:
        site_session = await site_manager.get(site)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    page = site_session.page
    await page.keyboard.press("Escape")
    # Try common close/dismiss button selectors
    for sel in [
        'button[aria-label*="close" i]',
        'button[aria-label*="dismiss" i]',
        '[data-testid*="close" i]',
        'button[class*="close" i]',
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                await loc.click()
                break
        except Exception:
            pass
    return {"status": "dismissed", "url": page.url}


@app.get("/session/status/{site}")
async def session_status(site: str):
    """Return whether a site session is waiting for login."""
    # Resolve alias → canonical name so "grok" finds the "x-ai" session
    try:
        from proxy.site_config import SiteConfig
        key = SiteConfig.find(site, _SITES_DIR).name
    except Exception:
        key = site
    if key in site_manager._sessions:
        sess = site_manager._sessions[key].session
        return {"site": site, "canonical": key, "login_pending": sess.login_pending, "ready": sess.is_ready}
    # Session not yet initialized — it will need login on first use
    return {"site": site, "login_pending": False, "ready": False, "initialized": False}


@app.post("/session/notify-login/{site}")
async def session_notify_login(site: str):
    """Signal that the user has completed login for a site session.

    Call this from run.py (or curl) after the user has logged in to the site
    in the browser window. The server will save cookies and mark the session ready.

    Call: curl -X POST http://127.0.0.1:8080/session/notify-login/x-ai
    """
    # Resolve alias → canonical name
    try:
        from proxy.site_config import SiteConfig
        key = SiteConfig.find(site, _SITES_DIR).name
    except Exception:
        key = site
    if key in site_manager._sessions:
        await site_manager._sessions[key].session.notify_login()
        return {"status": "notified", "site": site, "canonical": key}
    # Also handle the main browser session
    if key in ("use-ai", "main"):
        await browser_session.notify_login()
        return {"status": "notified", "site": site}
    raise HTTPException(status_code=404, detail=f"No active session for site {site!r}")


@app.post("/debug/save-cookies/{site}")
async def debug_save_cookies(site: str):
    """Persist the current browser session cookies to disk.

    Call: curl -X POST http://127.0.0.1:8080/debug/save-cookies/perplexity
    """
    try:
        site_session = await site_manager.get(site)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    await site_session.session.save_cookies()
    return {"status": "saved", "path": str(site_session.session._cookies_path)}


@app.get("/debug/find-text/{site}")
async def debug_find_text(
    site: str,
    min_len: int = Query(80, description="Minimum text length to include"),
):
    """Find all elements with substantial text — useful for discovering last_ai_msg selector.

    Call: curl "http://127.0.0.1:8080/debug/find-text/perplexity"
    """
    try:
        site_session = await site_manager.get(site)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if not site_session.is_ready:
        raise HTTPException(status_code=503, detail=f"Session for {site!r} not ready")

    page = site_session.page
    results = await page.evaluate(f"""() => {{
        const minLen = {min_len};
        const seen = new Set();
        const found = [];
        document.querySelectorAll('div, p, section, article, span').forEach(el => {{
            const text = (el.innerText || '').trim();
            if (text.length < minLen) return;
            // Skip containers whose children already have more specific matches
            const childrenWithText = Array.from(el.children).filter(c => (c.innerText || '').trim().length >= minLen);
            if (childrenWithText.length > 0) return;
            const key = text.slice(0, 40);
            if (seen.has(key)) return;
            seen.add(key);
            const dataAttrs = {{}};
            for (const attr of el.attributes) {{
                if (attr.name.startsWith('data-') || attr.name === 'role' || attr.name === 'aria-label') {{
                    dataAttrs[attr.name] = attr.value;
                }}
            }}
            found.push({{
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                classes: el.className?.slice(0, 120) || null,
                dataAttrs,
                textPreview: text.slice(0, 120),
            }});
        }});
        return found.slice(0, 30);
    }}""")
    return {"site": site, "url": page.url, "elements": results}


# ---------------------------------------------------------------------------
# Auto-discovery: inspect a site's DOM and suggest selectors
# ---------------------------------------------------------------------------

# JS that extracts a rich snapshot of all interactive elements from the page.
_INSPECT_JS = """() => {
    const truncate = (s, n) => (s || '').trim().slice(0, n);

    const buttons = Array.from(document.querySelectorAll('button')).map(el => ({
        text: truncate(el.innerText, 80),
        type: el.type || null,
        ariaLabel: el.getAttribute('aria-label'),
        dataTestid: el.getAttribute('data-testid'),
        classes: truncate(el.className, 120),
        disabled: el.disabled,
    }));

    const inputs = Array.from(document.querySelectorAll('input, textarea')).map(el => ({
        tag: el.tagName.toLowerCase(),
        type: el.type || null,
        placeholder: truncate(el.placeholder, 80),
        ariaLabel: el.getAttribute('aria-label'),
        name: el.name || null,
        id: el.id || null,
        role: el.getAttribute('role'),
        classes: truncate(el.className, 120),
    }));

    const contenteditable = Array.from(document.querySelectorAll('[contenteditable]')).map(el => ({
        role: el.getAttribute('role'),
        ariaLabel: el.getAttribute('aria-label'),
        placeholder: el.getAttribute('placeholder') || el.getAttribute('data-placeholder'),
        classes: truncate(el.className, 120),
    }));

    const chatLinks = Array.from(document.querySelectorAll('a[href]'))
        .filter(el => {
            const h = el.getAttribute('href') || '';
            return /chat|new|history|convers|thread/i.test(h);
        })
        .map(el => ({
            text: truncate(el.innerText, 80),
            href: truncate(el.getAttribute('href'), 120),
            ariaLabel: el.getAttribute('aria-label'),
        }));

    const selects = Array.from(document.querySelectorAll('select')).map(el => ({
        name: el.name || null,
        id: el.id || null,
        ariaLabel: el.getAttribute('aria-label'),
        options: Array.from(el.options).map(o => truncate(o.text, 40)).slice(0, 15),
    }));

    const dataRoles = Array.from(
        document.querySelectorAll('[data-role], [data-message-role]')
    ).map(el => ({
        tag: el.tagName.toLowerCase(),
        dataRole: el.getAttribute('data-role'),
        dataMessageRole: el.getAttribute('data-message-role'),
        classes: truncate(el.className, 80),
        textPreview: truncate(el.innerText, 100),
    }));

    // Model selector candidates: buttons/divs that look like model pickers
    const modelCandidates = Array.from(
        document.querySelectorAll('[aria-label*="model" i], [data-testid*="model" i], ' +
            'button[class*="model" i], [class*="model-select" i]')
    ).map(el => ({
        tag: el.tagName.toLowerCase(),
        text: truncate(el.innerText, 80),
        ariaLabel: el.getAttribute('aria-label'),
        classes: truncate(el.className, 120),
    }));

    return { buttons, inputs, contenteditable, chatLinks, selects, dataRoles, modelCandidates };
}"""


def _heuristic_selectors(dom: dict) -> dict:
    """
    Apply heuristic rules to the DOM snapshot to suggest CSS selectors.
    Returns a dict with keys matching SiteConfig fields.
    """
    suggestions: dict[str, str | None] = {
        "chat_input": None,
        "submit_button": None,
        "last_ai_msg": None,
        "thinking_spinner": None,
        "new_chat": None,
        "model_selector": None,
    }

    # chat_input: prefer role=textbox, then textarea with relevant placeholder
    for el in dom.get("contenteditable", []):
        if el.get("role") == "textbox":
            suggestions["chat_input"] = '[role="textbox"]'
            break
    if not suggestions["chat_input"]:
        for el in dom.get("inputs", []):
            if el.get("tag") == "textarea":
                ph = (el.get("placeholder") or "").lower()
                if any(w in ph for w in ("message", "ask", "type", "prompt")):
                    placeholder = el.get("placeholder", "")
                    suggestions["chat_input"] = (
                        f'textarea[placeholder*="{placeholder[:30]}" i]'
                    )
                    break

    # submit_button: button[type=submit] first, then aria-label "send"
    for el in dom.get("buttons", []):
        if el.get("type") == "submit":
            suggestions["submit_button"] = 'button[type="submit"]'
            break
    if not suggestions["submit_button"]:
        for el in dom.get("buttons", []):
            label = (el.get("ariaLabel") or el.get("text") or "").lower()
            if "send" in label or "submit" in label:
                aria = el.get("ariaLabel")
                if aria:
                    suggestions["submit_button"] = (
                        f'button[aria-label*="{aria[:30]}" i]'
                    )
                    break

    # last_ai_msg: prefer data-role="assistant"
    for el in dom.get("dataRoles", []):
        if el.get("dataRole") == "assistant":
            suggestions["last_ai_msg"] = '[data-role="assistant"]'
            break
    if not suggestions["last_ai_msg"]:
        for el in dom.get("dataRoles", []):
            if el.get("dataMessageRole") == "assistant":
                suggestions["last_ai_msg"] = '[data-message-role="assistant"]'
                break

    # new_chat: links/buttons with "new" in href or text
    for el in dom.get("chatLinks", []):
        href = el.get("href") or ""
        if "new" in href.lower():
            suggestions["new_chat"] = f'a[href*="{href[:40]}" i]'
            break
    if not suggestions["new_chat"]:
        for el in dom.get("buttons", []):
            text = (el.get("text") or el.get("ariaLabel") or "").lower()
            if "new" in text and "chat" in text:
                aria = el.get("ariaLabel")
                if aria:
                    suggestions["new_chat"] = f'button[aria-label*="{aria[:30]}" i]'
                    break

    # model_selector
    if dom.get("modelCandidates"):
        el = dom["modelCandidates"][0]
        aria = el.get("ariaLabel")
        if aria:
            suggestions["model_selector"] = f'[aria-label*="{aria[:40]}" i]'

    return {k: v for k, v in suggestions.items() if v}


@app.get("/inspect/{site}")
async def inspect_site(
    site: str,
    write: bool = Query(
        False, description="Write suggested selectors back to the YAML config"
    ),
    use_llm: bool = Query(
        True, description="Use Claude to improve selector suggestions"
    ),
):
    """
    Inspect a site's DOM and auto-discover CSS selectors for chat interaction.

    Returns heuristic + (optionally) LLM-enhanced selector suggestions.
    Pass ?write=true to save the suggestions directly into the site's YAML config.
    Pass ?use_llm=false to skip the LLM call and return heuristics only.

    Call: curl http://127.0.0.1:8080/inspect/use-ai
    """
    try:
        site_session = await site_manager.get(site)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if not site_session.is_ready:
        raise HTTPException(status_code=503, detail=f"Session for {site!r} not ready")

    page = site_session.page
    url = page.url
    title = await page.title()

    # Collect DOM snapshot
    dom = await page.evaluate(_INSPECT_JS)

    # Heuristic suggestions
    heuristic = _heuristic_selectors(dom)

    llm_suggestions: dict | None = None
    llm_error: str | None = None

    if use_llm:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            llm_error = "ANTHROPIC_API_KEY not set"
        else:
            try:
                import anthropic

                # Always call the real Anthropic API, never the local proxy
                client = anthropic.AsyncAnthropic(
                    api_key=api_key,
                    base_url="https://api.anthropic.com",
                )

                dom_summary = _summarize_dom_for_llm(dom)
                prompt = (
                    f"You are analyzing the DOM of a web LLM chat interface.\n"
                    f"Site: {url}\n"
                    f"Title: {title}\n\n"
                    f"DOM snapshot (interactive elements only):\n{dom_summary}\n\n"
                    f"Heuristic selector suggestions (may be incomplete or wrong):\n"
                    f"{heuristic}\n\n"
                    f"Task: Return the best CSS selectors for these interaction points as JSON:\n"
                    f"- chat_input: where the user types their message\n"
                    f"- submit_button: the send/submit button\n"
                    f"- last_ai_msg: CSS selector for the last assistant response element\n"
                    f"- thinking_spinner: loading indicator while AI generates (null if not found)\n"
                    f"- new_chat: button/link to start a fresh conversation (null if not found)\n"
                    f"- model_selector: element to change the AI model (null if not found)\n"
                    f"- placeholders: list of transient placeholder strings the site shows while "
                    f"generating (e.g. 'Thinking...', 'Generating...')\n\n"
                    f"Return ONLY a JSON object with these keys. Use null for missing items. "
                    f"Prefer the most specific, stable selector (data-* attributes > aria-label > class)."
                )

                message = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=512,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw = message.content[0].text.strip()
                # Strip markdown code fences if present
                if raw.startswith("```"):
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                import json

                llm_suggestions = json.loads(raw)
            except Exception as exc:
                log.warning("LLM inspection failed: %s", exc)
                llm_error = str(exc)

    # Merge: LLM wins over heuristic where both have a value
    merged = {**heuristic}
    if llm_suggestions:
        for k, v in llm_suggestions.items():
            if v:
                merged[k] = v

    result = {
        "site": site,
        "url": url,
        "title": title,
        "heuristic_suggestions": heuristic,
        "llm_suggestions": llm_suggestions,
        "llm_error": llm_error,
        "merged_suggestions": merged,
    }

    if write and merged:
        yaml_path = _find_yaml_for_site(site)
        if yaml_path:
            _write_selectors_to_yaml(yaml_path, merged)
            result["written_to"] = str(yaml_path)
        else:
            result["write_error"] = f"Could not find YAML for site {site!r}"

    return result


def _summarize_dom_for_llm(dom: dict) -> str:
    """Produce a compact text summary of the DOM snapshot for the LLM prompt."""
    lines = []

    if dom.get("inputs"):
        lines.append("INPUTS/TEXTAREAS:")
        for el in dom["inputs"][:10]:
            lines.append(
                f"  <{el['tag']} type={el.get('type')} placeholder={el.get('placeholder')!r} "
                f"role={el.get('role')} aria-label={el.get('ariaLabel')!r} classes={el.get('classes')!r}>"
            )

    if dom.get("contenteditable"):
        lines.append("CONTENTEDITABLE:")
        for el in dom["contenteditable"][:5]:
            lines.append(
                f"  role={el.get('role')} aria-label={el.get('ariaLabel')!r} "
                f"placeholder={el.get('placeholder')!r} classes={el.get('classes')!r}"
            )

    if dom.get("buttons"):
        lines.append("BUTTONS (first 20):")
        for el in dom["buttons"][:20]:
            lines.append(
                f"  text={el.get('text')!r} type={el.get('type')} "
                f"aria-label={el.get('ariaLabel')!r} data-testid={el.get('dataTestid')!r} "
                f"disabled={el.get('disabled')} classes={el.get('classes')!r}"
            )

    if dom.get("chatLinks"):
        lines.append("CHAT/HISTORY LINKS:")
        for el in dom["chatLinks"][:10]:
            lines.append(
                f"  href={el.get('href')!r} text={el.get('text')!r} "
                f"aria-label={el.get('ariaLabel')!r}"
            )

    if dom.get("dataRoles"):
        lines.append("DATA-ROLE ELEMENTS:")
        for el in dom["dataRoles"][:10]:
            lines.append(
                f"  <{el['tag']} data-role={el.get('dataRole')!r} "
                f"data-message-role={el.get('dataMessageRole')!r} "
                f"text={el.get('textPreview')!r}>"
            )

    if dom.get("modelCandidates"):
        lines.append("MODEL SELECTOR CANDIDATES:")
        for el in dom["modelCandidates"][:5]:
            lines.append(
                f"  <{el['tag']} text={el.get('text')!r} aria-label={el.get('ariaLabel')!r}>"
            )

    return "\n".join(lines)


def _find_yaml_for_site(site_name: str) -> Path | None:
    """Find the YAML config file for a site."""
    for f in _SITES_DIR.glob("*.yaml"):
        import yaml

        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            if data.get("name") == site_name or f.stem == site_name:
                return f
        except Exception:
            continue
    return None


def _write_selectors_to_yaml(yaml_path: Path, suggestions: dict) -> None:
    """Merge suggested selectors into an existing site YAML config."""
    import yaml

    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))

    selector_keys = {
        "chat_input",
        "submit_button",
        "last_ai_msg",
        "thinking_spinner",
        "new_chat",
    }
    selectors = data.get("selectors") or {}
    for k, v in suggestions.items():
        if k in selector_keys and v:
            selectors[k] = v
    if selectors:
        data["selectors"] = selectors

    if "placeholders" in suggestions and suggestions["placeholders"]:
        data["placeholders"] = suggestions["placeholders"]

    yaml_path.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    log.info("Wrote selectors to %s", yaml_path)


# ---------------------------------------------------------------------------
# Anthropic Messages API
# ---------------------------------------------------------------------------


@app.get("/v1/models")
async def list_models():
    # Return real Claude model IDs so Claude Code's client-side validator accepts them.
    # The proxy ignores the model field and always routes to use.ai.
    return ModelsResponse(
        data=[
            ModelObject(id="claude-opus-4-6"),
            ModelObject(id="claude-sonnet-4-6"),
            ModelObject(id="claude-haiku-4-5-20251001"),
            # Legacy IDs in case Claude Code is on an older version
            ModelObject(id="claude-opus-4-5"),
            ModelObject(id="claude-sonnet-4-5"),
        ]
    )


@app.post("/v1/messages")
async def messages(request: MessagesRequest, raw: Request):
    log.info("POST /v1/messages model=%s stream=%s", request.model, request.stream)
    await browser_session.ensure_ready()
    if not browser_session.is_ready:
        raise HTTPException(status_code=503, detail="Browser session not ready")

    if request.stream:
        return StreamingResponse(
            translator.stream(request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        response = await translator.complete(request)
        return response
    except TimeoutError:
        raise HTTPException(status_code=504, detail="use.ai response timed out")
    except Exception as exc:
        log.exception("Error during completion")
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Generic web proxy — used by the /proxy skill
# ---------------------------------------------------------------------------


class ProxyRequest(BaseModel):
    site: str
    prompt: str
    model: str | None = None  # friendly name from site's models map, e.g. "claude-sonnet"


@app.post("/v1/proxy")
async def proxy_prompt(body: ProxyRequest, raw: Request):
    """Send a raw prompt to any configured web LLM site and return the response.

    The site must have a YAML config in proxy/sites/.
    The session is initialized on first use (may prompt for login in headed mode).
    """
    log.info("POST /v1/proxy site=%s prompt_len=%d", body.site, len(body.prompt))
    try:
        site_session = await site_manager.get(body.site)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    # If the client disconnected while we were waiting for session init (e.g. during login),
    # abort rather than holding the lock and running a full browser interaction for nobody.
    if await raw.is_disconnected():
        log.info("Client disconnected after session init for site=%s — aborting", body.site)
        raise HTTPException(status_code=499, detail="Client disconnected")

    if not site_session.is_ready:
        raise HTTPException(
            status_code=503, detail=f"Session for {body.site!r} not ready"
        )

    sel = SiteSelectors.from_config(site_session.config)
    extra_placeholders = frozenset(site_session.config.placeholders)

    async with site_session.lock:
        page = site_session.page
        await scraper.start_new_chat(page, sel)

        # Before typing, check if the page is showing a login wall.
        # This catches expired sessions and sites that allow anonymous navigation.
        if body.model:
            model_label = site_session.config.models.get(body.model)
            if model_label:
                await scraper.select_model(page, model_label, site_session.config.model_selector or "")
            else:
                log.warning("Unknown model %r for site %r — using default", body.model, body.site)

        await scraper.type_message(page, body.prompt, sel)
        await scraper.submit_message(page, sel)
        text = await streaming.wait_for_complete_response(page, sel, extra_placeholders)

    return {"site": body.site, "model": body.model, "text": text}


# ---------------------------------------------------------------------------
# Catch-all: log unknown routes so we can see what Claude Code is calling
# ---------------------------------------------------------------------------


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def catch_all(path: str, raw: Request):
    body = await raw.body()
    log.warning(
        "UNHANDLED %s /%s  body=%s",
        raw.method,
        path,
        body[:500].decode(errors="replace") if body else "(empty)",
    )
    raise HTTPException(status_code=404, detail=f"Unknown endpoint: /{path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run():
    uvicorn.run(
        "proxy.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    run()
