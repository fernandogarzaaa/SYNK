"""CDP Browser Controller: optional direct browser control (Phase 2).
Implements the 'Runtime' side of the Truth Layer.
Requires playwright; fails closed when unavailable so extension mode still works.
"""
from __future__ import annotations
from typing import Optional, Dict, Any

try:
    from playwright.async_api import async_playwright, Browser, Page, BrowserContext
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:  # keep extension mode working without playwright
    async_playwright = None  # type: ignore
    Browser = object  # type: ignore
    Page = object  # type: ignore
    BrowserContext = object  # type: ignore
    _PLAYWRIGHT_AVAILABLE = False


class BrowserController:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self.playwright = None
        self.browser = None
        self.context = None
        self.pages: Dict[str, Any] = {}
        self.available = _PLAYWRIGHT_AVAILABLE

    async def start(self):
        """Launches the browser and creates a shared context."""
        if not self.available:
            print("BrowserController unavailable: playwright not installed (extension mode).")
            return
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=["--remote-debugging-port=9222"]
        )
        self.context = await self.browser.new_context()
        print("Browser started via CDP.")

    async def _require_page(self, tab_id: str = "default"):
        if not self.available or self.context is None:
            raise RuntimeError("CDP browser unavailable (playwright missing or not started)")
        if tab_id in self.pages:
            return self.pages[tab_id]
        page = await self.context.new_page()
        self.pages[tab_id] = page
        return page

    async def get_page(self, tab_id: str = "default"):
        return await self._require_page(tab_id)

    async def navigate(self, tab_id: str, url: str):
        page = await self._require_page(tab_id)
        await page.goto(url)
        return page.url

    async def click(self, tab_id: str, selector: str):
        page = await self._require_page(tab_id)
        await page.click(selector)

    async def type(self, tab_id: str, selector: str, text: str):
        page = await self._require_page(tab_id)
        await page.fill(selector, text)

    async def snapshot(self, tab_id: str) -> Dict[str, Any]:
        """Extracts structured DOM matching extension/content.js shape.

        Returns {url, title, nodes: [{role,name,tag,selector,interactive}]}.
        """
        page = await self._require_page(tab_id)
        nodes = await page.evaluate("""() => {
            const selector = (el) => {
                if (el.id) return '#' + el.id;
                const parts = [];
                let cur = el;
                for (let i = 0; i < 4 && cur && cur !== document.body; i++) {
                    let s = cur.tagName.toLowerCase();
                    const c = (typeof cur.className === 'string' ? cur.className.trim().split(/\\s+/)[0] : '');
                    if (c) s += '.' + c;
                    parts.unshift(s);
                    cur = cur.parentElement;
                }
                return parts.join(' > ');
            };
            const roleOf = (el) => {
                const explicit = el.getAttribute && el.getAttribute('role');
                if (explicit) return explicit;
                const tag = el.tagName.toLowerCase();
                if (tag === 'button') return 'button';
                if (tag === 'a') return 'link';
                if (tag === 'input') {
                    const t = (el.type || 'text').toLowerCase();
                    if (t === 'checkbox') return 'checkbox';
                    if (t === 'radio') return 'radio';
                    if (t === 'password') return 'passwordbox';
                    return 'textbox';
                }
                if (tag === 'textarea') return 'textbox';
                if (tag === 'select') return 'combobox';
                if (tag === 'form') return 'form';
                if (/^h[1-6]$/.test(tag)) return 'heading';
                if (tag === 'img') return 'image';
                return tag;
            };
            const nameOf = (el) => {
                return (
                    (el.getAttribute && el.getAttribute('aria-label')) ||
                    (el.innerText ? el.innerText.trim().slice(0, 80) : '') ||
                    el.value || el.placeholder || el.title || el.name || ''
                ).toString().trim().slice(0, 120);
            };
            const els = document.querySelectorAll(
                'a,button,input,select,textarea,form,[role],[aria-label],h1,h2,h3'
            );
            const out = [];
            els.forEach((el) => {
                const r = el.getBoundingClientRect();
                if (r.width === 0 && r.height === 0) return;
                const tag = el.tagName.toLowerCase();
                out.push({
                    role: roleOf(el),
                    name: nameOf(el),
                    tag,
                    selector: selector(el),
                    interactive: ['a','button','input','select','textarea'].includes(tag),
                });
                if (out.length >= 800) return;
            });
            return out;
        }""")
        return {
            "url": page.url,
            "title": await page.title(),
            "nodes": nodes,
        }

    async def screenshot(self, tab_id: str = "default") -> bytes:
        """Capture a PNG screenshot of the tab (L3 selective vision)."""
        page = await self._require_page(tab_id)
        return await page.screenshot(type="png")

    async def stop(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
