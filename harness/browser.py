"""CDP Browser Controller: backwards-compatible shim (Stage D).

The old controller launched a Chromium with ``--remote-debugging-port``
while the surrounding docs implied "attach to the user's browser" --
that claim was never true. This module now delegates to
``OwnedBrowserRuntime.launch()``: a SYNK-managed Chromium with a
persistent SYNK-owned profile. Same class name and method signatures so
``harness.server`` keeps working; new code should use the
``BrowserRuntime`` interface (``harness/browser_runtime.py``) directly.

Playwright remains optional: without it the controller reports
``available == False`` and the server runs in extension mode.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from .browser_owned import OwnedBrowserRuntime
from .browser_runtime import ElementTarget
from .session import MAIN_FRAME


def _playwright_available() -> bool:
    try:
        import playwright.async_api  # noqa: F401
        return True
    except ImportError:
        return False


class BrowserController:
    """Async-compat wrapper around OwnedBrowserRuntime (launch mode)."""

    def __init__(self, headless: bool = False, profile_dir: str | None = None):
        self.headless = headless
        self._rt = OwnedBrowserRuntime.launch(profile_dir=profile_dir,
                                              headless=headless)
        # Compat attributes read by older call sites.
        self.playwright = None
        self.browser = None
        self.context = None
        self.pages: Dict[str, Any] = {}

    @property
    def available(self) -> bool:
        return _playwright_available()

    @property
    def runtime(self) -> OwnedBrowserRuntime:
        """The underlying BrowserRuntime (connected after start())."""
        return self._rt

    async def start(self):
        """Connect the SYNK-owned browser (own loop thread; this coroutine
        only bridges)."""
        if not self.available:
            print("BrowserController unavailable: playwright not installed "
                  "(extension mode).")
            return
        await asyncio.to_thread(self._rt.connect)
        print("Browser started: SYNK-owned Chromium (persistent SYNK "
              "profile), NOT the user's browser.")

    def _target(self, tab_id: str, selector: str) -> ElementTarget:
        return ElementTarget(session_id=None, window_id="win_default",
                             tab_id=tab_id, frame_id=MAIN_FRAME,
                             frame_chain=[MAIN_FRAME], shadow_path=[],
                             locator={"strategy": "css", "value": selector})

    async def _require_ack(self, ack, what: str):
        if not ack.executed:
            raise RuntimeError(f"CDP {what} failed: {ack.error}")
        return ack

    async def navigate(self, tab_id: str, url: str):
        ack = await asyncio.to_thread(self._rt.navigate, url, tab_id=tab_id)
        await self._require_ack(ack, "navigate")
        return ack.observed.url if ack.observed else url

    async def click(self, tab_id: str, selector: str):
        ack = await asyncio.to_thread(self._rt.click,
                                      self._target(tab_id, selector))
        await self._require_ack(ack, "click")

    async def type(self, tab_id: str, selector: str, text: str):
        ack = await asyncio.to_thread(self._rt.type,
                                      self._target(tab_id, selector), text)
        await self._require_ack(ack, "type")

    async def get_page(self, tab_id: str = "default"):
        return await asyncio.to_thread(self._rt._page_for, tab_id)

    async def snapshot(self, tab_id: str = "default") -> Dict[str, Any]:
        """Structured DOM in extension/content.js shape."""
        return await asyncio.to_thread(self._rt.snapshot, tab_id)

    async def screenshot(self, tab_id: str = "default") -> bytes:
        return await asyncio.to_thread(self._rt.screenshot, tab_id=tab_id)

    async def stop(self):
        await asyncio.to_thread(self._rt.disconnect)
