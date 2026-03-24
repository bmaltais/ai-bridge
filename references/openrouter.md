---
name: openrouter
description: OpenRouter API provider setup, model aliases, usage, and rate limit notes
use_when: configuring OpenRouter; choosing a model alias; troubleshooting 429 errors
links: [architecture.md]
---

# OpenRouter (API Provider — No Browser)

API-based provider (`provider_type: api` in YAML). No browser or cookies needed.

## Setup

Add to `.env`:
```ini
OPENROUTER_API_KEY=sk-or-v1-...
```
Free key: https://openrouter.ai/keys

## Usage

```bash
# Default: openrouter/free (auto-selected free model)
uv run python proxy/send.py openrouter "Your prompt here"

# Specific free model
uv run python proxy/send.py openrouter "Your prompt" --model llama-70b
```

## Model Aliases (`proxy/sites/openrouter.yaml`)

| Alias | Model |
|---|---|
| `free` | `openrouter/free` (default) |
| `llama-70b` | `meta-llama/llama-3.3-70b-instruct:free` |
| `llama-8b` | `meta-llama/llama-3.1-8b-instruct:free` |
| `gemma-27b` | `google/gemma-3-27b-it:free` |
| `gemma-12b` | `google/gemma-3-12b-it:free` |
| `qwen-72b` | `qwen/qwen-2.5-72b-instruct:free` |
| `mistral` | `mistralai/mistral-small-3.1-24b-instruct:free` |
| `deepseek-r1` | `deepseek/deepseek-r1:free` |
| `deepseek-v3` | `deepseek/deepseek-chat-v3-0324:free` |
| `phi-4` | `microsoft/phi-4:free` |
| `auto` | `openrouter/auto` (paid — requires credits) |

Raw model IDs also work: `--model "mistralai/mistral-7b-instruct"`

## Notes

- Free models may return 429. Retry after a few seconds or switch alias.
- `openrouter/auto` routes to best paid model — requires credits.
