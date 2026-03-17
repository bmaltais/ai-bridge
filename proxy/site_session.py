"""
Per-site browser session manager for the /v1/proxy endpoint.

Manages one BrowserSession per site, initializing lazily on first request.
Sessions persist for the lifetime of the server process.
"""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Page

from proxy.browser import BrowserSession
from proxy.site_config import SiteConfig

log = logging.getLogger(__name__)


@dataclass
class SessionMetrics:
    """Minimal metrics for a site session (request count, error count, total latency)."""

    requests: int = 0
    errors: int = 0
    total_latency_ms: int = 0

    @property
    def avg_latency_ms(self) -> float:
        """Return average latency in milliseconds, or 0 if no requests."""
        return self.total_latency_ms / self.requests if self.requests > 0 else 0


@dataclass
class SiteSession:
    """Container for a site's browser session and its configuration."""

    session: BrowserSession
    config: SiteConfig

    @property
    def page(self) -> Page:
        return self.session.page

    @property
    def lock(self) -> asyncio.Lock:
        return self.session.lock

    @property
    def is_ready(self) -> bool:
        return self.session.is_ready


class SiteSessionManager:
    """Holds one SiteSession per site, created on demand."""

    def __init__(self, sites_dir: Path) -> None:
        self._sites_dir = sites_dir
        self._sessions: dict[str, SiteSession] = {}
        self._init_locks: dict[str, asyncio.Lock] = {}
        self._metrics: dict[str, SessionMetrics] = {}

    async def get(self, site_name: str) -> SiteSession:
        """Return a ready SiteSession for the given site, initializing it if needed.

        Aliases (e.g. "grok" → "x-ai") share the same session — lookup is by canonical name.
        """
        # Resolve alias → canonical name via config, then use canonical as the session key.
        cfg = SiteConfig.find(site_name, self._sites_dir)
        key = cfg.name  # e.g. "x-ai" even when site_name="grok"

        # Fast path: already fully initialized
        if key in self._sessions and self._sessions[key].is_ready:
            return self._sessions[key]

        # One init-lock per canonical site name
        if key not in self._init_locks:
            self._init_locks[key] = asyncio.Lock()

        async with self._init_locks[key]:
            if key not in self._sessions:
                log.info(
                    "Initializing browser session for site=%s url=%s",
                    key,
                    cfg.url,
                )
                browser = BrowserSession(
                    url=cfg.url,
                    cookies_path=cfg.cookies_path,
                    auth_check=cfg.auth_check,
                )
                # Register BEFORE initialize() so /session/status can observe login_pending
                # while the browser is waiting for the user to log in.
                self._sessions[key] = SiteSession(session=browser, config=cfg)
                # Initialize metrics for this site
                if key not in self._metrics:
                    self._metrics[key] = SessionMetrics()

            if not self._sessions[key].is_ready:
                sess = self._sessions[key]
                await sess.session.initialize()
                log.info("Session ready for site=%s", key)

        return self._sessions[key]

    async def close_all(self) -> None:
        for name, site_session in self._sessions.items():
            log.info("Closing session for site=%s", name)
            await site_session.session.close()
        self._sessions.clear()

    def _resolve_key(self, site_name: str) -> str:
        """Resolve a site name or alias to its canonical session key."""
        from proxy.site_config import SiteConfig

        try:
            return SiteConfig.find(site_name, self._sites_dir).name
        except Exception:
            return site_name

    async def notify_login(self, site_name: str) -> bool:
        """Signal login completion for a site. Returns True if the signal was sent."""
        key = self._resolve_key(site_name)
        if key in self._sessions:
            await self._sessions[key].session.notify_login()
            return True
        return False

    def get_status(self, site_name: str) -> dict:
        """Return session status for a site (login_pending, ready, initialized)."""
        key = self._resolve_key(site_name)
        if key in self._sessions:
            sess = self._sessions[key].session
            return {
                "site": site_name,
                "canonical": key,
                "login_pending": sess.login_pending,
                "ready": sess.is_ready,
                "initialized": True,
            }
        return {
            "site": site_name,
            "login_pending": False,
            "ready": False,
            "initialized": False,
        }

    def record_request(self, site_name: str, latency_ms: int, error: bool = False) -> None:
        """Record a request metric for a site (non-blocking, threadlocal counter update)."""
        key = self._resolve_key(site_name)
        if key not in self._metrics:
            self._metrics[key] = SessionMetrics()
        metrics = self._metrics[key]
        metrics.requests += 1
        metrics.total_latency_ms += latency_ms
        if error:
            metrics.errors += 1

    def get_metrics(self) -> dict[str, dict]:
        """Return metrics for all sites as JSON-serializable dict."""
        result = {}
        for site_name, metrics in self._metrics.items():
            result[site_name] = {
                "requests": metrics.requests,
                "errors": metrics.errors,
                "total_latency_ms": metrics.total_latency_ms,
                "avg_latency_ms": round(metrics.avg_latency_ms, 2),
            }
        return result
