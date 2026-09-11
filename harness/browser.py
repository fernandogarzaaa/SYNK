"""CDP Browser Controller: replaces the extension for direct browser control (Phase 2).
Implements the 'Runtime' side of the Truth Layer.
"""
from __future__ import annotations
import asyncio
from playwright.async_api import async_playwright, Browser, Page, BrowserContext
from typing import Optional, List, Dict, Any

class BrowserController:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.pages: Dict[str, Page] = {}

    async def start(self):
        """Launches the browser and creates a shared context."""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless, 
            args=["--remote-debugging-port=9222"]
        )
        self.context = await self.browser.new_context()
        print("Browser started via CDP.")

    async def get_page(self, tab_id: str = "default") -> Page:
        """Returns existing page or creates a new one."""
        if tab_id in self.pages:
            return self.pages[tab_id]
        
        page = await self.context.new_page()
        self.pages[tab_id] = page
        return page

    async def navigate(self, tab_id: str, url: str):
        page = await self.get_page(tab_id)
        await page.goto(url)
        return page.url

    async def click(self, tab_id: str, selector: str):
        page = await self.get_page(tab_id)
        await page.click(selector)

    async def type(self, tab_id: str, selector: str, text: str):
        page = await self.get_page(tab_id)
        await page.fill(selector, text)

    async def snapshot(self, tab_id: str) -> Dict[str, Any]:
        """Extracts structured DOM for the Truth Layer."""
        page = await self.get_page(tab_id)
        # This replaces the extension's content.js snapshotting
        nodes = await page.evaluate("""() => {
            const getNodes = (root) => {
                const result = [];
                const walk = (el) => {
                    if (el.nodeType !== 1) return;
                    result.push({
                        tag: el.tagName.toLowerCase(),
                        text: el.innerText?.slice(0, 100),
                        role: el.getAttribute('role'),
                        id: el.id,
                        className: el.className,
                        interactive: ['A', 'BUTTON', 'INPUT', 'SELECT'].includes(el.tagName)
                    });
                    for (const child of el.children) walk(child);
                };
                walk(document.body);
                return result;
            };
            return getNodes(document.body);
        }""")
        return {
            "url": page.url,
            "title": await page.title(),
            "nodes": nodes
        }

    async def stop(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
