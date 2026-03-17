"""
Playwright browser session manager.

Owns a browser for the lifetime of a site session.
All browser interactions are serialized through an asyncio.Lock.
"""

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

# patchright patches navigator.webdriver, TLS fingerprint, and other bot signals.
# Used only in headless mode — headed mode doesn't need it (Cloudflare passes real browsers).
try:
    from patchright.async_api import async_playwright as _patchright_playwright  # type: ignore

    _HAVE_PATCHRIGHT = True
except ImportError:
    _HAVE_PATCHRIGHT = False

from proxy.config import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Selector for "are we logged in?" — chat input present means authenticated
# ---------------------------------------------------------------------------
CHAT_INPUT_SELECTOR = (
    '[role="textbox"], '
    'textarea[placeholder*="message" i], '
    'textarea[placeholder*="ask" i], '
    'div[contenteditable="true"]'
)

# Chromium launch flags shared across all launch modes.
# --disable-blink-features=AutomationControlled : suppress bot-detection signals.
# --disable-features=ExternalProtocolDialog     : prevent Windows "Open in X app" / Store
#     redirects when navigating to x.com or other sites with registered app protocol handlers.
_CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=ExternalProtocolDialog",
]



# ---------------------------------------------------------------------------
# LoginHandler abstraction � decouples platform-specific notifications
# ---------------------------------------------------------------------------


class LoginHandler:
    """Abstract base for login notification strategies."""

    def notify(self, url: str) -> None:
        """Notify user that login is required at the given URL.

        Args:
            url: The login URL to display to the user.
        """
        raise NotImplementedError


class WindowsMessageBoxLoginHandler(LoginHandler):
    """Windows MessageBox notification (platform-specific)."""

    def notify(self, url: str) -> None:
        """Fire a Windows toast notification so the login prompt is hard to miss."""
        if sys.platform != "win32":
            return
        try:
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "[System.Windows.Forms.MessageBox]::Show("
                f"'Log in to {url} in the browser window, then press ENTER in the terminal.', "
                "'Proxy: Login Required', "
                "'OK', "
                "'Information') | Out-Null"
            )
            subprocess.Popen(
                ["powershell", "-WindowStyle", "Hidden", "-Command", ps],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            pass  # notification is best-effort


class HeadlessLoginHandler(LoginHandler):
    """Silent handler for CI/CD and headless environments (logs only)."""

    def notify(self, url: str) -> None:
        """Log login requirement without user interaction."""
        log.info("Login required at %s", url)


def get_login_handler(headless: bool) -> LoginHandler:
    """Factory function to select appropriate login handler.

    Args:
        headless: Whether running in headless mode.

    Returns:
        Appropriate LoginHandler instance for the environment.
    """
    if headless or sys.platform != "win32":
        return HeadlessLoginHandler()
    return WindowsMessageBoxLoginHandler()


class BrowserSession:
    """Playwright session with cookie persistence for one site."""

    def __init__(
        self,
        url: str | None = None,
        cookies_path: Path | None = None,
        auth_check: str | None = None,
        login_handler: LoginHandler | None = None,
    ) -> None:
        self._url = url or settings.use_ai_url
        self._cookies_path = cookies_path or settings.cookies_path
        self._auth_check = auth_check  # overrides default CHAT_INPUT_SELECTOR when set
        self._login_handler = login_handler or get_login_handler(settings.headless)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()
        self._ready = False
        self._login_notify: asyncio.Event | None = None
        self._login_pending: bool = False

    # ------------------------------------------------------------------
    # Public state
    # ------------------------------------------------------------------

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def login_pending(self) -> bool:
        return self._login_pending

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser not initialized")
        return self._page

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Initialize the browser session.

        Starts Playwright, launches browser (headless or headed per settings),
        restores session from saved cookies if available, or prompts for login.

        Note: When called through ensure_ready(), this is guarded by a 30-second
        asyncio.timeout on lock acquisition. If initialization cannot acquire
        the lock within 30 seconds, asyncio.TimeoutError is raised.

        Raises:
            RuntimeError: If Playwright fails to start or browser launch fails.
            asyncio.TimeoutError: If lock acquisition times out (via ensure_ready).
        """
        if settings.headless and _HAVE_PATCHRIGHT:
            log.info("Using patchright (stealth headless mode)")
            self._playwright = await _patchright_playwright().start()
        else:
            self._playwright = await async_playwright().start()

        has_cookies = self._cookies_path.exists()

        if has_cookies:
            await self._launch_with_cookies()
            authenticated = await self._check_authenticated()
            if not authenticated:
                log.warning("Saved session expired, re-launching for login...")
                self._cookies_path.unlink(missing_ok=True)
                await self._close_browser()
                await self._launch_headed_and_login()
        else:
            await self._launch_headed_and_login()

        self._ready = True
        log.info("Browser session ready.")

    async def ensure_ready(self) -> None:
        """Initialize on first use; safe to call multiple times.

        Acquires the internal lock with a 30-second timeout to prevent indefinite
        hangs if another coroutine is stuck holding the lock.

        Raises:
            asyncio.TimeoutError: If lock cannot be acquired within 30 seconds.
        """
        if self._ready:
            return
        try:
            async with asyncio.timeout(30):
                async with self._lock:
                    if not self._ready:
                        await self.initialize()
        except asyncio.TimeoutError:
            log.error(
                "Browser session initialization timed out after 30 seconds while waiting for lock. "
                "Another operation may be stuck. Consider restarting the proxy."
            )
            raise

    async def notify_login(self) -> None:
        """Signal that the user has completed login in the browser window."""
        if self._login_notify is not None:
            self._login_notify.set()

    async def is_healthy(self) -> bool:
        """Quick liveness check before and after recovery attempts (2s timeout)."""
        try:
            if self._page is None or self._page.is_closed():
                return False
            url = self._page.url
            if not url or url == "about:blank" or "login" in url.lower():
                return False
            await self._page.wait_for_selector(
                self._auth_check or CHAT_INPUT_SELECTOR,
                timeout=2000,
                state="visible",
            )
            return True
        except Exception:
            return False

    async def recover(self, chat_url: str | None = None) -> bool:
        """Attempt lightweight in-place recovery without full re-init.

        Tries (1) navigation to chat_url or site root, (2) page reload.
        Returns True if session is healthy again, False if full re-init is needed.
        Auth-expired sessions (login page) skip in-place repair entirely.

        Note: This method does not acquire the browser lock, so it can be called
        concurrently with other operations. Browser interactions respect internal
        Playwright timeouts (15s for navigation, 10s for reload). If the lock
        is contended, recovery may be blocked by another operation's 30-second
        initialization timeout.
        """
        try:
            if self._page is None or self._page.is_closed():
                return False
            if "login" in self._page.url.lower():
                return False  # cookies expired — only reauth() can fix this

            target = chat_url or self._url
            try:
                await self._page.goto(target, wait_until="domcontentloaded", timeout=15000)
                if await self.is_healthy():
                    log.info("Session recovered via navigation to %s", target)
                    return True
            except Exception as exc:
                log.warning("Recovery navigation to %s failed: %s", target, exc)

            try:
                await self._page.reload(wait_until="domcontentloaded", timeout=10000)
                if await self.is_healthy():
                    log.info("Session recovered via page reload")
                    return True
            except Exception as exc:
                log.warning("Recovery reload failed: %s", exc)
        except Exception as exc:
            log.warning("Unexpected error during recover(): %s", exc)

        return False

    async def reauth(self) -> None:
        """Re-authenticate: close the current browser, delete stale cookies, open a headed login window."""
        log.warning("Re-authenticating: closing existing browser session")
        self._ready = False
        self._cookies_path.unlink(missing_ok=True)
        await self._close_browser()
        await self._launch_headed_and_login()
        self._ready = True
        log.info("Re-authentication complete")

    async def close(self) -> None:
        self._ready = False
        await self._close_browser()
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    # ------------------------------------------------------------------
    # Internal launch helpers
    # ------------------------------------------------------------------

    async def _launch_with_cookies(self) -> None:
        """Launch browser (headless or headed per settings) with saved session."""
        assert self._playwright
        headless = settings.headless
        log.info("Launching browser (headless=%s) with saved session", headless)
        self._browser = await self._playwright.chromium.launch(
            headless=headless,
            args=_CHROMIUM_ARGS,
        )
        self._context = await self._browser.new_context(
            storage_state=str(self._cookies_path),
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        self._page = await self._context.new_page()
        await self._page.goto(self._url, wait_until="domcontentloaded")

    async def _launch_headed_and_login(self) -> None:
        assert self._playwright
        self._browser = await self._playwright.chromium.launch(
            headless=False,
            args=_CHROMIUM_ARGS,
        )
        self._context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        self._page = await self._context.new_page()
        await self._page.goto(self._url, wait_until="domcontentloaded")

        self._login_handler.notify(self._url)
        log.info(
            "Browser open for login at %s — waiting for POST /session/notify-login signal",
            self._url,
        )

        # Wait for the user to signal login completion via POST /session/notify-login/<site>.
        # run.py detects login_pending and exits with code 3; Claude tells the user to log in
        # and, when they say "done", calls the notify endpoint which fires this event.
        self._login_notify = asyncio.Event()
        self._login_pending = True

        await self._login_notify.wait()

        self._login_pending = False
        self._login_notify = None

        await self.save_cookies()
        log.info("Session saved to %s", self._cookies_path)

        # Close the headed browser and reopen headless from the saved cookies.
        # This keeps subsequent interactions invisible to the user.
        log.info("Switching to headless session...")
        await self._close_browser()
        await self._launch_with_cookies()


    async def _close_browser(self) -> None:
        if self._context:
            await self._context.close()
            self._context = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        self._page = None

    # ------------------------------------------------------------------
    # Auth check
    # ------------------------------------------------------------------

    async def _check_authenticated(self) -> bool:
        assert self._page
        selector = self._auth_check or CHAT_INPUT_SELECTOR
        try:
            await self._page.wait_for_selector(selector, timeout=6000)
            return True
        except PlaywrightTimeoutError:
            return False

    # ------------------------------------------------------------------
    # Cookie persistence
    # ------------------------------------------------------------------

    async def save_cookies(self) -> None:
        """Save browser cookies atomically.

        Writes to a temporary file first, then performs an atomic rename to the
        target cookies_path. This prevents corruption if the process crashes mid-write.
        """
        assert self._context
        self._cookies_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write: write to temp file, then atomic rename
        temp_path = self._cookies_path.parent / f"{self._cookies_path.name}.tmp"
        await self._context.storage_state(path=str(temp_path))
        try:
            os.replace(str(temp_path), str(self._cookies_path))
        except Exception as exc:
            log.error("Failed to atomically rename cookies: %s", exc)
            raise
