---
name: architecture
description: ai-bridge internal file layout, startup optimizations, and component responsibilities
use_when: debugging internals; adding a new module; understanding request flow
links: [openrouter.md]
---

# Architecture

```
proxy/
  main.py          — FastAPI: /v1/proxy, /inspect, /debug, /session, /v1/control
  browser.py       — Playwright session + cookie persistence + login flow
  scraper.py       — CSS selectors and DOM actions (type, submit, read, diagnose)
  streaming.py     — Response completion detection (polls until stable)
  config.py        — Settings via pydantic-settings (reads .env)
  site_config.py   — YAML-driven per-site config loader
  site_session.py  — One BrowserSession per site, lazy-initialized
  send.py          — CLI client: auto-starts bridge, sends prompts, prints responses
  tray.py          — Taskbar tray monitor (tkinter: status, uptime, stop button)
  ensure.py        — Bridge lifecycle helper (used by send.py + external callers)
  lifecycle.py     — PID file, port conflict resolution, watchdog thread
  inspector.py     — DOM snapshot + heuristic selector discovery
  adapters/
    openrouter.py  — Direct REST API adapter (no browser)
  sites/           — Per-site YAML configs (x-grok, perplexity, chatgpt, openrouter, ...)
```

## Startup Optimizations

- **15 Chromium launch args** disable unused subsystems (~300-800ms savings)
- **Lazy Playwright imports** in scraper/streaming/site_session (~100-300ms savings)
- **Explicit uvicorn settings** — no auto-detection overhead
- **Skip redundant navigation** — detects if chat input already present after cookie launch

## Tray Monitor

When `send.py` auto-starts the bridge, it launches a tkinter tray GUI (minimized to taskbar):
- Green/red status indicator (polls `/health` every 5s)
- Uptime counter + "Stop ai-bridge" button
- Auto-closes 3s after bridge stops

## Headless vs Headed

Perplexity and ChatGPT trigger Cloudflare bot detection in headless mode. Set `HEADLESS=false`
in `.env` to run headed. For headless use, `patchright` (stealth Chromium fork) is integrated —
patches `navigator.webdriver`, TLS fingerprint, and canvas fingerprint.
