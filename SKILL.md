---
name: ai-bridge
description: Start, manage, and interact with ai-bridge — a Playwright-based local server that exposes Grok, Perplexity, ChatGPT, OpenRouter, and other LLM interfaces via a simple HTTP API. Use this skill when the user says "start ai-bridge", "start the proxy", "run the proxy", "proxy is down", "query perplexity", "query grok", "query openrouter", "use a web LLM", "use openrouter", "restart the proxy", "check proxy status", or "/proxy".
---

# ai-bridge

```yaml
config:
  role: llm_proxy_server                          # local HTTP gateway to web LLMs
  capabilities: [send_prompt, manage_sessions, inspect_dom, debug_selectors]
  providers:
    browser: [x-grok, perplexity, chatgpt]        # Playwright + cookie auth
    api: [openrouter]                              # REST API, no browser
  cycle: [ensure_running, send_prompt, detect_response, return_text]
```

## Quick Start

```
FUNC quickstart():
  cd ~/.claude/skills/ai-bridge
  uv sync                                         # first time only
  uv run playwright install chromium              # headed mode
  uv run patchright install chromium              # headless/stealth (Cloudflare bypass)

FUNC send(site: str, prompt: str, model: str = None):
  # send.py handles lifecycle: health check → auto-start → tray monitor
  uv run python proxy/send.py {site} {prompt} [--model {model}] [--port {port}]

FUNC manual_start():                              # optional — send.py auto-starts
  uv run python -m proxy.main
```

## Session Storage

```
cookies_path = ~/.claude/ai-bridge/cookies/<site>.json   # outside repo, not in git
FUNC reset_session(site): DELETE cookies_path(site)       # re-prompts on next request
```

## API Endpoints

```
ENDPOINTS = {
  # Core
  "POST /v1/proxy":                  {site, prompt, model?},           # send prompt
  "POST /v1/control":                {site, capability: "new_chat"},   # invoke capability
  "GET  /v1/capabilities/{site}":    list_capabilities,
  "GET  /v1/health/detailed":        session_status_all,
  "GET  /v1/metrics":                request_error_latency_stats,
  "GET  /health":                    liveness_check,

  # Debug / Inspect
  "GET  /inspect/{site}":            dom_snapshot + selector_suggestions,
  "GET  /inspect/{site}?write=true": save_suggestions_to_yaml,
  "GET  /debug/selectors/{site}":    diagnose_css_selectors,
  "GET  /debug/html/{site}":         dump_candidate_elements,
  "GET  /debug/find-text/{site}":    find_substantial_text,
  "POST /debug/eval/{site}":         run_arbitrary_js,
  "POST /debug/dismiss-modal/{site}":dismiss_overlays,
  "POST /debug/save-cookies/{site}": persist_cookies_to_disk,

  # Session
  "POST /session/notify-login/{site}": signal_manual_login_done,
  "GET  /session/status/{site}":      check_login_state,
}
```

## Authentication

```
FUNC auth_flow(site: str):
  IF NOT cookies_exist(site):
    launch_headed_chromium(site.url)               # first run: manual login
    WAIT user_logs_in()
    POST /session/notify-login/{site}              # saves cookies
  ELSE:
    launch_headless(site.url, cookies=load(site))  # subsequent: auto-session
  ON session_expired: DELETE cookies(site)          # re-prompts next request
```

## Configuration (.env)

```
ENV = {
  HOST: "127.0.0.1",  PORT: 8080,  HEADLESS: true,
  OPENROUTER_API_KEY: "sk-or-v1-...",
  POLL_INTERVAL_MS: 200,  STABLE_THRESHOLD_MS: 1500,  RESPONSE_TIMEOUT_S: 120
}
```

## Adding a New Site

```
FUNC add_site(name: str):
  COPY proxy/sites/_template.yaml -> proxy/sites/{name}.yaml
  EDIT yaml: set name, url
  ENV HEADLESS=false                               # first run needs headed
  send.py {name} "hello"                           # opens browser, log in
  GET /inspect/{name}                              # DOM snapshot → ask Claude for selectors
  GET /inspect/{name}?write=true                   # save heuristic suggestions to YAML
  GET /debug/selectors/{name}                      # verify selectors match
```

## Reference Loading

| Task | Load |
|---|---|
| File layout, startup optimizations, tray monitor, headless/headed | references/architecture.md |
| OpenRouter setup, model aliases, rate limits | references/openrouter.md |
