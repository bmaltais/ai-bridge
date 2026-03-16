---
name: ai-bridge
description: Start, manage, and interact with ai-bridge — a Playwright-based local server that exposes use.ai, Grok, Perplexity, ChatGPT, and other web LLM interfaces as an Anthropic-compatible API (`/v1/messages`). Use this skill when the user says "start ai-bridge", "start the proxy", "run the proxy", "proxy is down", "connect claude to use.ai", "point claude code to use.ai", "switch model provider", "restart the proxy", "check proxy status", or "/proxy".
---

# ai-bridge

A local Playwright-based server that exposes web LLM chat interfaces through an Anthropic-compatible
API. Claude Code connects to it via `ANTHROPIC_BASE_URL`.

## Quick Start

```bash
# From this directory:
cd C:/Users/berna/.claude/skills/ai-bridge

# First time: install deps + playwright browsers
uv sync
uv run playwright install chromium

# Start the server (defaults: 127.0.0.1:8080)
uv run python -m proxy.main
```

Set Claude Code to use it (must be shell env vars, NOT just .env):

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-api03-fake-key"
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8080"
claude
```

## Session Storage (Cookies)

Browser session cookies are stored **outside the repo** at:

```
~/.claude/ai-bridge/cookies/<site>.json
```

This keeps credentials out of git entirely. The location is configurable via `COOKIES_PATH` (for
the default use-ai session) in `.env`, or per-site via the `cookies_path:` field in a site YAML.

To reset a session: delete the corresponding `~/.claude/ai-bridge/cookies/<site>.json` and restart.

## Architecture

```
proxy/
├── main.py          — FastAPI app: /v1/messages, /v1/models, /v1/proxy, /inspect, /debug
├── browser.py       — Playwright singleton session with cookie persistence + login flow
├── scraper.py       — CSS selectors and DOM actions (type, submit, read, diagnose)
├── translator.py    — Anthropic API → web LLM prompt → Anthropic response
├── streaming.py     — Response completion detection + SSE stream formatting
├── models.py        — Pydantic models for Anthropic API (request/response/SSE events)
├── config.py        — Settings via pydantic-settings (reads .env)
├── site_config.py   — YAML-driven per-site configuration loader
├── site_session.py  — One BrowserSession per site, lazy-initialized
└── sites/           — Per-site YAML configs (use-ai, x-ai/grok, perplexity, chatgpt)
```

## API Endpoints

| Endpoint | Description |
|---|---|
| `POST /v1/messages` | Anthropic Messages API — routes to use.ai (default site) |
| `GET /v1/models` | Returns Claude model IDs so Claude Code's validator accepts them |
| `POST /v1/proxy` | Generic proxy: `{"site": "perplexity", "prompt": "hello", "model": "claude-sonnet"}` |
| `GET /inspect/{site}` | Auto-discover CSS selectors via DOM analysis + LLM |
| `GET /debug/selectors` | Diagnose selectors on the main browser session |
| `GET /debug/selectors/{site}` | Diagnose selectors for a specific site |
| `GET /debug/html/{site}` | Dump candidate response elements |
| `POST /session/notify-login/{site}` | Signal login complete after manual auth |
| `GET /session/status/{site}` | Check session state and login-pending flag |
| `GET /health` | Liveness check |

## Authentication

- **First run**: opens headed Chromium, user logs in manually, cookies saved to `~/.claude/ai-bridge/cookies/<site>.json`
- **Subsequent runs**: headless, loads saved session
- **Session expired**: delete the cookie file and restart
- **Force headed**: set `HEADLESS=false` in `.env`

Login flow for `/v1/proxy`:
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
5. GET `/inspect/<name>` — auto-generate selector config
6. GET `/inspect/<name>?write=true` — save suggestions to the YAML file
7. GET `/debug/selectors/<name>` — verify selectors match live elements

## Key Design Decisions

- **System prompt dropped**: `translator.collapse_messages()` strips Claude Code's internal system
  prompt — it caused use.ai to misinterpret requests as tasks to execute.
- **No real streaming**: use.ai shows "Thinking..." placeholder that breaks delta slicing.
  `iter_response_deltas()` waits for the complete response then yields it as one chunk.
- **Single-turn optimization**: if only one user message, `[User]:` prefix is omitted.
- **Stateless**: every API request starts a new chat (page reload fallback if new-chat button missing).
- **Browser lock**: serializes all requests — no concurrency.

## Debugging

```bash
# Check what selectors are found on the live page
curl http://127.0.0.1:8080/debug/selectors
curl http://127.0.0.1:8080/debug/selectors/perplexity

# Test a specific site
curl -X POST http://127.0.0.1:8080/v1/proxy \
  -H "Content-Type: application/json" \
  -d '{"site": "perplexity", "prompt": "What is 2+2?"}'
```

## Headless vs Headed

Perplexity and ChatGPT trigger Cloudflare bot detection in headless mode. Set `HEADLESS=false`
in `.env` to run with a visible browser — this bypasses the challenge. For always-headless use,
a stealth Playwright plugin (e.g. `playwright-stealth`) can be added.
