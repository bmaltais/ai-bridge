"""
FastAPI entrypoint for the ai-bridge proxy.

Exposes:
  POST /v1/proxy              — send a prompt to any configured web LLM site
  GET  /inspect/{site}        — auto-discover selectors via DOM analysis (returns data for Claude to analyze)
  GET  /debug/selectors/{site} — diagnose selectors on a site's browser session
  GET  /debug/html/{site}     — dump candidate response elements for a site
  POST /debug/eval/{site}     — run arbitrary JS on a site's browser page
  POST /debug/dismiss-modal/{site} — dismiss overlays/modals
  POST /debug/save-cookies/{site}  — persist current session cookies to disk
  GET  /debug/find-text/{site}     — find elements with substantial text
  GET  /session/status/{site}      — check session state
  POST /session/notify-login/{site} — signal login complete
  GET  /health                — liveness check
  GET  /v1/health/detailed    — detailed session status for all sites
  GET  /v1/metrics            — request/error/latency metrics for all sites
"""

import io
import time
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from proxy import inspector, lifecycle, scraper, streaming
from proxy.config import settings
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
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Browser sessions are initialized lazily on first request.")
    yield
    log.info("Shutting down browser sessions...")
    await site_manager.close_all()
    try:
        lifecycle.PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


app = FastAPI(title="ai-bridge proxy", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/health/detailed")
async def health_detailed():
    """Return detailed session status for all initialized sites.

    Returns status (ready/logged-in/waiting) for each site.
    """
    result = {}
    for site_name in site_manager._sessions.keys():
        result[site_name] = site_manager.get_status(site_name)
    return {"status": "ok", "sites": result}


@app.get("/v1/metrics")
async def metrics():
    """Return request/error/latency metrics for all sites.

    Fast endpoint — returns aggregated counters (no blocking operations).
    """
    return {
        "status": "ok",
        "metrics": site_manager.get_metrics(),
    }


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


@app.get("/debug/selectors/{site}")
async def debug_selectors_site(site: str):
    """Run selector diagnosis on a specific site's browser page."""
    try:
        site_session = await site_manager.get(site)
    except (FileNotFoundError, ValueError) as exc:
        elapsed_ms = int((time.time() - start_time) * 1000)
        site_manager.record_request(body.site, elapsed_ms, error=True)
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
        elapsed_ms = int((time.time() - start_time) * 1000)
        site_manager.record_request(body.site, elapsed_ms, error=True)
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
        elapsed_ms = int((time.time() - start_time) * 1000)
        site_manager.record_request(body.site, elapsed_ms, error=True)
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
        elapsed_ms = int((time.time() - start_time) * 1000)
        site_manager.record_request(body.site, elapsed_ms, error=True)
        raise HTTPException(status_code=404, detail=str(exc))
    page = site_session.page
    await page.keyboard.press("Escape")
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
    return site_manager.get_status(site)


@app.post("/session/notify-login/{site}")
async def session_notify_login(site: str):
    """Signal that the user has completed login for a site session.

    Call: curl -X POST http://127.0.0.1:8080/session/notify-login/perplexity
    """
    if await site_manager.notify_login(site):
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
        elapsed_ms = int((time.time() - start_time) * 1000)
        site_manager.record_request(body.site, elapsed_ms, error=True)
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
        elapsed_ms = int((time.time() - start_time) * 1000)
        site_manager.record_request(body.site, elapsed_ms, error=True)
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


@app.get("/inspect/{site}")
async def inspect_site(
    site: str,
    write: bool = Query(
        False, description="Write suggested selectors back to the YAML config"
    ),
):
    """
    Inspect a site's DOM and return heuristic CSS selector suggestions.

    Returns the raw DOM snapshot and heuristic suggestions for Claude to analyze.
    Pass ?write=true to save the heuristic suggestions directly into the site's YAML config.

    Call: curl http://127.0.0.1:8080/inspect/perplexity
    """
    try:
        site_session = await site_manager.get(site)
    except (FileNotFoundError, ValueError) as exc:
        elapsed_ms = int((time.time() - start_time) * 1000)
        site_manager.record_request(body.site, elapsed_ms, error=True)
        raise HTTPException(status_code=404, detail=str(exc))
    if not site_session.is_ready:
        raise HTTPException(status_code=503, detail=f"Session for {site!r} not ready")

    page = site_session.page
    url = page.url
    title = await page.title()

    dom = await page.evaluate(inspector.INSPECT_JS)
    suggestions = inspector.heuristic_selectors(dom)

    result = {
        "site": site,
        "url": url,
        "title": title,
        "suggestions": suggestions,
        "dom": dom,
    }

    if write and suggestions:
        yaml_path = inspector.find_yaml_for_site(site, _SITES_DIR)
        if yaml_path:
            inspector.write_selectors_to_yaml(yaml_path, suggestions)
            result["written_to"] = str(yaml_path)
        else:
            result["write_error"] = f"Could not find YAML for site {site!r}"

    return result


# ---------------------------------------------------------------------------
# Generic web proxy — POST a prompt to any configured site
# ---------------------------------------------------------------------------


class ProxyRequest(BaseModel):
    site: str
    prompt: str
    model: str | None = (
        None  # friendly name from site's models map, e.g. "claude-sonnet"
    )
    chat_url: str | None = None  # resume existing chat thread; if None, starts new chat
    new_conversation: bool | None = (
        None  # True=force new chat, False=never, None=heuristic
    )


class ControlRequest(BaseModel):
    site: str
    capability: str  # e.g. "new_chat", "model_selector"
    value: str | float | None = None  # required for fill/select/set_value actions


@app.post("/v1/proxy")
async def proxy_prompt(body: ProxyRequest, raw: Request):
    """Send a prompt to any configured web LLM site and return the response.

    The site must have a YAML config in proxy/sites/.
    The session is initialized on first use (may prompt for login in headed mode).
    """
    log.info("POST /v1/proxy site=%s prompt_len=%d", body.site, len(body.prompt))
    start_time = time.time()

    try:
        site_session = await site_manager.get(body.site)
    except (FileNotFoundError, ValueError) as exc:
        elapsed_ms = int((time.time() - start_time) * 1000)
        site_manager.record_request(body.site, elapsed_ms, error=True)
        raise HTTPException(status_code=404, detail=str(exc))

    if await raw.is_disconnected():
        log.info(
            "Client disconnected after session init for site=%s — aborting", body.site
        )
        elapsed_ms = int((time.time() - start_time) * 1000)
        site_manager.record_request(body.site, elapsed_ms, error=True)
        raise HTTPException(status_code=499, detail="Client disconnected")

    if not site_session.is_ready:
        elapsed_ms = int((time.time() - start_time) * 1000)
        site_manager.record_request(body.site, elapsed_ms, error=True)
        raise HTTPException(
            status_code=503, detail=f"Session for {body.site!r} not ready"
        )

    sel = SiteSelectors.from_config(site_session.config)
    extra_placeholders = frozenset(site_session.config.placeholders)

    last_exc: Exception | None = None
    async with site_session.lock:
        for attempt in range(1, 3):  # max 2 attempts: action → recover → retry
            try:
                page = site_session.page

                # new_conversation: explicit flag > heuristic (no chat_url = fresh context wanted)
                want_new = body.new_conversation
                if want_new is None:
                    want_new = not body.chat_url  # heuristic: no URL means new thread

                if want_new and "new_chat" in site_session.config.capabilities:
                    log.info("Invoking new_chat capability before prompt")
                    await scraper.invoke_capability(
                        page, "new_chat", site_session.config
                    )

                if site_session.config.skip_new_chat and not body.chat_url:
                    # Cloudflare-protected sites: do not navigate; capture existing content so
                    # wait_for_complete_response knows to ignore it and wait for new content.
                    init_text = await scraper.get_last_ai_message_text(page, sel)
                    log.debug("skip_new_chat: init_text len=%d", len(init_text))
                else:
                    # chat_url provided: always navigate (resume specific conversation).
                    # skip_new_chat=False: normal new-chat flow.
                    await scraper.goto_or_start_chat(page, sel, chat_url=body.chat_url)
                    init_text = ""

                if body.model:
                    model_label = site_session.config.models.get(body.model)
                    if model_label:
                        await scraper.select_model(
                            page, model_label, site_session.config.model_selector or ""
                        )
                    else:
                        log.warning(
                            "Unknown model %r for site %r — using default",
                            body.model,
                            body.site,
                        )

                await scraper.type_message(page, body.prompt, sel)
                await scraper.submit_message(page, sel)
                text = await streaming.wait_for_complete_response(
                    page,
                    sel,
                    extra_placeholders,
                    init_text=init_text,
                    fallback_detection=site_session.config.fallback_detection,
                    chat_input_sel=sel.chat_input,
                    completion_signal=site_session.config.completion_signal,
                    stable_threshold_ms=site_session.config.stable_threshold_ms,
                )
                last_exc = None
                elapsed_ms = int((time.time() - start_time) * 1000)
                site_manager.record_request(body.site, elapsed_ms, error=False)
                break  # success

            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    log.warning(
                        "Proxy attempt %d failed (%s: %s) — trying in-place recovery",
                        attempt,
                        type(exc).__name__,
                        exc,
                    )
                    recovered = await site_session.session.recover(body.chat_url)
                    if not recovered:
                        log.warning("In-place recovery failed for %s", body.site)
                        break  # no point retrying; caller gets 503

    if last_exc is not None:
        elapsed_ms = int((time.time() - start_time) * 1000)
        site_manager.record_request(body.site, elapsed_ms, error=True)
        raise HTTPException(
            status_code=503,
            detail=f"Proxy failed after recovery attempt: {last_exc}",
        )

    return {"site": body.site, "model": body.model, "text": text, "chat_url": page.url}


# ---------------------------------------------------------------------------
# Capability API
# ---------------------------------------------------------------------------


@app.get("/v1/capabilities/{site}")
async def get_capabilities(site: str):
    """Return the capabilities dict from a site's YAML config (no DOM scan).

    Call: curl http://127.0.0.1:8080/v1/capabilities/x-grok
    """
    try:
        site_session = await site_manager.get(site)
    except (FileNotFoundError, ValueError) as exc:
        elapsed_ms = int((time.time() - start_time) * 1000)
        site_manager.record_request(body.site, elapsed_ms, error=True)
        raise HTTPException(status_code=404, detail=str(exc))
    caps = site_session.config.capabilities
    return {
        "site": site,
        "capabilities": {
            name: {
                "type": cap.type,
                "action": cap.action,
                "description": cap.description,
                "options": cap.options,
                "requires_confirmation": cap.requires_confirmation,
            }
            for name, cap in caps.items()
        },
    }


@app.post("/v1/control")
async def control_capability(body: ControlRequest):
    """Invoke a named capability on a site's browser session.

    Call: curl -X POST http://127.0.0.1:8080/v1/control \\
            -H 'Content-Type: application/json' \\
            -d '{"site": "x-grok", "capability": "new_chat"}'
    """
    log.info(
        "POST /v1/control site=%s capability=%s value=%r",
        body.site,
        body.capability,
        body.value,
    )
    try:
        site_session = await site_manager.get(body.site)
    except (FileNotFoundError, ValueError) as exc:
        elapsed_ms = int((time.time() - start_time) * 1000)
        site_manager.record_request(body.site, elapsed_ms, error=True)
        raise HTTPException(status_code=404, detail=str(exc))
    if not site_session.is_ready:
        raise HTTPException(
            status_code=503, detail=f"Session for {body.site!r} not ready"
        )

    async with site_session.lock:
        try:
            await scraper.invoke_capability(
                site_session.page, body.capability, site_session.config, body.value
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail=f"Capability invocation failed: {exc}"
            )

    return {
        "status": "ok",
        "site": body.site,
        "capability": body.capability,
        "chat_url": site_session.page.url,
    }


# ---------------------------------------------------------------------------
# Catch-all: log unknown routes
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
    host, port = settings.host, settings.port

    lifecycle.resolve_port_conflict(host, port)
    lifecycle.write_pid()

    watchdog_pid = int(os.environ.get("WATCHDOG_PID", "0")) or os.getppid()
    if watchdog_pid > 1:  # PID 1 = system/init, always alive — skip watchdog
        lifecycle.start_watchdog(watchdog_pid)
    else:
        log.info("No valid watchdog PID — proxy will run until manually stopped.")

    log.info("Proxy ready on http://%s:%d", host, port)
    uvicorn.run(
        "proxy.main:app",
        host=host,
        port=port,
        log_level="info",
        # Explicit settings — skip auto-detection overhead (~30-150ms).
        lifespan="on",
        interface="asgi3",
        ws="none",  # no WebSocket routes
    )


if __name__ == "__main__":
    run()
