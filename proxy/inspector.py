"""
DOM inspection and selector heuristics.

Provides:
  INSPECT_JS          — JS snippet that snapshots interactive DOM elements
  heuristic_selectors — derive CSS selector suggestions from the DOM snapshot
  find_yaml_for_site  — locate the YAML config file for a site name
  write_selectors_to_yaml — merge heuristic suggestions into an existing YAML config
"""

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DOM snapshot — run via page.evaluate() to collect interactive elements
# ---------------------------------------------------------------------------

INSPECT_JS = """() => {
    const truncate = (s, n) => (s || '').trim().slice(0, n);

    const buttons = Array.from(document.querySelectorAll('button')).map(el => ({
        text: truncate(el.innerText, 80),
        type: el.type || null,
        ariaLabel: el.getAttribute('aria-label'),
        dataTestid: el.getAttribute('data-testid'),
        classes: truncate(el.className, 120),
        disabled: el.disabled,
    }));

    const inputs = Array.from(document.querySelectorAll('input, textarea')).map(el => ({
        tag: el.tagName.toLowerCase(),
        type: el.type || null,
        placeholder: truncate(el.placeholder, 80),
        ariaLabel: el.getAttribute('aria-label'),
        name: el.name || null,
        id: el.id || null,
        role: el.getAttribute('role'),
        classes: truncate(el.className, 120),
    }));

    const contenteditable = Array.from(document.querySelectorAll('[contenteditable]')).map(el => ({
        role: el.getAttribute('role'),
        ariaLabel: el.getAttribute('aria-label'),
        placeholder: el.getAttribute('placeholder') || el.getAttribute('data-placeholder'),
        classes: truncate(el.className, 120),
    }));

    const chatLinks = Array.from(document.querySelectorAll('a[href]'))
        .filter(el => {
            const h = el.getAttribute('href') || '';
            return /chat|new|history|convers|thread/i.test(h);
        })
        .map(el => ({
            text: truncate(el.innerText, 80),
            href: truncate(el.getAttribute('href'), 120),
            ariaLabel: el.getAttribute('aria-label'),
        }));

    const selects = Array.from(document.querySelectorAll('select')).map(el => ({
        name: el.name || null,
        id: el.id || null,
        ariaLabel: el.getAttribute('aria-label'),
        options: Array.from(el.options).map(o => truncate(o.text, 40)).slice(0, 15),
    }));

    const dataRoles = Array.from(
        document.querySelectorAll('[data-role], [data-message-role]')
    ).map(el => ({
        tag: el.tagName.toLowerCase(),
        dataRole: el.getAttribute('data-role'),
        dataMessageRole: el.getAttribute('data-message-role'),
        classes: truncate(el.className, 80),
        textPreview: truncate(el.innerText, 100),
    }));

    const modelCandidates = Array.from(
        document.querySelectorAll('[aria-label*="model" i], [data-testid*="model" i], ' +
            'button[class*="model" i], [class*="model-select" i]')
    ).map(el => ({
        tag: el.tagName.toLowerCase(),
        text: truncate(el.innerText, 80),
        ariaLabel: el.getAttribute('aria-label'),
        classes: truncate(el.className, 120),
    }));

    return { buttons, inputs, contenteditable, chatLinks, selects, dataRoles, modelCandidates };
}"""


# ---------------------------------------------------------------------------
# Heuristic selector derivation
# ---------------------------------------------------------------------------


def heuristic_selectors(dom: dict) -> dict:
    """
    Apply heuristic rules to a DOM snapshot to suggest CSS selectors.
    Returns a dict with keys matching SiteConfig fields (only non-None entries).
    """
    suggestions: dict[str, str | None] = {
        "chat_input": None,
        "submit_button": None,
        "last_ai_msg": None,
        "thinking_spinner": None,
        "new_chat": None,
        "model_selector": None,
    }

    for el in dom.get("contenteditable", []):
        if el.get("role") == "textbox":
            suggestions["chat_input"] = '[role="textbox"]'
            break
    if not suggestions["chat_input"]:
        for el in dom.get("inputs", []):
            if el.get("tag") == "textarea":
                ph = (el.get("placeholder") or "").lower()
                if any(w in ph for w in ("message", "ask", "type", "prompt")):
                    placeholder = el.get("placeholder", "")
                    suggestions["chat_input"] = (
                        f'textarea[placeholder*="{placeholder[:30]}" i]'
                    )
                    break

    for el in dom.get("buttons", []):
        if el.get("type") == "submit":
            suggestions["submit_button"] = 'button[type="submit"]'
            break
    if not suggestions["submit_button"]:
        for el in dom.get("buttons", []):
            label = (el.get("ariaLabel") or el.get("text") or "").lower()
            if "send" in label or "submit" in label:
                aria = el.get("ariaLabel")
                if aria:
                    suggestions["submit_button"] = (
                        f'button[aria-label*="{aria[:30]}" i]'
                    )
                    break

    for el in dom.get("dataRoles", []):
        if el.get("dataRole") == "assistant":
            suggestions["last_ai_msg"] = '[data-role="assistant"]'
            break
    if not suggestions["last_ai_msg"]:
        for el in dom.get("dataRoles", []):
            if el.get("dataMessageRole") == "assistant":
                suggestions["last_ai_msg"] = '[data-message-role="assistant"]'
                break

    for el in dom.get("chatLinks", []):
        href = el.get("href") or ""
        if "new" in href.lower():
            suggestions["new_chat"] = f'a[href*="{href[:40]}" i]'
            break
    if not suggestions["new_chat"]:
        for el in dom.get("buttons", []):
            text = (el.get("text") or el.get("ariaLabel") or "").lower()
            if "new" in text and "chat" in text:
                aria = el.get("ariaLabel")
                if aria:
                    suggestions["new_chat"] = f'button[aria-label*="{aria[:30]}" i]'
                    break

    if dom.get("modelCandidates"):
        el = dom["modelCandidates"][0]
        aria = el.get("ariaLabel")
        if aria:
            suggestions["model_selector"] = f'[aria-label*="{aria[:40]}" i]'

    return {k: v for k, v in suggestions.items() if v}


# ---------------------------------------------------------------------------
# YAML config helpers
# ---------------------------------------------------------------------------

_SELECTOR_KEYS = frozenset(
    {"chat_input", "submit_button", "last_ai_msg", "thinking_spinner", "new_chat"}
)


def find_yaml_for_site(site_name: str, sites_dir: Path) -> Path | None:
    """Return the YAML config file path for a site, or None if not found."""
    import yaml

    for f in sites_dir.glob("*.yaml"):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            if data.get("name") == site_name or f.stem == site_name:
                return f
        except Exception:
            continue
    return None


def write_selectors_to_yaml(yaml_path: Path, suggestions: dict) -> None:
    """Merge suggested selectors into an existing site YAML config."""
    import yaml

    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))

    selectors = data.get("selectors") or {}
    for k, v in suggestions.items():
        if k in _SELECTOR_KEYS and v:
            selectors[k] = v
    if selectors:
        data["selectors"] = selectors

    if "placeholders" in suggestions and suggestions["placeholders"]:
        data["placeholders"] = suggestions["placeholders"]

    yaml_path.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    log.info("Wrote selectors to %s", yaml_path)
