"""
Bridge between the Anthropic Messages API and the use.ai browser session.

Each request:
  1. Collapses the full message history into a single prompt string.
  2. Navigates to a fresh use.ai chat (stateless mode).
  3. Types and submits the prompt.
  4. Waits for / streams the response.
  5. Returns or yields Anthropic-format data.
"""

import logging
from typing import AsyncIterator

from proxy import scraper, streaming
from proxy.browser import browser_session
from proxy.models import MessagesRequest, MessagesResponse

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Message collapsing
# ---------------------------------------------------------------------------


def collapse_messages(request: MessagesRequest) -> str:
    """
    Flatten the Anthropic messages list into a single plain-text prompt.

    The system prompt is intentionally dropped: it contains Claude Code's
    internal scaffolding instructions which use.ai would misinterpret as
    user tasks. Only the actual conversation turns are forwarded.

    For multi-turn conversations the full history is included so use.ai has
    context. For single-turn requests (the common case) only the user message
    is sent.
    """
    parts: list[str] = []

    for msg in request.messages:
        prefix = "[User]" if msg.role == "user" else "[Assistant]"
        parts.append(f"{prefix}: {msg.text()}")

    # Single user turn: skip the prefix for cleaner output
    if len(parts) == 1 and request.messages[0].role == "user":
        return request.messages[0].text()

    return "\n\n".join(parts)


def estimate_input_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Non-streaming completion
# ---------------------------------------------------------------------------


async def complete(request: MessagesRequest) -> MessagesResponse:
    prompt = collapse_messages(request)
    input_tokens = estimate_input_tokens(prompt)

    async with browser_session.lock:
        page = browser_session.page
        await scraper.start_new_chat(page)
        await scraper.type_message(page, prompt)
        await scraper.submit_message(page)
        response_text = await streaming.wait_for_complete_response(page)

    return MessagesResponse.from_text(response_text, request.model, input_tokens)


# ---------------------------------------------------------------------------
# Streaming completion
# ---------------------------------------------------------------------------


async def stream(request: MessagesRequest) -> AsyncIterator[str]:
    """
    Yield raw SSE strings (event + data lines) in Anthropic streaming format.

    The browser lock is held for the entire duration of the stream.
    Concurrent requests will queue behind it.
    """
    prompt = collapse_messages(request)
    input_tokens = estimate_input_tokens(prompt)

    # We need to hold the lock across the whole streaming operation.
    # Using an async generator means we can't use `async with` directly,
    # so we acquire/release manually.
    await browser_session.lock.acquire()
    try:
        page = browser_session.page
        await scraper.start_new_chat(page)
        await scraper.type_message(page, prompt)
        await scraper.submit_message(page)

        delta_iter = streaming.iter_response_deltas(page)
        async for sse_line in streaming.anthropic_sse_stream(
            delta_iter, request.model, input_tokens
        ):
            yield sse_line
    finally:
        browser_session.lock.release()
