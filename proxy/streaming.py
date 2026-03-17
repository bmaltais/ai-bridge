"""
Response completion detection.

Strategy (default): poll the last AI message element every POLL_INTERVAL_MS.
When text is stable for STABLE_THRESHOLD_MS and the thinking spinner is gone,
we consider the response complete. Uses structured phase-based timeouts:
  - Phase 0 (initial): sleep(0.5) to let page start processing
  - Phase 1 (arrival): wait TEXT_ARRIVAL_TIMEOUT_S for first real content
  - Phase 2 (streaming): wait STREAMING_TIMEOUT_S for stability and completion

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

# ---- Phase-based timeout structure ----
# Instead of a single global 120s timeout, responses are gated by structured phases:
# - Phase 0 (initial): sleep(0.5) before first poll — let page start processing
# - Phase 1 (arrival): poll for first real (non-placeholder) text response
# - Phase 2 (streaming): poll for text stability and completion signals
#
# This allows fast responses to complete in 5-10s while preserving fallback for slow
# servers (e.g. Grok reasoning models) without a bloated single timeout.
_DOM_LOAD_TIMEOUT_S = 15.0  # Phase 0: max wait for DOM to settle after submission
_TEXT_ARRIVAL_TIMEOUT_S = 5.0  # Phase 1: max wait for first real content to arrive
_STREAMING_TIMEOUT_S = 120.0  # Phase 2: max wait for text stability and done signals

# How long to wait on an empty primary selector before activating DOM-scan fallback.
_FALLBACK_TRIGGER_S = 5.0

# How long to wait (s) for the submit button to go disabled after submission.
# Fast responses may skip disabled state entirely — that's fine, we fall through.
# 10s (was 5s): Grok's reasoning model ("Expert" mode) can take 5–10s of server-side
# prep before starting to stream, so Regenerate may not disappear within 5s.
_BUTTON_DISABLE_WAIT_S = 10.0

# Stability ticks required after button re-enables before we return.
# 2 ticks × POLL_INTERVAL_MS (200ms default) = 400ms — enough to catch a final
# streaming burst without waiting a full STABLE_THRESHOLD_MS cycle.
# NOTE: this constant is used as the fallback when the done signal never cycles.
# When the done signal cycles (went absent → reappeared), only 1 tick is required
# — the cycle is a hard binary signal; extra stability is redundant.
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

    Uses structured phase-based timeouts:
      - Phase 0 (initial): sleep(0.5) to let page start processing
      - Phase 1 (arrival): wait up to TEXT_ARRIVAL_TIMEOUT_S for first real content
      - Phase 2 (streaming): wait up to STREAMING_TIMEOUT_S for text stability

    Args:
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
    input_sel = chat_input_sel or sel.chat_input

    # Phase 0: Give the page a moment to start generating before we start polling.
    log.debug("Phase 0: Initial page processing (sleep 0.5s)")
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
            stable_ticks_needed=stable_ticks_needed,
        )

    # ---- Default: time-based stability path with phase gates ----
    last_text = ""
    stable_count = 0
    elapsed = 0.0
    using_fallback = False
    phase1_complete = False  # True once we've seen first real content

    # --- Phase 1: Wait for first real (non-placeholder) content to arrive ---
    log.debug("Phase 1 (TEXT_ARRIVAL): waiting up to %.0fs for first real content", _TEXT_ARRIVAL_TIMEOUT_S)
    while elapsed < _TEXT_ARRIVAL_TIMEOUT_S:
        await asyncio.sleep(poll)
        elapsed += poll

        if not using_fallback:
            current = await scraper.get_last_ai_message_text(page, sel)
            # Primary selector still empty after FALLBACK_TRIGGER_S — activate fallback.
            if fallback_detection and not current and elapsed >= _FALLBACK_TRIGGER_S:
                log.warning(
                    "Phase 1: Primary selector empty after %.0fs — activating DOM-scan fallback",
                    elapsed,
                )
                using_fallback = True
                continue
        else:
            current = await _dom_scan_last_message(page, input_sel)

        # Check if we have real content (not placeholder, not init_text)
        if _is_complete(current, extra_placeholders) and current != init_text:
            log.debug(
                "Phase 1: First real content arrived at %.1fs (len=%d)",
                elapsed,
                len(current),
            )
            last_text = current
            phase1_complete = True
            break
        else:
            last_text = current

    if not phase1_complete:
        log.warning(
            "Phase 1: Timeout after %.0fs with no real content — proceeding to Phase 2",
            _TEXT_ARRIVAL_TIMEOUT_S,
        )

    # --- Phase 2: Wait for text stability and final completion ---
    log.debug("Phase 2 (STREAMING): waiting up to %.0fs for text stability", _STREAMING_TIMEOUT_S)
    stable_count = 0
    phase2_start = elapsed

    while elapsed - phase2_start < _STREAMING_TIMEOUT_S:
        await asyncio.sleep(poll)
        elapsed += poll

        if not using_fallback:
            current = await scraper.get_last_ai_message_text(page, sel)
            # Primary selector still empty after FALLBACK_TRIGGER_S — activate fallback.
            if fallback_detection and not current and elapsed >= _FALLBACK_TRIGGER_S:
                log.warning(
                    "Phase 2: Primary selector empty after %.0fs — activating DOM-scan fallback",
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
                log.info(
                    "Phase 2: Response complete after %.1fs (stable_ticks=%d, len=%d, fallback=%s)",
                    elapsed,
                    stable_count,
                    len(last_text),
                    using_fallback,
                )
                break
            # Spinner still visible — keep waiting
            stable_count = 0

    phase2_elapsed = elapsed - phase2_start
    if phase2_elapsed >= _STREAMING_TIMEOUT_S:
        log.warning(
            "Phase 2: Timeout after %.0fs (total elapsed=%.1fs, fallback=%s, len=%d)",
            _STREAMING_TIMEOUT_S,
            elapsed,
            using_fallback,
            len(last_text),
        )

    return last_text


async def _is_done_signal(page: Page, sel: SiteSelectors) -> bool:
    """Return True when the site's chosen completion signal fires.

    Priority order:
    1. done_indicator visible — element that appears ONLY after generation completes
       (e.g. button[aria-label="Regenerate"] on x.com/i/grok).  Most reliable when
       the send button doesn't stay in the DOM across idle/generating/done states.
    2. submit_button_enabled — fallback for sites where the send button persists and
       is disabled during generation (e.g. most ChatGPT-style interfaces).
    """
    if sel.done_indicator:
        try:
            return await page.locator(sel.done_indicator).first.is_visible()
        except Exception:
            pass
    return await scraper.is_submit_button_enabled(page, sel)


async def _wait_button_signal(
    page: Page,
    sel: SiteSelectors,
    extra_placeholders: frozenset[str],
    init_text: str,
    fallback_detection: bool,
    input_sel: str,
    poll: float,
    stable_ticks_needed: int = _BUTTON_STABLE_TICKS,
) -> str:
    """Button/indicator-signal completion path with structured phase timeouts.

    Phase 1 (up to _BUTTON_DISABLE_WAIT_S): wait for the done signal to be absent,
    confirming generation has started.  Some fast responses skip this state — fine,
    we fall through to Phase 2 immediately.

    Phase 2 (up to _STREAMING_TIMEOUT_S): poll until the done signal fires AND the text
    has changed from init_text AND _is_complete() passes AND enough stable ticks.

    Required stable ticks (adaptive):
    - done signal CYCLED (went absent in Phase 1 or Phase 2, then reappeared):
      1 tick — the cycle is a hard binary signal; extra stability is redundant.
    - done signal NEVER cycled (pre-existing from a prior answer, skip_new_chat=true):
      stable_ticks_needed (time-based fallback) — text stability is the primary gate.

    Why: skip_new_chat=true keeps us on the same conversation page. The done_indicator
    (e.g. Regenerate button) is pre-existing from the previous answer and starts
    visible.  When Grok starts a new generation it removes the button; when done it
    reappears.  If the button never cycled within _BUTTON_DISABLE_WAIT_S (slow-start
    reasoning model), Phase 1 falls through and Phase 2 sees done=True from tick 1 —
    so the only reliable gate is text stability.  When the button DID cycle, trusting
    it immediately (1 tick) avoids a timeout on long streaming responses where
    stable_count never reaches threshold because text keeps changing throughout.

    Done signal priority: done_indicator visible > submit_button_enabled.
    x.com/i/grok uses done_indicator=button[aria-label="Regenerate"] because the
    send button (button[aria-label="Grok something"]) is absent from the DOM when idle
    — watching it is useless since it's never found and always returns False.
    """
    elapsed = 0.0
    using_fallback = False
    last_text = ""
    stable_count = 0
    done_signal_cycled = False  # True once done signal is observed absent then present

    # --- Phase 1: detect generation start (done signal should be absent) ---
    log.debug("Phase 1 (BUTTON_DISABLE): waiting up to %.0fs for done signal to go absent", _BUTTON_DISABLE_WAIT_S)
    phase1_elapsed = 0.0
    while phase1_elapsed < _BUTTON_DISABLE_WAIT_S:
        await asyncio.sleep(poll)
        phase1_elapsed += poll
        if not await _is_done_signal(page, sel):
            log.debug(
                "Phase 1: Done signal absent after %.1fs — generation confirmed started",
                phase1_elapsed,
            )
            done_signal_cycled = True  # went absent; will become present again when done
            break
    else:
        log.debug(
            "Phase 1: Done signal never went absent in %.0fs — slow start or pre-existing signal; "
            "will track cycle in Phase 2",
            _BUTTON_DISABLE_WAIT_S,
        )
    elapsed += phase1_elapsed

    # --- Phase 2: wait for generation end (done signal fires) ---
    log.debug("Phase 2 (STREAMING): waiting up to %.0fs for done signal to fire", _STREAMING_TIMEOUT_S)
    done_was_absent = done_signal_cycled  # already observed absent in Phase 1?
    phase2_start = elapsed

    while elapsed - phase2_start < _STREAMING_TIMEOUT_S:
        await asyncio.sleep(poll)
        elapsed += poll

        # Read text (with fallback support)
        if not using_fallback:
            current = await scraper.get_last_ai_message_text(page, sel)
            if fallback_detection and not current and elapsed >= _FALLBACK_TRIGGER_S:
                log.warning(
                    "Phase 2: Primary selector empty after %.0fs — activating DOM-scan fallback",
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

        done = await _is_done_signal(page, sel)

        # Track cycle: absent → present = genuine new completion signal
        if not done:
            done_was_absent = True
        elif done_was_absent and not done_signal_cycled:
            done_signal_cycled = True
            log.debug(
                "Phase 2: Done signal cycled (absent → present) at %.1fs — trusting as genuine completion",
                elapsed,
            )

        # Required ticks: 1 when signal is a genuine cycle; text-stability fallback otherwise.
        required_ticks = 1 if done_signal_cycled else stable_ticks_needed

        if (
            done
            and _is_complete(last_text, extra_placeholders)
            and last_text != init_text
            and stable_count >= required_ticks
        ):
            log.info(
                "Phase 2: Done signal fired (cycled=%s) + %d stable tick(s) — response complete (len=%d, elapsed=%.1fs)",
                done_signal_cycled,
                stable_count,
                len(last_text),
                elapsed,
            )
            break

    phase2_elapsed = elapsed - phase2_start
    if phase2_elapsed >= _STREAMING_TIMEOUT_S:
        log.warning(
            "Phase 2: Timeout after %.0fs (total elapsed=%.1fs, cycled=%s, len=%d)",
            _STREAMING_TIMEOUT_S,
            elapsed,
            done_signal_cycled,
            len(last_text),
        )

    return last_text
