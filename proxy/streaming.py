"""
Response completion detection.

Strategy (default): poll the last AI message element every POLL_INTERVAL_MS.
When text is stable for STABLE_THRESHOLD_MS and the thinking spinner is gone,
we consider the response complete.

Strategy (submit_button_enabled): use the submit button DOM state as the primary
completion gate. Phase 1 waits for the button to become disabled/absent (confirms
generation started). Phase 2 waits for the button to re-enable (generation done),
then confirms with 2 stability ticks. This is reliable for sites like x.com/i/grok
where the button is disabled or replaced by a stop button during generation, making
it a binary DOM signal rather than a timing heuristic — mid-response pauses (e.g.
Grok "thinking" phases between paragraphs) never trigger false completion.

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

# How long to wait (s) for the submit button to go disabled after submission.
# Fast responses may skip disabled state entirely — that's fine, we fall through.
_BUTTON_DISABLE_WAIT_S = 5.0

# Stability ticks required after button re-enables before we return.
# 2 ticks × POLL_INTERVAL_MS (200ms default) = 400ms — enough to catch a final
# streaming burst without waiting a full STABLE_THRESHOLD_MS cycle.
_BUTTON_STABLE_TICKS = 2


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
    # Grok (x.com/i/grok) shows a "Thinking [about <topic>]\n<one-line summary>" preview
    # in the last_ai_msg element before the actual response starts streaming.  Variants:
    #   "Thinking about the user's request\n<summary>"  — complex questions (34 chars before \n)
    #   "Thinking\n<summary>"                           — simpler questions (8 chars before \n)
    # Both are multi-line, so the single-line Searching guard doesn't catch them.
    # The exact strings "Thinking" / "Thinking..." are already in _BUILTIN_PLACEHOLDERS
    # but only as exact matches.  This startswith guard covers the multi-line variants.
    # Threshold is 60 (not 30) to cover the longest known prefix "Thinking about the user's
    # request" (34 chars). Real responses are always substantially longer than 2 lines.
    if t.startswith("Thinking") and "\n" in t and t.index("\n") < 60:
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


async def _read_text(
    page: Page,
    sel: SiteSelectors,
    using_fallback: bool,
    input_sel: str,
) -> str:
    """Read the current last AI message text, using fallback DOM scan if activated."""
    if using_fallback:
        return await _dom_scan_last_message(page, input_sel)
    return await scraper.get_last_ai_message_text(page, sel)


async def wait_for_complete_response(
    page: Page,
    sel: SiteSelectors | None = None,
    extra_placeholders: frozenset[str] = frozenset(),
    init_text: str = "",
    fallback_detection: bool = False,
    chat_input_sel: str | None = None,
    completion_signal: str | None = None,
    stable_threshold_ms: int | None = None,
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
    completion_signal: "submit_button_enabled" → use button DOM state as primary gate
               (reliable for Grok); None → time-based stability (default).
    stable_threshold_ms: per-site override for the time-based stable threshold.
               None = use global settings.stable_threshold_ms.
    """
    sel = sel or SiteSelectors()
    poll = settings.poll_interval_ms / 1000
    threshold_ms = (
        stable_threshold_ms
        if stable_threshold_ms is not None
        else settings.stable_threshold_ms
    )
    stable_ticks_needed = max(1, round(threshold_ms / settings.poll_interval_ms))
    timeout = settings.response_timeout_s
    input_sel = chat_input_sel or sel.chat_input

    # Give the page a moment to start generating before we start polling.
    await asyncio.sleep(0.5)

    if completion_signal == "submit_button_enabled":
        return await _wait_button_signal(
            page,
            sel,
            extra_placeholders,
            init_text,
            fallback_detection,
            input_sel,
            poll,
            timeout,
        )

    # ---- Default: time-based stability path ----
    last_text = ""
    stable_count = 0
    elapsed = 0.0
    using_fallback = False

    while elapsed < timeout:
        await asyncio.sleep(poll)
        elapsed += poll

        if not using_fallback:
            current = await scraper.get_last_ai_message_text(page, sel)
            # Primary selector still empty after FALLBACK_TRIGGER_S — activate fallback.
            if fallback_detection and not current and elapsed >= _FALLBACK_TRIGGER_S:
                log.warning(
                    "Primary selector empty after %.0fs — activating DOM-scan fallback",
                    elapsed,
                )
                using_fallback = True
                continue
        else:
            current = await _dom_scan_last_message(page, input_sel)

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
        log.warning(
            "Response timed out after %ds (fallback=%s)", timeout, using_fallback
        )

    return last_text


async def _wait_button_signal(
    page: Page,
    sel: SiteSelectors,
    extra_placeholders: frozenset[str],
    init_text: str,
    fallback_detection: bool,
    input_sel: str,
    poll: float,
    timeout: float,
) -> str:
    """Button-signal completion path.

    Phase 1 (up to _BUTTON_DISABLE_WAIT_S): wait for the submit button to become
    disabled/absent, confirming generation has started.  Some fast responses skip this
    state entirely — that's fine, we fall through to Phase 2 immediately.

    Phase 2 (up to timeout): poll until the submit button re-enables AND the text has
    changed from init_text AND _is_complete() passes.  Then require _BUTTON_STABLE_TICKS
    consecutive stable reads before returning.  This absorbs any final streaming burst
    that arrives just as the button re-enables.
    """
    elapsed = 0.0
    using_fallback = False
    last_text = ""
    stable_count = 0

    # --- Phase 1: detect generation start ---
    phase1_elapsed = 0.0
    while phase1_elapsed < _BUTTON_DISABLE_WAIT_S:
        await asyncio.sleep(poll)
        phase1_elapsed += poll
        if not await scraper.is_submit_button_enabled(page, sel):
            log.debug(
                "Button disabled after %.1fs — generation confirmed started",
                phase1_elapsed,
            )
            break
    else:
        log.debug(
            "Button never went disabled in %.0fs — fast response or selector issue; "
            "proceeding to Phase 2 anyway",
            _BUTTON_DISABLE_WAIT_S,
        )
    elapsed += phase1_elapsed

    # --- Phase 2: wait for generation end ---
    while elapsed < timeout:
        await asyncio.sleep(poll)
        elapsed += poll

        # Read text (with fallback support)
        if not using_fallback:
            current = await scraper.get_last_ai_message_text(page, sel)
            if fallback_detection and not current and elapsed >= _FALLBACK_TRIGGER_S:
                log.warning(
                    "Primary selector empty after %.0fs — activating DOM-scan fallback",
                    elapsed,
                )
                using_fallback = True
                continue
        else:
            current = await _dom_scan_last_message(page, input_sel)

        if current == last_text:
            stable_count += 1
        else:
            stable_count = 0
            last_text = current

        # Button re-enabled: generation done. Confirm with stability ticks.
        btn_enabled = await scraper.is_submit_button_enabled(page, sel)
        if (
            btn_enabled
            and _is_complete(last_text, extra_placeholders)
            and last_text != init_text
            and stable_count >= _BUTTON_STABLE_TICKS
        ):
            log.debug(
                "Button re-enabled + text stable (%d ticks) — response complete (len=%d)",
                stable_count,
                len(last_text),
            )
            break

    if elapsed >= timeout:
        log.warning("Button-signal response timed out after %ds", timeout)

    return last_text
