# ai-bridge

A local server that exposes LLM interfaces through a simple HTTP API.
Call `POST /v1/proxy` to send prompts to any configured site.
Most sites use browser sessions (cookies) for auth. **OpenRouter** is API-based — no browser needed, just an API key.

## Quick Start

```bash
cd C:/Users/berna/.claude/skills/ai-bridge

# First time: install deps + browsers
uv sync
uv run playwright install chromium   # headed mode
uv run patchright install chromium   # headless/stealth mode (Cloudflare bypass)

# Send a prompt — bridge auto-starts if not running
uv run python proxy/send.py x-grok "Hello from ai-bridge"
```

`send.py` checks bridge health, starts it automatically if needed (hidden console, no black CMD window), and launches a taskbar tray monitor. No manual server management required.

### Manual Start (optional)

If you prefer to start the bridge separately:

```bash
uv run python -m proxy.main
```

## Sending Prompts

Use `proxy/send.py` — handles bridge lifecycle and is more reliable than curl on Windows:

```bash
uv run python proxy/send.py x-grok "Write a short report on Claude Code"
uv run python proxy/send.py perplexity "What is 2+2?" --model sonar
uv run python proxy/send.py openrouter "Explain async/await in Python"
uv run python proxy/send.py openrouter "Explain async/await" --model llama-70b
```

### What send.py does

1. Checks if the bridge is running (`GET /health`)
2. If not, starts it in the background (hidden console window)
3. Launches the tray monitor (minimized to taskbar)
4. Sends the prompt via `POST /v1/proxy`
5. Prints the response to stdout, chat URL to stderr

## Tray Monitor

A lightweight tkinter GUI that starts minimized to the taskbar when the bridge launches:

- Green/red status indicator (polls `/health` every 5s)
- Uptime counter
- "Stop ai-bridge" button (reads PID file, terminates process)
- Auto-closes 3s after bridge stops

Click the taskbar icon to open it when needed.

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

- Free models may rate-limit (429). Retry after a few seconds or try another alias.
- `openrouter/auto` requires account credits; all `:free` models are cost-free.

## Architecture

```
proxy/
├── main.py          — FastAPI app: /v1/proxy, /inspect, /debug, /session, /v1/control endpoints
├── browser.py       — Playwright session with cookie persistence + login flow
├── scraper.py       — CSS selectors and DOM actions (type, submit, read, diagnose)
├── streaming.py     — Response completion detection (polls until stable)
├── config.py        — Settings via pydantic-settings (reads .env)
├── site_config.py   — YAML-driven per-site configuration loader
├── site_session.py  — One BrowserSession per site, lazy-initialized
├── send.py          — CLI client: auto-starts bridge, sends prompts, prints responses
├── tray.py          — Taskbar tray monitor (tkinter GUI: status, uptime, stop button)
├── ensure.py        — Bridge lifecycle helper (used by send.py and external callers)
├── lifecycle.py     — PID file, port conflict resolution, watchdog thread
├── inspector.py     — DOM snapshot + heuristic selector discovery
├── adapters/
│   └── openrouter.py — Direct REST API adapter (no browser)
└── sites/           — Per-site YAML configs (x-grok, perplexity, chatgpt, openrouter, ...)
```

### Startup Optimizations

The bridge is tuned for fast cold starts:

- **15 Chromium launch args** disable unused subsystems (GPU, sync, translate, phishing detection, etc.) — saves ~300-800ms. Patchright-incompatible flags (`--disable-extensions`, `--disable-default-apps`) are intentionally excluded.
- **Lazy Playwright imports** in scraper.py, streaming.py, site_session.py — defers ~100-300ms of import cost until first request.
- **Explicit uvicorn settings** (`lifespan=on`, `interface=asgi3`, `ws=none`) — skips auto-detection overhead.
- **Skip redundant navigation** — `new_chat` capability detects if the chat input is already present and avoids reloading the page.

## API Endpoints

| Endpoint | Description |
|---|---|
| `POST /v1/proxy` | Send prompt: `{"site": "openrouter", "prompt": "hello", "model": "llama-70b"}` |
| `POST /v1/control` | Invoke a capability: `{"site": "x-grok", "capability": "new_chat"}` |
| `GET /v1/capabilities/{site}` | List available capabilities for a site |
| `GET /v1/health/detailed` | Session status for all initialized sites |
| `GET /v1/metrics` | Request/error/latency metrics for all sites |
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
- **Session expired**: delete the cookie file — bridge will re-prompt on next request
- **Force headed**: set `HEADLESS=false` in `.env`

Login flow:
1. `uv run python proxy/send.py perplexity "test"` — bridge starts, browser opens
2. Log in manually
3. `POST /session/notify-login/perplexity` — signals login done, cookies saved

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
4. `uv run python proxy/send.py <name> "hello"` — browser opens, log in
5. `GET /inspect/<name>` — returns DOM snapshot + selector suggestions
6. `GET /inspect/<name>?write=true` — save suggestions to YAML
7. `GET /debug/selectors/<name>` — verify selectors

## Headless vs Headed

Perplexity and ChatGPT trigger Cloudflare bot detection in headless mode. Set `HEADLESS=false`
in `.env` to run with a visible browser. For always-headless use, `patchright` (stealth Chromium fork)
patches `navigator.webdriver`, TLS fingerprint, and canvas fingerprint to bypass bot detection.
