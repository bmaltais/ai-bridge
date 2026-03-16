"""
Site configuration loader.

Each YAML file in proxy/sites/ describes one web LLM interface:
  name:         identifier used in API calls (e.g. "use-ai")
  url:          landing page to navigate to on startup
  cookies_path: where to persist the browser session (default: ~/.claude/ai-bridge/cookies/<name>.json)
  placeholders: list of transient texts to ignore during response-completion detection
  selectors:    optional CSS overrides if generic selectors fail

Selector overrides are optional — the generic selectors in scraper.py work for most sites.
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Cookies live outside the repo — never committed, never pushed.
# Default: ~/.claude/ai-bridge/cookies/<site>.json
_COOKIES_DIR = Path.home() / ".claude" / "ai-bridge" / "cookies"


@dataclass
class SiteConfig:
    name: str
    url: str
    cookies_path: Path

    # Transient placeholder texts the site shows while the AI is still generating.
    # Add site-specific ones here; proxy/streaming.py always ignores the built-in set.
    placeholders: list[str] = field(default_factory=list)

    # Optional CSS selector overrides (None → use scraper.py defaults)
    chat_input: str | None = None
    submit_button: str | None = None
    last_ai_msg: str | None = None
    thinking_spinner: str | None = None
    new_chat: str | None = None
    model_selector: str | None = None
    auth_check: str | None = None  # selector present only when logged in; falls back to chat_input

    # Aliases: alternate names that resolve to this site (e.g. ["grok"] for x-ai)
    aliases: list[str] = field(default_factory=list)

    # Friendly-name → picker-label mapping for model selection
    models: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "SiteConfig":
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        default_cookies = str(_COOKIES_DIR / f"{data['name']}.json")
        selectors = data.get("selectors", {})
        return cls(
            name=data["name"],
            url=data["url"],
            cookies_path=Path(data.get("cookies_path", default_cookies)),
            placeholders=data.get("placeholders", []),
            chat_input=selectors.get("chat_input"),
            submit_button=selectors.get("submit_button"),
            last_ai_msg=selectors.get("last_ai_msg"),
            thinking_spinner=selectors.get("thinking_spinner"),
            new_chat=selectors.get("new_chat"),
            model_selector=selectors.get("model_selector"),
            auth_check=selectors.get("auth_check"),
            aliases=data.get("aliases", []),
            models=data.get("models", {}),
        )

    @classmethod
    def find(cls, name: str, sites_dir: Path) -> "SiteConfig":
        """Find a config by site name or YAML file stem.

        Normalizes common aliases: dots become hyphens (e.g. "use.ai" → "use-ai").
        """
        if not sites_dir.exists():
            raise FileNotFoundError(f"Sites directory not found: {sites_dir}")
        # Build candidate names: original + dot-to-hyphen variant
        candidates = {name, name.replace(".", "-")}
        for f in sites_dir.glob("*.yaml"):
            try:
                cfg = cls.load(f)
                if cfg.name in candidates or f.stem in candidates or any(a in candidates for a in cfg.aliases):
                    return cfg
            except Exception:
                continue
        available = [f.stem for f in sites_dir.glob("*.yaml")]
        raise ValueError(
            f"No site config found for {name!r}. "
            f"Available: {available or ['(none)']}"
        )
