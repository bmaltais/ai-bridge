"""
Response completion detection.

Strategy: poll the last AI message element every POLL_INTERVAL_MS.
When text is stable for STABLE_THRESHOLD_MS and the thinking spinner is gone,
we consider the response complete.
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
    return True


async def wait_for_complete_response(
    page: Page,
    sel: SiteSelectors | None = None,
    extra_placeholders: frozenset[str] = frozenset(),
    init_text: str = "",
) -> str:
    """
    Block until the AI has finished generating its response.
    Returns the full text of the last assistant message.

    init_text: text that was already in last_ai_msg before the prompt was submitted.
               The response is only considered done when the text differs from this
               AND is stable. Pass this when skipping start_new_chat (e.g. Cloudflare
               sites) so we don't accidentally return stale content from a prior turn.
    """
    sel = sel or SiteSelectors()
    poll = settings.poll_interval_ms / 1000
    stable_ticks_needed = max(
        1, round(settings.stable_threshold_ms / settings.poll_interval_ms)
    )
    timeout = settings.response_timeout_s

    last_text = ""
    stable_count = 0
    elapsed = 0.0

    # Give the page a moment to start generating before we start polling
    await asyncio.sleep(0.5)

    while elapsed < timeout:
        await asyncio.sleep(poll)
        elapsed += poll

        current = await scraper.get_last_ai_message_text(page, sel)

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
        log.warning("Response timed out after %ds", timeout)

    return last_text
