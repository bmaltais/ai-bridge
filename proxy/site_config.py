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
from typing import Any

import yaml

# Cookies live outside the repo — never committed, never pushed.
# Default: ~/.claude/ai-bridge/cookies/<site>.json
_COOKIES_DIR = Path.home() / ".claude" / "ai-bridge" / "cookies"


@dataclass
class CapabilityConfig:
    """One named UI control a site exposes (button, select, slider, etc.)."""

    type: str  # button | select | input | textarea | slider | toggle
    selector: str  # CSS selector for the element
    action: str  # click | fill | set_value | select_by_value | select_by_label | toggle
    description: str = ""
    options: list[dict[str, str]] = field(default_factory=list)  # [{value, label}, ...]
    range: list[float] = field(default_factory=list)  # [min, max] for sliders
    step: float | None = None
    requires_confirmation: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "CapabilityConfig":
        return cls(
            type=data["type"],
            selector=data["selector"],
            action=data["action"],
            description=data.get("description", ""),
            options=data.get("options", []),
            range=data.get("range", []),
            step=data.get("step"),
            requires_confirmation=data.get("requires_confirmation", False),
        )


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
    auth_check: str | None = (
        None  # selector present only when logged in; falls back to chat_input
    )

    # When True, skip start_new_chat entirely (avoids headless bot-detection for Cloudflare sites)
    skip_new_chat: bool = False

    # When True, fall back to a DOM scan if the primary last_ai_msg selector returns empty
    # for more than FALLBACK_TRIGGER_S seconds (see streaming.py).  Use for sites with
    # hashed/volatile CSS class names where the selector may break between deploys.
    fallback_detection: bool = False

    # Element that appears ONLY when generation is complete (e.g. Regenerate button on Grok).
    # When set, streaming.py uses its DOM visibility as the done signal — more reliable than
    # submit-button state for sites where the send button is absent when idle (e.g. x.com/i/grok,
    # where button[aria-label="Grok something"] only appears while typing, never when idle).
    done_indicator: str | None = None

    # Primary completion signal. Options:
    #   None (default) — time-based: text stable for stable_threshold_ms + spinner gone
    #   "submit_button_enabled" — button-based: wait for submit button to re-enable after
    #       being disabled/absent during generation. More reliable for sites that toggle the
    #       send button (e.g. x.com/i/grok) because it's a direct DOM state signal, not a
    #       timing heuristic, so mid-response pauses never trigger false completion.
    completion_signal: str | None = None

    # Per-site override for stable_threshold_ms (ms of stable text required before declaring
    # completion in the time-based fallback path). None = use global settings value.
    stable_threshold_ms: int | None = None

    # Aliases: alternate names that resolve to this site (e.g. ["grok"] for x-ai)
    aliases: list[str] = field(default_factory=list)

    # Friendly-name → picker-label mapping for model selection
    models: dict[str, str] = field(default_factory=dict)

    # Named UI capabilities (new_chat, model_selector, temperature, etc.)
    # Keyed by capability name; loaded from the 'capabilities:' YAML section.
    capabilities: dict[str, "CapabilityConfig"] = field(default_factory=dict)

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
            skip_new_chat=data.get("skip_new_chat", False),
            fallback_detection=data.get("fallback_detection", False),
            done_indicator=selectors.get("done_indicator"),
            completion_signal=data.get("completion_signal"),
            stable_threshold_ms=data.get("stable_threshold_ms"),
            aliases=data.get("aliases", []),
            models=data.get("models", {}),
            capabilities={
                k: CapabilityConfig.from_dict(v)
                for k, v in data.get("capabilities", {}).items()
            },
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
                if (
                    cfg.name in candidates
                    or f.stem in candidates
                    or any(a in candidates for a in cfg.aliases)
                ):
                    return cfg
            except Exception:
                continue
        available = [f.stem for f in sites_dir.glob("*.yaml")]
        raise ValueError(
            f"No site config found for {name!r}. "
            f"Available: {available or ['(none)']}"
        )
