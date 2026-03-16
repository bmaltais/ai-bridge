"""
Response completion detection.

Strategy: poll the last AI message element every POLL_INTERVAL_MS.
When text is stable for STABLE_THRESHOLD_MS and the thinking spinner is gone,
we consider the response complete.

Fallback detection (opt-in per site via fallback_detection: true in YAML):
If the primary last_ai_msg selector returns empty for FALLBACK_TRIGGER_S seconds,
switch to a DOM scan — find the last leaf element with >100 chars of text inside
the <main> container.  This survives hashed CSS class changes (e.g. x.com deploys).
"""

import asyncio
import logging

from playwright.async_api import Page

from proxy import scraper
from proxy.config import settings
from proxy.scraper import SiteSelectors

log = logging.getLogger(__name__)

# Built-in placeholder texts that indicate the AI is still generating.
# Sites can add their own via SiteConfig.placeholders.
_BUILTIN_PLACEHOLDERS = frozenset({"Thinking...", "Thinking", "...", ""})

# How long to wait on an empty primary selector before activating DOM-scan fallback.
_FALLBACK_TRIGGER_S = 5.0


def _is_complete(text: str, extra_placeholders: frozenset[str] = frozenset()) -> bool:
    """Return True if `text` looks like a real completed response."""
    t = text.strip()
    if t in _BUILTIN_PLACEHOLDERS or t in extra_placeholders:
        return False
    # Some sites prefix each message with the model name, e.g. "Using OpenAI GPT-5.1".
    # If the entire text is just that badge line, the actual content hasn't arrived yet.
    if t.startswith("Using ") and "\n" not in t and len(t) < 50:
        return False
    # Some sites show a transient search indicator, e.g. "Searching Write a short one..."
    # before the real response arrives.  Treat any single-line "Searching …" fragment as
    # a loading placeholder so we keep polling.
    if t.startswith("Searching") and "\n" not in t and len(t) < 80:
        return False
    # Grok (x.com/i/grok) shows a "Thinking about <topic>\n<one-line summary>" preview
    # in the last_ai_msg element before the actual response starts streaming.  This is
    # multi-line so the single-line Searching guard doesn't catch it.  No real response
    # starts with "Thinking about", so this guard is safe unconditionally.
    if t.startswith("Thinking about"):
        return False
    return True


async def _dom_scan_last_message(page: Page, chat_input_sel: str) -> str:
    """Fallback: scan the <main> container for the last leaf element with substantial text.

    Used when the primary last_ai_msg selector returns empty (e.g. after a CSS class
    rename on x.com).  Returns empty string if nothing is found.
    """
    try:
        return await page.evaluate(
            """(chat_input_sel) => {
                const input = document.querySelector(chat_input_sel);
                const container = (input && input.closest('main'))
                    || document.querySelector('[role="main"]')
                    || document.querySelector('main')
                    || document.body;
                // Leaf elements (no child elements) with at least 100 chars of text.
                const leaves = Array.from(container.querySelectorAll('*')).filter(el =>
                    el.children.length === 0 &&
                    (el.innerText || '').trim().length > 100
                );
                if (!leaves.length) return '';
                return leaves[leaves.length - 1].innerText.trim();
            }""",
            chat_input_sel,
        )
    except Exception as exc:
        log.debug("_dom_scan_last_message error: %s", exc)
        return ""


async def wait_for_complete_response(
    page: Page,
    sel: SiteSelectors | None = None,
    extra_placeholders: frozenset[str] = frozenset(),
    init_text: str = "",
    fallback_detection: bool = False,
    chat_input_sel: str | None = None,
) -> str:
    """
    Block until the AI has finished generating its response.
    Returns the full text of the last assistant message.

    init_text: text that was already in last_ai_msg before the prompt was submitted.
               The response is only considered done when the text differs from this
               AND is stable. Pass this when skipping start_new_chat (e.g. Cloudflare
               sites) so we don't accidentally return stale content from a prior turn.
    fallback_detection: when True, if the primary selector returns empty for
               FALLBACK_TRIGGER_S seconds, switch to a DOM scan of leaf elements.
               Enable for sites with volatile hashed CSS classes (e.g. x.com).
    chat_input_sel: CSS selector for the chat input — used to find the <main>
               container in DOM-scan fallback.  Defaults to sel.chat_input.
    """
    sel = sel or SiteSelectors()
    poll = settings.poll_interval_ms / 1000
    stable_ticks_needed = max(
        1, round(settings.stable_threshold_ms / settings.poll_interval_ms)
    )
    timeout = settings.response_timeout_s
    input_sel = chat_input_sel or sel.chat_input

    last_text = ""
    stable_count = 0
    elapsed = 0.0
    using_fallback = False

    # Give the page a moment to start generating before we start polling
    await asyncio.sleep(0.5)

    while elapsed < timeout:
        await asyncio.sleep(poll)
        elapsed += poll

        if using_fallback:
            current = await _dom_scan_last_message(page, input_sel)
        else:
            current = await scraper.get_last_ai_message_text(page, sel)
            # Primary selector still empty after FALLBACK_TRIGGER_S — activate fallback.
            if (
                fallback_detection
                and not current
                and elapsed >= _FALLBACK_TRIGGER_S
            ):
                log.warning(
                    "Primary selector empty after %.0fs — activating DOM-scan fallback",
                    elapsed,
                )
                using_fallback = True
                continue

        if current == last_text:
            stable_count += 1
        else:
            stable_count = 0
            last_text = current

        # Stable for long enough, not a placeholder, and different from the pre-submit state?
        if (
            stable_count >= stable_ticks_needed
            and _is_complete(last_text, extra_placeholders)
            and last_text != init_text
        ):
            # Double-check spinner is gone
            if not await scraper.is_thinking(page, sel):
                break
            # Spinner still visible — keep waiting
            stable_count = 0

    if elapsed >= timeout:
        log.warning("Response timed out after %ds (fallback=%s)", timeout, using_fallback)

    return last_text
