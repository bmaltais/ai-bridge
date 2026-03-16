"""
Web LLM DOM interaction layer.

All CSS selectors live here. When a site's UI changes, only the YAML config
(or this file's defaults) needs updating.

`SiteSelectors` merges per-site config overrides with the generic defaults so
callers never have to handle None. Pass a `SiteSelectors` instance to every
function; omit it to use the generic fallback chain.

Run with HEADLESS=false to inspect selectors in a visible browser.
"""

import logging
from dataclasses import dataclass

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default selectors — ordered from most specific to most general fallback
# ---------------------------------------------------------------------------

_CHAT_INPUT = (
    '[role="textbox"], '
    'textarea[placeholder*="message" i], '
    'textarea[placeholder*="ask" i], '
    'textarea[placeholder*="type" i], '
    'div[contenteditable="true"]'
)

_SUBMIT_BUTTON = (
    'button[type="submit"], '
    'button[aria-label*="send" i], '
    'button[aria-label*="submit" i], '
    'button[data-testid*="send" i]'
)

# Use data-role attributes first — they're explicit and stable.
# Avoid [class*="assistant"] — too broad, matches model-name badges.
_LAST_AI_MSG = (
    '[data-role="assistant"], ' '[data-message-role="assistant"], ' ".assistant-message"
)

_THINKING_SPINNER = (
    '[aria-label*="loading" i], '
    '[aria-label*="thinking" i], '
    ".typing-indicator, "
    '[data-testid*="thinking" i], '
    '[class*="loading" i], '
    '[class*="thinking" i], '
    '[class*="spinner" i]'
)

_NEW_CHAT = (
    'a[href*="/chat/new" i], '
    'button[aria-label*="new chat" i], '
    'button[aria-label*="new conversation" i], '
    'a[aria-label*="new chat" i]'
)

# Login page indicator (narrow — used by browser.py auth check)
LOGIN_INDICATOR = 'input[type="email"], input[name="email"]'


# ---------------------------------------------------------------------------
# SiteSelectors — merged, always-resolved selector set
# ---------------------------------------------------------------------------


@dataclass
class SiteSelectors:
    """Resolved CSS selectors for one site. Defaults are the generic fallback chains."""

    chat_input: str = _CHAT_INPUT
    submit_button: str = _SUBMIT_BUTTON
    last_ai_msg: str = _LAST_AI_MSG
    thinking_spinner: str = _THINKING_SPINNER
    new_chat: str = _NEW_CHAT

    @classmethod
    def from_config(cls, config) -> "SiteSelectors":
        """Build from a SiteConfig, falling back to defaults for unset fields."""
        if config is None:
            return cls()
        return cls(
            chat_input=config.chat_input or _CHAT_INPUT,
            submit_button=config.submit_button or _SUBMIT_BUTTON,
            last_ai_msg=config.last_ai_msg or _LAST_AI_MSG,
            thinking_spinner=config.thinking_spinner or _THINKING_SPINNER,
            new_chat=config.new_chat or _NEW_CHAT,
        )


_DEFAULT = SiteSelectors()


# ---------------------------------------------------------------------------
# DOM actions
# ---------------------------------------------------------------------------


async def type_message(page: Page, text: str, sel: SiteSelectors = _DEFAULT) -> None:
    """Clear the chat input and type the given text."""
    await page.wait_for_selector(sel.chat_input, timeout=10_000)
    await page.click(sel.chat_input)
    # Use fill for textarea; for contenteditable, triple-click + type
    try:
        await page.fill(sel.chat_input, text)
    except Exception:
        await page.triple_click(sel.chat_input)
        await page.keyboard.type(text)


async def submit_message(page: Page, sel: SiteSelectors = _DEFAULT) -> None:
    """Click the send button, or press Enter if the button is not found."""
    try:
        btn = page.locator(sel.submit_button).first
        await btn.wait_for(state="visible", timeout=3_000)
        await btn.click()
    except Exception:
        log.debug("Submit button not found, pressing Enter instead")
        await page.keyboard.press("Enter")


async def get_last_ai_message_text(page: Page, sel: SiteSelectors = _DEFAULT) -> str:
    """Return the current text content of the last assistant message."""
    try:
        loc = page.locator(sel.last_ai_msg).last
        return await loc.inner_text(timeout=2_000)
    except Exception:
        return ""


async def diagnose_selectors(page: Page, sel: SiteSelectors = _DEFAULT) -> None:
    """Log what elements are actually present — call this to debug selector issues."""
    url = page.url
    log.info("=== SELECTOR DIAGNOSIS (url=%s) ===", url)

    checks = {
        "CHAT_INPUT": sel.chat_input,
        "SUBMIT_BUTTON": sel.submit_button,
        "LAST_AI_MSG": sel.last_ai_msg,
        "THINKING_SPINNER": sel.thinking_spinner,
        "NEW_CHAT": sel.new_chat,
    }
    for name, selector in checks.items():
        try:
            count = await page.locator(selector).count()
            if count > 0:
                text = await page.locator(selector).last.inner_text(timeout=1000)
                log.info("  %-20s FOUND (%d) text=%r", name, count, text[:80])
            else:
                log.info("  %-20s NOT FOUND (0 matches)", name)
        except Exception as exc:
            log.info("  %-20s ERROR: %s", name, exc)

    title = await page.title()
    log.info("  PAGE TITLE: %r", title)
    log.info("=== END DIAGNOSIS ===")


async def is_thinking(page: Page, sel: SiteSelectors = _DEFAULT) -> bool:
    """Return True if a loading/thinking indicator is visible."""
    try:
        loc = page.locator(sel.thinking_spinner).first
        return await loc.is_visible()
    except Exception:
        return False


async def is_submit_button_enabled(page: Page, sel: SiteSelectors = _DEFAULT) -> bool:
    """Return True if the send button is not disabled."""
    try:
        btn = page.locator(sel.submit_button).first
        disabled = await btn.get_attribute("disabled")
        return disabled is None
    except Exception:
        return True


async def select_model(page: Page, model_label: str, model_selector: str) -> bool:
    """Open the model picker and click the option matching model_label.

    Returns True if the model was selected, False if not found or no selector configured.
    model_label should be the exact label as shown in the picker (e.g. 'Claude Sonnet 4.6').
    """
    if not model_selector:
        return False
    try:
        btn = page.locator(model_selector).first
        await btn.wait_for(state="visible", timeout=5_000)
        await btn.click()
        # Wait for the dropdown to appear — some sites use [role='option'],
        # others (e.g. x.com/i/grok) use [role='menuitem'].  Try both.
        option = (
            page.locator("[role='option'],[role='menuitem']")
            .filter(has_text=model_label)
            .first
        )
        await option.wait_for(state="visible", timeout=3_000)
        await option.click()
        log.info("Model selected: %r", model_label)
        return True
    except Exception as exc:
        log.warning("select_model failed for %r: %s", model_label, exc)
        return False


async def start_new_chat(page: Page, sel: SiteSelectors = _DEFAULT) -> None:
    """Navigate to a fresh chat so there is no context carry-over."""
    try:
        loc = page.locator(sel.new_chat).first
        await loc.wait_for(state="visible", timeout=5_000)
        await loc.click()
        await page.wait_for_selector(sel.chat_input, timeout=30_000)
    except PlaywrightTimeoutError:
        # Navigate to the site root rather than reloading — avoids landing on
        # an OAuth redirect URL that may not have the chat input.
        origin = "/".join(page.url.split("/")[:3])
        log.debug("New-chat button not found, navigating to %s", origin)
        await page.goto(origin, wait_until="domcontentloaded")
        await page.wait_for_selector(sel.chat_input, timeout=30_000)
    except Exception as exc:
        log.warning("start_new_chat failed: %s", exc)


async def invoke_capability(
    page: Page,
    cap_name: str,
    site_config,  # SiteConfig — avoid circular import
    value=None,
) -> None:
    """Invoke a named UI capability (new_chat, model_selector, etc.).

    Known capabilities route to existing helpers for consistency.
    Unknown capabilities are dispatched generically via action type.
    """
    from proxy.site_config import CapabilityConfig  # local import to avoid cycle

    caps: dict[str, CapabilityConfig] = site_config.capabilities
    if cap_name not in caps:
        raise ValueError(
            f"Capability {cap_name!r} not found for site {site_config.name!r}. "
            f"Available: {list(caps)}"
        )
    cap = caps[cap_name]

    # Route known capabilities to existing helpers
    if cap_name == "new_chat":
        sel = SiteSelectors.from_config(site_config)
        # Navigate to site_config.url rather than calling start_new_chat() — avoids
        # the origin-fallback bug where start_new_chat() navigates to https://x.com
        # (site root) instead of https://x.com/i/grok (the actual chat URL).
        log.info("new_chat: navigating to %s", site_config.url)
        await page.goto(site_config.url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_selector(sel.chat_input, timeout=15_000)
        return
    if cap_name == "model_selector":
        if value is None:
            raise ValueError("model_selector requires a value (model label)")
        await select_model(page, str(value), cap.selector)
        return
    if cap_name == "grok_420_beta":
        # Toggle is inside the Auto dropdown — must open it first.
        sel = SiteSelectors.from_config(site_config)
        model_cap = site_config.capabilities.get("model_selector")
        if model_cap:
            btn = page.locator(model_cap.selector).first
            await btn.wait_for(state="visible", timeout=5_000)
            await btn.click()
            await page.wait_for_timeout(500)
        toggle = page.locator(cap.selector).first
        await toggle.wait_for(state="attached", timeout=3_000)
        await toggle.evaluate("el => { el.click(); el.dispatchEvent(new Event('change', {bubbles: true})); }")
        log.info("grok_420_beta toggled")
        await page.keyboard.press("Escape")
        return

    # Generic fallback: dispatch by action type
    elem = await page.wait_for_selector(cap.selector, timeout=10_000)
    if elem is None:
        raise RuntimeError(f"Selector not found for capability {cap_name!r}: {cap.selector}")

    if cap.action == "click":
        await elem.click()
    elif cap.action == "fill":
        if value is None:
            raise ValueError(f"fill action requires a value for {cap_name!r}")
        await elem.fill(str(value))
    elif cap.action == "set_value":
        if value is None:
            raise ValueError(f"set_value action requires a value for {cap_name!r}")
        await elem.evaluate("(el, v) => { el.value = v; el.dispatchEvent(new Event('input')); }", value)
    elif cap.action == "select_by_value":
        if value is None:
            raise ValueError(f"select_by_value action requires a value for {cap_name!r}")
        await elem.select_option(value=str(value))
    elif cap.action == "select_by_label":
        if value is None:
            raise ValueError(f"select_by_label action requires a value for {cap_name!r}")
        await elem.select_option(label=str(value))
    elif cap.action == "toggle":
        await elem.evaluate("el => { el.checked = !el.checked; el.dispatchEvent(new Event('change')); }")
    else:
        raise ValueError(f"Unsupported action {cap.action!r} for capability {cap_name!r}")

    log.info("Capability %r invoked (action=%s, value=%r)", cap_name, cap.action, value)


async def goto_or_start_chat(
    page: Page, sel: SiteSelectors = _DEFAULT, chat_url: str | None = None
) -> None:
    """Resume an existing chat by URL, or start a fresh chat if no URL is given.

    Drop-in replacement for start_new_chat() — callers that pass only (page, sel)
    behave identically to before.  Pass chat_url to resume a thread.
    """
    if chat_url:
        try:
            if not chat_url.startswith("https://"):
                raise ValueError(f"chat_url must start with https://, got: {chat_url!r}")
            log.info("goto_or_start_chat: resuming %s", chat_url)
            await page.goto(chat_url, wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_selector(sel.chat_input, timeout=15_000)
            return  # successfully resumed
        except Exception as exc:
            log.warning("goto_or_start_chat: failed to resume %s (%s) — falling back to new chat", chat_url, exc)
    await start_new_chat(page, sel)
