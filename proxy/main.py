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
"""

import logging
import _thread
import io
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from proxy import scraper, streaming
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
        _PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


app = FastAPI(title="ai-bridge proxy", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok"}


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
    try:
        from proxy.site_config import SiteConfig

        key = SiteConfig.find(site, _SITES_DIR).name
    except Exception:
        key = site
    if key in site_manager._sessions:
        sess = site_manager._sessions[key].session
        return {
            "site": site,
            "canonical": key,
            "login_pending": sess.login_pending,
            "ready": sess.is_ready,
        }
    return {"site": site, "login_pending": False, "ready": False, "initialized": False}


@app.post("/session/notify-login/{site}")
async def session_notify_login(site: str):
    """Signal that the user has completed login for a site session.

    Call: curl -X POST http://127.0.0.1:8080/session/notify-login/perplexity
    """
    try:
        from proxy.site_config import SiteConfig

        key = SiteConfig.find(site, _SITES_DIR).name
    except Exception:
        key = site
    if key in site_manager._sessions:
        await site_manager._sessions[key].session.notify_login()
        return {"status": "notified", "site": site, "canonical": key}
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

    for el in dom.get("dataRoles", []):
        if el.get("dataRole") == "assistant":
            suggestions["last_ai_msg"] = '[data-role="assistant"]'
            break
    if not suggestions["last_ai_msg"]:
        for el in dom.get("dataRoles", []):
            if el.get("dataMessageRole") == "assistant":
                suggestions["last_ai_msg"] = '[data-message-role="assistant"]'
                break

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
        raise HTTPException(status_code=404, detail=str(exc))
    if not site_session.is_ready:
        raise HTTPException(status_code=503, detail=f"Session for {site!r} not ready")

    page = site_session.page
    url = page.url
    title = await page.title()

    dom = await page.evaluate(_INSPECT_JS)
    suggestions = _heuristic_selectors(dom)

    result = {
        "site": site,
        "url": url,
        "title": title,
        "suggestions": suggestions,
        "dom": dom,
    }

    if write and suggestions:
        yaml_path = _find_yaml_for_site(site)
        if yaml_path:
            _write_selectors_to_yaml(yaml_path, suggestions)
            result["written_to"] = str(yaml_path)
        else:
            result["write_error"] = f"Could not find YAML for site {site!r}"

    return result


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
# Generic web proxy — POST a prompt to any configured site
# ---------------------------------------------------------------------------


class ProxyRequest(BaseModel):
    site: str
    prompt: str
    model: str | None = (
        None  # friendly name from site's models map, e.g. "claude-sonnet"
    )
    chat_url: str | None = None  # resume existing chat thread; if None, starts new chat
    new_conversation: bool | None = None  # True=force new chat, False=never, None=heuristic


class ControlRequest(BaseModel):
    site: str
    capability: str          # e.g. "new_chat", "model_selector"
    value: str | float | None = None  # required for fill/select/set_value actions


@app.post("/v1/proxy")
async def proxy_prompt(body: ProxyRequest, raw: Request):
    """Send a prompt to any configured web LLM site and return the response.

    The site must have a YAML config in proxy/sites/.
    The session is initialized on first use (may prompt for login in headed mode).
    """
    log.info("POST /v1/proxy site=%s prompt_len=%d", body.site, len(body.prompt))
    try:
        site_session = await site_manager.get(body.site)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    if await raw.is_disconnected():
        log.info(
            "Client disconnected after session init for site=%s — aborting", body.site
        )
        raise HTTPException(status_code=499, detail="Client disconnected")

    if not site_session.is_ready:
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
                    await scraper.invoke_capability(page, "new_chat", site_session.config)

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
                )
                last_exc = None
                break  # success

            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    log.warning(
                        "Proxy attempt %d failed (%s: %s) — trying in-place recovery",
                        attempt, type(exc).__name__, exc,
                    )
                    recovered = await site_session.session.recover(body.chat_url)
                    if not recovered:
                        log.warning("In-place recovery failed for %s", body.site)
                        break  # no point retrying; caller gets 503

    if last_exc is not None:
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
    log.info("POST /v1/control site=%s capability=%s value=%r", body.site, body.capability, body.value)
    try:
        site_session = await site_manager.get(body.site)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if not site_session.is_ready:
        raise HTTPException(status_code=503, detail=f"Session for {body.site!r} not ready")

    async with site_session.lock:
        try:
            await scraper.invoke_capability(
                site_session.page, body.capability, site_session.config, body.value
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Capability invocation failed: {exc}")

    return {"status": "ok", "site": body.site, "capability": body.capability, "chat_url": site_session.page.url}


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


_PID_FILE = Path.home() / ".claude" / "ai-bridge" / "server.pid"


def _port_in_use(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


def _is_healthy(host: str, port: int) -> bool:
    try:
        import urllib.request

        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _read_pid() -> int | None:
    try:
        return int(_PID_FILE.read_text().strip())
    except Exception:
        return None


def _write_pid() -> None:
    import os

    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))


def _kill_pid(pid: int) -> None:
    import subprocess, sys

    if sys.platform == "win32":
        subprocess.call(
            ["powershell", "-Command", f"Stop-Process -Id {pid} -Force"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        subprocess.call(
            ["kill", "-9", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _pid_alive(pid: int) -> bool:
    """Return True if the given PID is still running."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        return exit_code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True  # process exists, we just lack permission to signal it
    except OSError:
        return False


def _start_watchdog(watchdog_pid: int) -> None:
    """Start a daemon thread that exits the proxy when watchdog_pid dies."""

    def _watch():
        log.info(
            "Watchdog monitoring PID %d — proxy will exit when that process ends.",
            watchdog_pid,
        )
        while True:
            time.sleep(10)
            if not _pid_alive(watchdog_pid):
                log.info("Watchdog PID %d is gone — shutting down proxy.", watchdog_pid)
                _PID_FILE.unlink(missing_ok=True)
                _thread.interrupt_main()  # raises KeyboardInterrupt in main thread; uvicorn handles graceful shutdown on all platforms
                return

    t = threading.Thread(target=_watch, daemon=True)
    t.start()


def run():
    host, port = settings.host, settings.port

    if _port_in_use(host, port):
        saved_pid = _read_pid()
        if _is_healthy(host, port):
            log.info(
                "ai-bridge already running and healthy at http://%s:%d — reusing it.",
                host,
                port,
            )
            sys.exit(0)

        # Port is occupied but not healthy.
        try:
            current_pid = (
                int(
                    __import__("subprocess")
                    .check_output(
                        [
                            "powershell",
                            "-Command",
                            f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
                            f"-ErrorAction SilentlyContinue).OwningProcess",
                        ],
                        text=True,
                        stderr=__import__("subprocess").DEVNULL,
                    )
                    .strip()
                )
                if sys.platform == "win32"
                else None
            )
        except Exception:
            current_pid = None

        if saved_pid and current_pid and saved_pid == current_pid:
            log.warning(
                "Port %d has a frozen ai-bridge process (PID %d) — killing it.",
                port,
                saved_pid,
            )
            _kill_pid(saved_pid)
            time.sleep(1)
        else:
            log.error(
                "Port %d is occupied by another application (PID %s). "
                "Change PORT in .env to avoid conflicts.",
                port,
                current_pid or "unknown",
            )
            sys.exit(1)

    _write_pid()

    # Start watchdog: monitor the process that launched us.
    # Pass WATCHDOG_PID=<pid> to override (e.g. the Claude Code process PID).
    # Defaults to the direct parent process.
    watchdog_pid = int(os.environ.get("WATCHDOG_PID", "0")) or os.getppid()
    if watchdog_pid > 1:  # PID 1 = system/init, always alive — skip watchdog
        _start_watchdog(watchdog_pid)
    else:
        log.info("No valid watchdog PID — proxy will run until manually stopped.")

    log.info("Proxy ready on http://%s:%d", host, port)
    uvicorn.run("proxy.main:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
