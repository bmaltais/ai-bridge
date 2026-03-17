"""
OpenRouter API adapter.

Sends prompts directly to the OpenRouter REST API (OpenAI-compatible).
No browser or Playwright involved — authentication is via API key only.

Requires OPENROUTER_API_KEY in .env (get one at https://openrouter.ai/keys).
"""

import logging

import httpx

from proxy.config import settings

log = logging.getLogger(__name__)

_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_MODEL = "openrouter/free"


async def query(model: str, prompt: str) -> str:
    """Send *prompt* to OpenRouter and return the response text.

    Args:
        model: OpenRouter model ID (e.g. "meta-llama/llama-3.3-70b-instruct")
               or "openrouter/auto" to let OpenRouter pick the best available model.
        prompt: The user message to send.

    Raises:
        RuntimeError: If OPENROUTER_API_KEY is not set.
        httpx.HTTPStatusError: On 4xx/5xx from OpenRouter.
    """
    if not settings.openrouter_api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. "
            "Add it to your .env file (get a key at https://openrouter.ai/keys)."
        )

    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "HTTP-Referer": "https://ai-bridge.local",
        "X-Title": "ai-bridge",
    }
    payload = {
        "model": model or _DEFAULT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
    }

    log.info("OpenRouter request: model=%s prompt_len=%d", model, len(prompt))
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{_BASE_URL}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()

    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    log.info("OpenRouter response: len=%d", len(text))
    return text
