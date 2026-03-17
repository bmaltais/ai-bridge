---
name: ai-bridge
description: Start, manage, and interact with ai-bridge — a Playwright-based local server that exposes Grok, Perplexity, ChatGPT, OpenRouter, and other LLM interfaces via a simple HTTP API. Use this skill when the user says "start ai-bridge", "start the proxy", "run the proxy", "proxy is down", "query perplexity", "query grok", "query openrouter", "use a web LLM", "use openrouter", "restart the proxy", "check proxy status", or "/proxy".
---

# ai-bridge

A local server that exposes LLM interfaces through a simple HTTP API.
Skills and agents call `POST /v1/proxy` to send prompts to any configured site.
Most sites use browser sessions (cookies) for auth. **OpenRouter** is API-based — no browser needed, just an API key.

## Quick Start

```bash
# From this directory:
cd C:/Users/berna/.claude/skills/ai-bridge

# First time: install deps + browsers
uv sync
uv run playwright install chromium   # headed mode
uv run patchright install chromium   # headless/stealth mode (Cloudflare bypass)

# Start the server (defaults: 127.0.0.1:8080)
# Pass WATCHDOG_PID=<VS Code PID> so the proxy exits when VS Code closes.
# On Windows, get the VS Code PID first:
#   powershell -Command "Get-Process Code | Sort-Object Id | Select-Object -First 1 -ExpandProperty Id"
# Then start the proxy:
WATCHDOG_PID=<vscode_pid> nohup uv run python -m proxy.main > ~/.claude/ai-bridge/proxy.log 2>&1 &
```

## Session Storage (Cookies)

Browser session cookies are stored **outside the repo** at:

```
~/.claude/ai-bridge/cookies/<site>.json
```

This keeps credentials out of git entirely. The location is configurable per-site via
the `cookies_path:` field in a site YAML.

To reset a session: delete the corresponding `~/.claude/ai-bridge/cookies/<site>.json` and restart.

## Architecture

```
proxy/
├── main.py          — FastAPI app: /v1/proxy, /inspect, /debug, /session endpoints
├── browser.py       — Playwright session with cookie persistence + login flow
├── scraper.py       — CSS selectors and DOM actions (type, submit, read, diagnose)
├── streaming.py     — Response completion detection (polls until stable)
├── config.py        — Settings via pydantic-settings (reads .env)
├── site_config.py   — YAML-driven per-site configuration loader
├── site_session.py  — One BrowserSession per site, lazy-initialized
└── sites/           — Per-site YAML configs (x-ai/grok, perplexity, chatgpt, ...)
```

## API Endpoints

| Endpoint | Description |
|---|---|
| `POST /v1/proxy` | Send a prompt to a site: `{"site": "perplexity", "prompt": "hello", "model": "claude-sonnet"}` |
| `GET /inspect/{site}` | Return DOM snapshot + heuristic selector suggestions for Claude to analyze |
| `GET /debug/selectors/{site}` | Diagnose selectors for a specific site |
| `GET /debug/html/{site}` | Dump candidate response elements |
| `GET /debug/find-text/{site}` | Find elements with substantial text |
| `POST /debug/eval/{site}` | Run arbitrary JS on a site's browser page |
| `POST /debug/dismiss-modal/{site}` | Dismiss overlays/modals |
| `POST /debug/save-cookies/{site}` | Persist current session cookies to disk |
| `POST /session/notify-login/{site}` | Signal login complete after manual auth |
| `GET /session/status/{site}` | Check session state and login-pending flag |
| `GET /health` | Liveness check |

## Authentication

- **First run**: opens headed Chromium, user logs in manually, cookies saved to `~/.claude/ai-bridge/cookies/<site>.json`
- **Subsequent runs**: headless, loads saved session
- **Session expired**: delete the cookie file and restart
- **Force headed**: set `HEADLESS=false` in `.env`

Login flow:
1. POST `/v1/proxy {"site": "perplexity", "prompt": "test"}` — browser opens at site
2. Log in manually in the browser window
3. POST `/session/notify-login/perplexity` — signals login done, switches to headless

## Configuration (.env)

Copy `.env.example` to `.env` and adjust:

```ini
HOST=127.0.0.1
PORT=8080
HEADLESS=true

# Response detection tuning
POLL_INTERVAL_MS=200
STABLE_THRESHOLD_MS=1500
RESPONSE_TIMEOUT_S=120
```

## Adding a New Site

1. Copy `proxy/sites/_template.yaml` → `proxy/sites/<site-name>.yaml`
2. Fill in `name` and `url`
3. Start server with `HEADLESS=false`
4. POST `/v1/proxy {"site": "<name>", "prompt": "hello"}` — browser opens, log in
5. GET `/inspect/<name>` — returns DOM snapshot + heuristic selector suggestions; ask Claude to analyze and suggest refined selectors
6. GET `/inspect/<name>?write=true` — save heuristic suggestions to the YAML file
7. GET `/debug/selectors/<name>` — verify selectors match live elements

## Sending Prompts

Prefer `proxy/send.py` over `curl` — single-quoted JSON fails on Windows bash but Python handles encoding correctly on all platforms:

```bash
# Basic usage
uv run python proxy/send.py grok "Write a short report on Claude Code"

# With model override
uv run python proxy/send.py perplexity "What is 2+2?" --model sonar

# Against a non-default port
uv run python proxy/send.py chatgpt "Hello" --port 9090
```

`curl` equivalent (only use if you escape the JSON manually):
```bash
curl -X POST http://127.0.0.1:8080/v1/proxy \
  -H "Content-Type: application/json" \
  -d "{\"site\": \"perplexity\", \"prompt\": \"What is 2+2?\"}"
```

## OpenRouter (API Provider — No Browser)

OpenRouter is configured as an **API provider** (`provider_type: api` in its YAML). No browser or cookies needed — it calls the OpenRouter REST API directly.

### Setup

Add your API key to `.env`:

```ini
OPENROUTER_API_KEY=sk-or-v1-...
```

Get a free key at https://openrouter.ai/keys.

### Usage

```bash
# Default: openrouter/free (no credits required — OpenRouter picks a free model)
uv run python proxy/send.py openrouter "Your prompt here"

# Specific free model
uv run python proxy/send.py openrouter "Your prompt" --model llama-70b

# Other free model aliases
uv run python proxy/send.py openrouter "Your prompt" --model deepseek-r1
uv run python proxy/send.py openrouter "Your prompt" --model gemma-27b
```

### Available model aliases (`proxy/sites/openrouter.yaml`)

| Alias | Model |
|---|---|
| `free` | `openrouter/free` (default — auto-selected free model) |
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

You can also pass any raw OpenRouter model ID directly: `--model "mistralai/mistral-7b-instruct"`.

### Notes

- **Server restart required** when `openrouter.yaml` is added or modified — site configs are loaded at request time but the server process must be restarted to pick up newly added YAML files.
- Free models may have rate limits (429). Retry after a few seconds or switch model aliases.
- `openrouter/auto` routes to the best paid model — requires account credits.

## Debugging

```bash
# Check what selectors are found on the live page
curl http://127.0.0.1:8080/debug/selectors/perplexity

# Get DOM snapshot for Claude to analyze
curl http://127.0.0.1:8080/inspect/perplexity

# Test a specific site (use send.py on Windows — see Sending Prompts above)
uv run python proxy/send.py perplexity "What is 2+2?"
```

## Headless vs Headed

Perplexity and ChatGPT trigger Cloudflare bot detection in headless mode. Set `HEADLESS=false`
in `.env` to run with a visible browser — this bypasses the challenge. For always-headless use,
`patchright` (a stealth Chromium fork) is already integrated — it patches `navigator.webdriver`,
TLS fingerprint, and canvas fingerprint to bypass bot detection.
