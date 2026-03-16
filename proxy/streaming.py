"""
Response completion detection and SSE streaming helpers.

Strategy: poll the last AI message element every POLL_INTERVAL_MS.
When text is stable for STABLE_THRESHOLD_MS and the thinking spinner is gone,
we consider the response complete.
"""

import asyncio
import json
import logging
import uuid
from typing import AsyncIterator

from playwright.async_api import Page

from proxy import scraper
from proxy.config import settings
from proxy.scraper import SiteSelectors
from proxy.models import (
    ContentBlock,
    ContentBlockDeltaEvent,
    ContentBlockStartEvent,
    ContentBlockStopEvent,
    MessageDeltaEvent,
    MessageDeltaUsage,
    MessageStartEvent,
    MessageStartMessage,
    MessageStopEvent,
    Usage,
)

log = logging.getLogger(__name__)

# Built-in placeholder texts that indicate the AI is still generating.
# Sites can add their own via SiteConfig.placeholders.
_BUILTIN_PLACEHOLDERS = frozenset({"Thinking...", "Thinking", "...", ""})


def _is_complete(text: str, extra_placeholders: frozenset[str] = frozenset()) -> bool:
    """Return True if `text` looks like a real completed response."""
    t = text.strip()
    if t in _BUILTIN_PLACEHOLDERS or t in extra_placeholders:
        return False
    # use.ai prefixes each message with the model name, e.g. "Using OpenAI GPT-5.1"
    # If the entire text is just that badge line (short, starts with "Using "),
    # the actual response content hasn't arrived yet.
    if t.startswith("Using ") and "\n" not in t and len(t) < 50:
        return False
    return True


# ---------------------------------------------------------------------------
# Core: wait for a complete response
# ---------------------------------------------------------------------------


async def wait_for_complete_response(
    page: Page,
    sel: SiteSelectors | None = None,
    extra_placeholders: frozenset[str] = frozenset(),
) -> str:
    """
    Block until the AI has finished generating its response.
    Returns the full text of the last assistant message.

    Raises asyncio.TimeoutError if response_timeout_s is exceeded.
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

        # Stable for long enough and not a transient placeholder?
        if stable_count >= stable_ticks_needed and _is_complete(
            last_text, extra_placeholders
        ):
            # Double-check spinner is gone
            if not await scraper.is_thinking(page, sel):
                break
            # Spinner still visible — keep waiting
            stable_count = 0

    if elapsed >= timeout:
        log.warning("Response timed out after %ds", timeout)

    return last_text


# ---------------------------------------------------------------------------
# Core: stream response deltas
# ---------------------------------------------------------------------------


async def iter_response_deltas(
    page: Page,
    sel: SiteSelectors | None = None,
    extra_placeholders: frozenset[str] = frozenset(),
) -> AsyncIterator[str]:
    """
    Yield the complete response as a single chunk once it is stable.
    """
    # Wait for the complete response, then yield it as one chunk.
    # Real delta streaming caused issues with use.ai's "Thinking..." placeholder:
    # when the placeholder is replaced by the actual response, the delta slice
    # current[len(last_text):] loses the response prefix.
    full_text = await wait_for_complete_response(page, sel, extra_placeholders)
    if full_text:
        yield full_text


# ---------------------------------------------------------------------------
# SSE formatting helpers
# ---------------------------------------------------------------------------


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


async def anthropic_sse_stream(
    delta_iter: AsyncIterator[str],
    model: str,
    input_tokens: int,
) -> AsyncIterator[str]:
    """
    Wrap raw text deltas in Anthropic-format SSE events.
    """
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    # message_start
    start = MessageStartEvent(
        message=MessageStartMessage(
            id=msg_id,
            model=model,
            usage=Usage(input_tokens=input_tokens, output_tokens=0),
        )
    )
    yield _sse("message_start", start.model_dump_json())

    # content_block_start
    yield _sse("content_block_start", ContentBlockStartEvent().model_dump_json())

    # ping (Anthropic sends one early)
    yield _sse("ping", '{"type":"ping"}')

    # content_block_delta events
    output_chars = 0
    async for delta_text in delta_iter:
        output_chars += len(delta_text)
        event = ContentBlockDeltaEvent(delta={"type": "text_delta", "text": delta_text})
        yield _sse("content_block_delta", event.model_dump_json())

    # content_block_stop
    yield _sse("content_block_stop", ContentBlockStopEvent().model_dump_json())

    # message_delta
    output_tokens = max(1, output_chars // 4)
    delta_event = MessageDeltaEvent(
        usage=MessageDeltaUsage(output_tokens=output_tokens)
    )
    yield _sse("message_delta", delta_event.model_dump_json())

    # message_stop
    yield _sse("message_stop", MessageStopEvent().model_dump_json())
