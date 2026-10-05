"""Browser-session based LMS adapter.

University portals often sit behind SSO with JS redirects and captchas, so we do not try to
re-implement the login protocol. Instead:
  1. `python -m app lms-login <name>` opens a visible browser; the user logs in once by hand.
  2. The Playwright storage state (cookies + localStorage) is saved to data/lms_<name>.json.
  3. The watcher reuses that state headlessly and calls the LMS's own JSON endpoints.
  4. If configured, selectors + credentials allow unattended re-login when the session expires.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, async_playwright

from ..config import LMSInstance
from ..models import Assignment

log = logging.getLogger(__name__)

INTERACTIVE_LOGIN_TIMEOUT_S = 300


class SessionExpired(Exception):
    """Saved session is gone and no unattended login is possible."""


class LMSAdapter(ABC):
    def __init__(self, inst: LMSInstance, headless: bool = True):
        self.inst = inst
        self.headless = headless

    # ── to implement per platform ───────────────────────────
    @property
    @abstractmethod
    def login_url(self) -> str: ...

    @property
    @abstractmethod
    def home_url(self) -> str: ...

    @abstractmethod
    async def is_logged_in(self, page: Page) -> bool:
        """Check session on the *current* page/context without disrupting the user."""

    @abstractmethod
    async def collect(self, page: Page) -> list[Assignment]: ...

    # ── shared flow ─────────────────────────────────────────
    async def fetch(self) -> list[Assignment]:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            try:
                context = await self._new_context(browser)
                page = await context.new_page()
                await page.goto(self.home_url, wait_until="domcontentloaded")
                if not await self.is_logged_in(page):
                    await self._auto_login(page)
                items = await self.collect(page)
                await context.storage_state(path=str(self.inst.state_path))  # refresh cookies
                return items
            finally:
                await browser.close()

    async def interactive_login(self) -> None:
        """Open a visible browser and wait until the user finishes logging in."""
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=False)
            try:
                context = await self._new_context(browser)
                page = await context.new_page()
                await page.goto(self.login_url, wait_until="domcontentloaded")
                print(f"[{self.inst.name}] Log in in the opened browser window (waiting up to "
                      f"{INTERACTIVE_LOGIN_TIMEOUT_S // 60} min)...")
                loop = asyncio.get_running_loop()
                deadline = loop.time() + INTERACTIVE_LOGIN_TIMEOUT_S
                while loop.time() < deadline:
                    await asyncio.sleep(2)
                    current = context.pages[-1] if context.pages else page
                    try:
                        if await self.is_logged_in(current):
                            break
                    except Exception:  # page mid-navigation
                        continue
                else:
                    raise TimeoutError("Login was not completed in time")
                await context.storage_state(path=str(self.inst.state_path))
                print(f"[{self.inst.name}] Session saved to {self.inst.state_path}")
            finally:
                await browser.close()

    async def probe(self) -> dict:
        """Follow the login redirect chain and list form fields, to help configure auto-login."""
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(self.login_url, wait_until="networkidle")
                fields = await page.evaluate(
                    """() => [...document.querySelectorAll('input, button, select')].map(e => ({
                        tag: e.tagName.toLowerCase(), type: e.type || '', id: e.id || '',
                        name: e.name || '', placeholder: e.placeholder || '',
                        text: (e.innerText || e.value || '').trim().slice(0, 40)
                    })).filter(f => f.type !== 'hidden')"""
                )
                return {"final_url": page.url, "title": await page.title(), "fields": fields}
            finally:
                await browser.close()

    async def _new_context(self, browser) -> BrowserContext:
        state = self.inst.state_path
        return await browser.new_context(
            storage_state=str(state) if state.exists() else None,
            locale="ko-KR",
            viewport={"width": 1280, "height": 900},
        )

    async def _auto_login(self, page: Page) -> None:
        if not self.inst.can_auto_login:
            raise SessionExpired(
                f"LMS '{self.inst.name}' session expired. Run: python -m app lms-login {self.inst.name}"
            )
        log.info("[%s] session expired, trying unattended login", self.inst.name)
        dialogs: list[str] = []  # portals report errors like "change your password" via alert()

        async def on_dialog(dialog) -> None:
            dialogs.append(dialog.message)
            await dialog.accept()

        page.on("dialog", on_dialog)
        await page.goto(self.login_url, wait_until="networkidle")
        for selector in self.inst.pre_login_clicks:
            # "?selector" = optional step, e.g. switch language only if it isn't switched already
            optional = selector.startswith("?")
            try:
                await page.click(selector.removeprefix("?"), timeout=5_000 if optional else 30_000)
            except Exception:
                if not optional:
                    raise
                continue
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
        for selector, value in ((self.inst.username_selector, self.inst.username),
                                (self.inst.password_selector, self.inst.password)):
            try:
                await page.fill(selector, value)
            except Exception as exc:
                # Playwright errors echo the filled value; never let credentials reach logs
                raise RuntimeError(f"Login field {selector!r} not fillable ({type(exc).__name__})") from None
        if self.inst.submit_selector:
            await page.click(self.inst.submit_selector)
        else:
            await page.press(self.inst.password_selector, "Enter")
        for selector in self.inst.post_login_clicks:
            try:
                await page.click(selector, timeout=10_000)
                log.info("[%s] clicked post-login prompt: %s", self.inst.name, selector)
            except Exception:
                pass  # prompt didn't appear this time
        # SSO portals redirect back to the LMS after login; wait for that before checking the session
        host = urlparse(self.inst.url).netloc
        try:
            await page.wait_for_url(lambda u: urlparse(u).netloc == host, timeout=30_000)
        except Exception:
            pass
        await page.goto(self.home_url, wait_until="domcontentloaded")
        if not await self.is_logged_in(page):
            reason = f"portal says: {' / '.join(dialogs)}" if dialogs else "captcha/2FA or wrong selectors"
            raise SessionExpired(
                f"Unattended login to '{self.inst.name}' failed ({reason}). "
                f"Run: python -m app lms-login {self.inst.name}"
            )
