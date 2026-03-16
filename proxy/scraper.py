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

# Module-level aliases kept for backward compatibility (browser.py uses CHAT_INPUT)
CHAT_INPUT = _CHAT_INPUT
SUBMIT_BUTTON = _SUBMIT_BUTTON
LAST_AI_MSG = _LAST_AI_MSG
THINKING_SPINNER = _THINKING_SPINNER
NEW_CHAT = _NEW_CHAT


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
        # Wait for the dropdown to appear
        option = page.locator(f"[role='option']").filter(has_text=model_label).first
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
