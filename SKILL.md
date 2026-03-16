---
name: ai-bridge
description: Start, manage, and interact with ai-bridge — a Playwright-based local server that exposes Grok, Perplexity, ChatGPT, and other web LLM interfaces via a simple HTTP API. Use this skill when the user says "start ai-bridge", "start the proxy", "run the proxy", "proxy is down", "query perplexity", "query grok", "use a web LLM", "restart the proxy", "check proxy status", or "/proxy".
---

# ai-bridge

A local Playwright-based server that exposes web LLM chat interfaces through a simple HTTP API.
Skills and agents call `POST /v1/proxy` to send prompts to any configured site.
No API keys required — the proxy uses browser sessions (cookies) to authenticate.

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

## Debugging

```bash
# Check what selectors are found on the live page
curl http://127.0.0.1:8080/debug/selectors/perplexity

# Get DOM snapshot for Claude to analyze
curl http://127.0.0.1:8080/inspect/perplexity

# Test a specific site
curl -X POST http://127.0.0.1:8080/v1/proxy \
  -H "Content-Type: application/json" \
  -d '{"site": "perplexity", "prompt": "What is 2+2?"}'
```

## Headless vs Headed

Perplexity and ChatGPT trigger Cloudflare bot detection in headless mode. Set `HEADLESS=false`
in `.env` to run with a visible browser — this bypasses the challenge. For always-headless use,
a stealth Playwright plugin (e.g. `playwright-stealth`) can be added.
