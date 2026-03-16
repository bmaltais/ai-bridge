"""
Playwright browser session manager.

Owns a browser for the lifetime of a site session.
All browser interactions are serialized through an asyncio.Lock.
"""

import asyncio
import logging
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


class BrowserSession:
    """Playwright session with cookie persistence for one site."""

    def __init__(
        self,
        url: str | None = None,
        cookies_path: Path | None = None,
        auth_check: str | None = None,
    ) -> None:
        self._url = url or settings.use_ai_url
        self._cookies_path = cookies_path or settings.cookies_path
        self._auth_check = auth_check  # overrides default CHAT_INPUT_SELECTOR when set
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
        """Initialize on first use; safe to call multiple times."""
        if self._ready:
            return
        async with self._lock:
            if not self._ready:
                await self.initialize()

    async def notify_login(self) -> None:
        """Signal that the user has completed login in the browser window."""
        if self._login_notify is not None:
            self._login_notify.set()

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
            args=["--disable-blink-features=AutomationControlled"],
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
            args=["--disable-blink-features=AutomationControlled"],
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

        self._notify_login_required(self._url)
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

    @staticmethod
    def _notify_login_required(url: str) -> None:
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
        assert self._context
        self._cookies_path.parent.mkdir(parents=True, exist_ok=True)
        await self._context.storage_state(path=str(self._cookies_path))
