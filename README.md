# ai-bridge

A LLM skill that acr as a local server that exposes LLM interfaces through a simple HTTP API.
Call `POST /v1/proxy` to send prompts to any configured site.
Most sites use browser sessions (cookies) for auth. **OpenRouter** is API-based — no browser needed, just an API key.

## Quick Start

```bash
cd C:/Users/berna/.claude/skills/ai-bridge

# First time: install deps + browsers
uv sync
uv run playwright install chromium   # headed mode
uv run patchright install chromium   # headless/stealth mode (Cloudflare bypass)

# Start the server (defaults: 127.0.0.1:8080)
WATCHDOG_PID=<vscode_pid> nohup uv run python -m proxy.main > ~/.claude/ai-bridge/proxy.log 2>&1 &
# Get VS Code PID on Windows: powershell -Command "Get-Process Code | Sort-Object Id | Select-Object -First 1 -ExpandProperty Id"
```

## Sending Prompts

Use `proxy/send.py` — more reliable than curl on Windows:

```bash
uv run python proxy/send.py grok "Write a short report on Claude Code"
uv run python proxy/send.py perplexity "What is 2+2?" --model sonar
uv run python proxy/send.py openrouter "Explain async/await in Python"
uv run python proxy/send.py openrouter "Explain async/await" --model llama-70b
```

## OpenRouter (API Provider — No Browser)

OpenRouter routes prompts to many models via REST API. No browser, no cookies — just an API key.

### Setup

Add to `.env`:

```ini
OPENROUTER_API_KEY=sk-or-v1-...
```

Get a free key at https://openrouter.ai/keys.

### Available model aliases

| Alias | Model | Cost |
|---|---|---|
| `free` *(default)* | `openrouter/free` | Free |
| `llama-70b` | `meta-llama/llama-3.3-70b-instruct:free` | Free |
| `llama-8b` | `meta-llama/llama-3.1-8b-instruct:free` | Free |
| `gemma-27b` | `google/gemma-3-27b-it:free` | Free |
| `gemma-12b` | `google/gemma-3-12b-it:free` | Free |
| `qwen-72b` | `qwen/qwen-2.5-72b-instruct:free` | Free |
| `mistral` | `mistralai/mistral-small-3.1-24b-instruct:free` | Free |
| `deepseek-r1` | `deepseek/deepseek-r1:free` | Free |
| `deepseek-v3` | `deepseek/deepseek-chat-v3-0324:free` | Free |
| `phi-4` | `microsoft/phi-4:free` | Free |
| `auto` | `openrouter/auto` | Paid |

Pass any raw OpenRouter model ID directly: `--model "mistralai/mistral-7b-instruct"`.

### Notes

- **Server restart required** after adding/modifying YAML site configs — they're read at request time but the running process needs to reload.
- Free models may rate-limit (429). Retry after a few seconds or try another alias.
- `openrouter/auto` requires account credits; all `:free` models are cost-free.

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
├── adapters/
│   └── openrouter.py — Direct REST API adapter (no browser)
└── sites/           — Per-site YAML configs (x-ai/grok, perplexity, chatgpt, openrouter, ...)
```

## API Endpoints

| Endpoint | Description |
|---|---|
| `POST /v1/proxy` | Send prompt: `{"site": "openrouter", "prompt": "hello", "model": "llama-70b"}` |
| `GET /inspect/{site}` | DOM snapshot + heuristic selector suggestions |
| `GET /debug/selectors/{site}` | Diagnose selectors for a site |
| `GET /debug/html/{site}` | Dump candidate response elements |
| `GET /debug/find-text/{site}` | Find elements with substantial text |
| `POST /debug/eval/{site}` | Run arbitrary JS on a site's browser page |
| `POST /debug/dismiss-modal/{site}` | Dismiss overlays/modals |
| `POST /debug/save-cookies/{site}` | Persist current session cookies to disk |
| `POST /session/notify-login/{site}` | Signal login complete after manual auth |
| `GET /session/status/{site}` | Check session state |
| `GET /health` | Liveness check |

## Browser-Based Sites (Authentication)

- **First run**: opens headed Chromium, user logs in manually, cookies saved to `~/.claude/ai-bridge/cookies/<site>.json`
- **Subsequent runs**: headless, loads saved session
- **Session expired**: delete the cookie file and restart
- **Force headed**: set `HEADLESS=false` in `.env`

Login flow:
1. `POST /v1/proxy {"site": "perplexity", "prompt": "test"}` — browser opens
2. Log in manually
3. `POST /session/notify-login/perplexity` — signals login done, switches to headless

## Configuration (.env)

Copy `.env.example` to `.env`:

```ini
HOST=127.0.0.1
PORT=8080
HEADLESS=true
OPENROUTER_API_KEY=sk-or-v1-...

# Response detection tuning
POLL_INTERVAL_MS=200
STABLE_THRESHOLD_MS=1500
RESPONSE_TIMEOUT_S=120
```

## Adding a New Browser Site

1. Copy `proxy/sites/_template.yaml` → `proxy/sites/<site-name>.yaml`
2. Fill in `name` and `url`
3. Start server with `HEADLESS=false`
4. `POST /v1/proxy {"site": "<name>", "prompt": "hello"}` — browser opens, log in
5. `GET /inspect/<name>` — returns DOM snapshot + selector suggestions
6. `GET /inspect/<name>?write=true` — save suggestions to YAML
7. `GET /debug/selectors/<name>` — verify selectors

## Headless vs Headed

Perplexity and ChatGPT trigger Cloudflare bot detection in headless mode. Set `HEADLESS=false`
in `.env` to run with a visible browser. For always-headless use, `patchright` (stealth Chromium fork)
patches `navigator.webdriver`, TLS fingerprint, and canvas fingerprint to bypass bot detection.
