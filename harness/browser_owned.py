"""Adapter B: SYNK-OWNED browser via Playwright/CDP (Stage D).

Two explicit modes, constructed via classmethods -- never one ambiguous
"start a browser":

* ``OwnedBrowserRuntime.launch(profile_dir=..., headless=...)``:
  launches a SYNK-managed Chromium with a PERSISTENT SYNK profile
  (default ``~/.synk/profiles/default``). This is NOT the user's
  browser: fresh profile directory owned by SYNK, no user cookies, no
  user extensions. Crash recovery relaunches it.

* ``OwnedBrowserRuntime.attach(ws_endpoint)``:
  connects over CDP to a debugging endpoint the OPERATOR explicitly
  controls (e.g. a Chromium they started with --remote-debugging-port).
  Passing --remote-debugging-port to a launch does NOT mean "attach to
  the user's browser", and this adapter never claims that.

Playwright is OPTIONAL and lazily imported: importing this module and
constructing the adapter never requires it; connect() raises a clear
error naming the missing optional dependency. The harness server keeps
working in extension mode without playwright installed.

Lifecycle: existing-tab discovery on connect, tab open/close events,
navigation/popup/dialog/download/network hooks, crash/restart
detection, context reset, per-tab health.

The public API is synchronous (BrowserRuntime); an internal event loop
in a daemon thread bridges to Playwright's async API.

Stdlib only (+ optional playwright, lazy).
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any, Callable

from .browser_runtime import (BrowserAck, BrowserObservation, BrowserRuntime,
                              ElementTarget, FrameGone, TabGone, TabHealth,
                              UnsupportedOperation, observation_id_for)
from .interactions import (INTERACTION_JS, ack_for_observation,
                           ack_not_executed)
from .session import MAIN_FRAME, new_id

DEFAULT_PROFILE_ROOT = os.path.expanduser("~/.synk/profiles")
DEFAULT_PROFILE_DIR = os.path.join(DEFAULT_PROFILE_ROOT, "default")


def _require_playwright():
    try:
        from playwright.async_api import async_playwright  # noqa
        return True
    except ImportError:
        return False


def _playwright_missing_error() -> RuntimeError:
    return RuntimeError(
        "playwright is not installed (optional dependency). "
        "Install it for SYNK-owned browser mode: "
        "pip install playwright && playwright install chromium. "
        "Extension (attached) mode works without it.")


# Lightweight DOM census used by observe(); the full node snapshot for
# /snapshot ingestion lives in snapshot().
_CENSUS_JS = """() => {
  const els = document.querySelectorAll(
    'a,button,input,select,textarea,[role],[aria-label]');
  let interactive = 0;
  els.forEach((el) => {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) interactive++;
  });
  return { nodeCount: els.length, interactiveCount: interactive,
           url: location.href, title: document.title,
           readyState: document.readyState };
}"""

# Accessibility-ish node snapshot, same shape as extension/content.js.
_SNAPSHOT_JS = """() => {
  const selector = (el) => {
    if (el.id) return '#' + el.id;
    const parts = [];
    let cur = el;
    for (let i = 0; i < 4 && cur && cur !== document.body; i++) {
      let s = cur.tagName.toLowerCase();
      const c = (typeof cur.className === 'string'
        ? cur.className.trim().split(/\\s+/)[0] : '');
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
    return tag;
  };
  const nameOf = (el) => (
    (el.getAttribute && el.getAttribute('aria-label')) ||
    (el.innerText ? el.innerText.trim().slice(0, 80) : '') ||
    (el.type !== 'password' ? (el.value || '') : '') ||
    el.placeholder || el.title || el.name || '').toString().trim().slice(0, 120);
  const els = document.querySelectorAll(
    'a,button,input,select,textarea,form,[role],[aria-label],h1,h2,h3');
  const out = [];
  els.forEach((el) => {
    const r = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    const node = {
      role: roleOf(el), name: nameOf(el), tag, selector: selector(el),
      interactive: ['a','button','input','select','textarea'].includes(tag),
      visible: !(r.width === 0 && r.height === 0),
      disabled: !!el.disabled,
      href: tag === 'a' ? (el.getAttribute('href') || '') : '',
      frame_id: 'main',
    };
    if (tag === 'input' || tag === 'textarea') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'checkbox' || t === 'radio') node.checked = !!el.checked;
      else node.value = (t === 'password') ? '' : (el.value ?? '');
    }
    if (tag === 'select') {
      const opt = el.selectedOptions && el.selectedOptions[0];
      node.value = opt ? (opt.value ?? opt.text) : '';
    }
    out.push(node);
    if (out.length >= 800) return;
  });
  return out;
}"""

# Accessibility-ish node snapshot, same shape as extension/content.js.
_SNAPSHOT_JS = """() => {
  const selector = (el) => {
    if (el.id) return '#' + el.id;
    const parts = [];
    let cur = el;
    for (let i = 0; i < 4 && cur && cur !== document.body; i++) {
      let s = cur.tagName.toLowerCase();
      const c = (typeof cur.className === 'string'
        ? cur.className.trim().split(/\\s+/)[0] : '');
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
    return tag;
  };
  const nameOf = (el) => (
    (el.getAttribute && el.getAttribute('aria-label')) ||
    (el.innerText ? el.innerText.trim().slice(0, 80) : '') ||
    (el.type !== 'password' ? (el.value || '') : '') ||
    el.placeholder || el.title || el.name || '').toString().trim().slice(0, 120);
  const els = document.querySelectorAll(
    'a,button,input,select,textarea,form,[role],[aria-label],h1,h2,h3');
  const out = [];
  els.forEach((el) => {
    const r = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    const node = {
      role: roleOf(el), name: nameOf(el), tag, selector: selector(el),
      interactive: ['a','button','input','select','textarea'].includes(tag),
      visible: !(r.width === 0 && r.height === 0),
      disabled: !!el.disabled,
      href: tag === 'a' ? (el.getAttribute('href') || '') : '',
      frame_id: 'main',
    };
    if (tag === 'input' || tag === 'textarea') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'checkbox' || t === 'radio') node.checked = !!el.checked;
      else node.value = (t === 'password') ? '' : (el.value ?? '');
    }
    if (tag === 'select') {
      const opt = el.selectedOptions && el.selectedOptions[0];
      node.value = opt ? (opt.value ?? opt.text) : '';
    }
    out.push(node);
    if (out.length >= 800) return;
  });
  return out;
}"""

class OwnedBrowserRuntime(BrowserRuntime):
    """SYNK-owned Chromium. Construct via launch() or attach()."""

    def __init__(self, session_id: str | None = None,
                 window_id: str | None = None):
        self._mode: str | None = None
        self._profile_dir: str | None = None
        self._ws_endpoint: str | None = None
        self._headless = True
        self._session_id = session_id or "default_session"
        self._window_id = window_id or "win_default"
        self._connected = False
        self._crashed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pw = None
        self._browser = None
        self._context = None
        self._lock = threading.RLock()
        # tab_id ("owned-N") -> playwright Page
        self._pages: dict[str, Any] = {}
        self._page_tab: dict[int, str] = {}   # id(page) -> tab_id
        self._counter = 0
        self._health: dict[str, TabHealth] = {}
        self._callbacks: list[Callable[[str, dict], Any]] = []
        self._network: dict[str, list[dict]] = {}
        self._dialogs: dict[str, list[dict]] = {}
        self._downloads: dict[str, list[dict]] = {}
        self._frames: dict[str, dict[str, Any]] = {}  # tab -> {synk_fid: frame}

    # -- construction -------------------------------------------------------
    @classmethod
    def launch(cls, profile_dir: str | None = None,
               headless: bool = True,
               session_id: str | None = None,
               window_id: str | None = None) -> "OwnedBrowserRuntime":
        """Launch a SYNK-MANAGED Chromium with a persistent SYNK profile.

        This is never the user's browser: the profile directory is owned
        by SYNK (default ~/.synk/profiles/default). No user data is read.
        """
        rt = cls(session_id=session_id, window_id=window_id)
        rt._mode = "owned-launch"
        rt._profile_dir = profile_dir or DEFAULT_PROFILE_DIR
        rt._headless = headless
        return rt

    @classmethod
    def attach(cls, ws_endpoint: str,
               session_id: str | None = None,
               window_id: str | None = None) -> "OwnedBrowserRuntime":
        """Attach to a CDP debugging endpoint the OPERATOR controls.

        ws_endpoint like ws://127.0.0.1:9222/devtools/browser/<id>.
        This does NOT attach to "the user's browser" by magic: the
        operator must have started that endpoint themselves.
        """
        if not ws_endpoint or not ws_endpoint.startswith("ws"):
            raise ValueError(
                "attach() needs an explicit CDP websocket endpoint, e.g. "
                "ws://127.0.0.1:9222/devtools/browser/<id>")
        rt = cls(session_id=session_id, window_id=window_id)
        rt._mode = "attached-endpoint"
        rt._ws_endpoint = ws_endpoint
        return rt

    @property
    def mode(self) -> str:  # noqa: D102 (override annotation)
        return self._mode or "unconfigured"

    @property
    def connected(self) -> bool:
        return self._connected and not self._crashed

    # -- lifecycle ----------------------------------------------------------
    def connect(self) -> dict:
        if not _require_playwright():
            raise _playwright_missing_error()
        with self._lock:
            if self._mode is None:
                raise ValueError(
                    "use OwnedBrowserRuntime.launch() or .attach(), not "
                    "the bare constructor")
            if self._connected:
                return {"mode": self._mode, "session_id": None,
                        "note": "already connected"}
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._pump,
                                            daemon=True,
                                            name="synk-playwright")
            self._thread.start()
            try:
                self._run(self._connect_async(), timeout=60)
            except Exception:
                self._shutdown_loop()
                raise
            self._connected = True
            self._crashed = False
            return {"mode": self._mode,
                    "profile_dir": self._profile_dir,
                    "endpoint": self._ws_endpoint,
                    "tabs": len(self._pages)}

    def disconnect(self) -> None:
        with self._lock:
            if self._loop and self._connected:
                try:
                    self._run(self._disconnect_async(), timeout=15)
                except Exception:
                    pass
            self._connected = False
            self._shutdown_loop()

    def restart(self) -> dict:
        """Relaunch the SYNK-owned browser and re-adopt its tabs.

        Managed-launch mode only. The loop thread is torn down and rebuilt
        and every tab gets a FRESH TabHealth record: a restarted browser is
        a new incarnation, so old per-tab health (including CRASHED) does
        not carry over. Attached-endpoint mode raises UnsupportedOperation:
        SYNK must not restart a browser process it does not own -- use
        disconnect()/connect() to re-attach instead.
        """
        if self._mode != "owned-launch":
            raise UnsupportedOperation(
                "restart() relaunches the browser process: valid only in "
                "owned-launch mode. For attached-endpoint, disconnect() "
                "then connect() to re-attach.")
        self.disconnect()
        with self._lock:
            self._health.clear()
            self._frames.clear()
            self._crashed = False
        return self.connect()

    def _pump(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _shutdown_loop(self):
        loop, self._loop = self._loop, None
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        th, self._thread = self._thread, None
        if th is not None and th is not threading.current_thread():
            th.join(timeout=5)

    def _run(self, coro, timeout: float = 30):
        if self._loop is None:
            raise RuntimeError("browser loop not running")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    # -- async internals ------------------------------------------------------
    async def _connect_async(self):
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        if self._mode == "owned-launch":
            os.makedirs(self._profile_dir, exist_ok=True)
            self._context = await self._pw.chromium.launch_persistent_context(
                self._profile_dir, headless=self._headless,
                accept_downloads=True)
            # launch_persistent_context has no separate Browser object.
            self._browser = self._context.browser
        else:  # attached-endpoint
            self._browser = await self._pw.chromium.connect_over_cdp(
                self._ws_endpoint)
            contexts = self._browser.contexts
            self._context = contexts[0] if contexts else \
                await self._browser.new_context(accept_downloads=True)
        if self._browser is not None:
            self._browser.on("disconnected", self._on_browser_disconnected)
        # Existing-tab discovery: adopt every page already open.
        for page in list(self._context.pages):
            self._adopt_page(page, discovered=True)
        self._context.on("page", lambda p: self._adopt_page(p))

    async def _disconnect_async(self):
        try:
            if self._mode == "owned-launch" and self._context is not None:
                await self._context.close()
            elif self._browser is not None:
                await self._browser.close()
        finally:
            if self._pw is not None:
                await self._pw.stop()
            self._pw = self._browser = self._context = None
            self._pages.clear()
            self._page_tab.clear()

    def _mint_tab_id(self) -> str:
        self._counter += 1
        return f"owned-{self._counter}"

    def _adopt_page(self, page, discovered: bool = False) -> str:
        """Register a Playwright page as a SYNK tab; wire lifecycle hooks."""
        with self._lock:
            tab_id = self._page_tab.get(id(page))
            if tab_id is None:
                tab_id = self._mint_tab_id()
                self._pages[tab_id] = page
                self._page_tab[id(page)] = tab_id
                self._health[tab_id] = TabHealth(tab_id=tab_id)
                self._network[tab_id] = []
                self._dialogs[tab_id] = []
                self._downloads[tab_id] = []
                # Main frame maps to page.main_frame at resolution time
                # (never a None placeholder that could leak downstream).
                self._frames[tab_id] = {}
        # Hooks are idempotent per page object (guarded by attribute).
        if not getattr(page, "_synk_hooked", False):
            page._synk_hooked = True
            tid = tab_id
            page.on("close", lambda: self._on_page_close(tid, page))
            page.on("crash", lambda: self._on_page_crash(tid))
            page.on("popup", lambda p: self._adopt_page(p))
            page.on("dialog",
                    lambda d: self._record_dialog(tid, d))
            page.on("download",
                    lambda d: self._record_download(tid, d))
            page.on("request",
                    lambda r: self._network[tid].append(
                        {"type": "request", "url": r.url,
                         "method": r.method, "ts": time.time()}))
            page.on("response",
                    lambda r: self._network[tid].append(
                        {"type": "response", "url": r.url,
                         "status": r.status, "ts": time.time()}))
            page.on("framenavigated",
                    lambda f: self._emit("navigation",
                                         {"tab_id": tid,
                                          "url": f.url}))
        if discovered:
            self._emit("tab.discovered", {"tab_id": tab_id})
        else:
            self._emit("tab.opened", {"tab_id": tab_id})
        return tab_id

    # -- event plumbing -------------------------------------------------------
    def on_event(self, callback: Callable[[str, dict], Any]) -> None:
        self._callbacks.append(callback)

    def _emit(self, event: str, data: dict) -> None:
        payload = {"event": event, "ts": time.time(), **data}
        for cb in list(self._callbacks):
            try:
                cb(event, payload)
            except Exception:
                pass

    def _on_browser_disconnected(self):
        with self._lock:
            self._crashed = True
            for tid, h in self._health.items():
                h.mark_crashed("browser process disconnected")
        self._emit("browser.crashed", {"mode": self._mode})

    def _on_page_close(self, tab_id: str, page) -> None:
        with self._lock:
            self._pages.pop(tab_id, None)
            self._page_tab.pop(id(page), None)
            h = self._health.get(tab_id)
            if h:
                h.mark_closed()
        self._emit("tab.closed", {"tab_id": tab_id})

    def _on_page_crash(self, tab_id: str) -> None:
        h = self._health.get(tab_id)
        if h:
            h.mark_crashed("renderer crashed")
        self._emit("tab.crashed", {"tab_id": tab_id})

    def _record_dialog(self, tab_id: str, dialog) -> None:
        try:
            info = {"type": dialog.type, "message": dialog.message,
                    "default_value": dialog.default_value,
                    "ts": time.time()}
        except Exception:
            info = {"type": "unknown", "ts": time.time()}
        self._dialogs.setdefault(tab_id, []).append(info)
        self._emit("dialog.opened", {"tab_id": tab_id, **info})
        # Recorded, not auto-dismissed: a dialog blocks the page's JS
        # until handled, so callers observe it via inspect_dialogs() and
        # decide. Auto-dismissing would destroy evidence.

    def _record_download(self, tab_id: str, download) -> None:
        try:
            info = {"url": download.url,
                    "suggested_filename": download.suggested_filename,
                    "ts": time.time()}
        except Exception:
            info = {"ts": time.time()}
        self._downloads.setdefault(tab_id, []).append(info)
        self._emit("download", {"tab_id": tab_id, **info})

    # -- guards -----------------------------------------------------------------
    def _ensure_live(self, tab_id: str | None = None):
        if not self._connected or self._loop is None:
            raise RuntimeError("owned browser not connected; call connect()")
        if self._crashed:
            if self._mode == "owned-launch":
                # Crash recovery: relaunch the managed browser.
                self._emit("browser.restarting", {"mode": self._mode})
                self._run(self._disconnect_async(), timeout=15)
                self._connected = False
                self.connect()
                self._emit("browser.restarted", {"mode": self._mode})
                if tab_id is not None and tab_id not in self._pages:
                    raise TabGone(
                        f"tab {tab_id} did not survive the browser restart")
                return
            raise RuntimeError(
                "attached endpoint disconnected; call connect() again")

    def _page_for(self, tab_id: str):
        self._ensure_live(tab_id)
        page = self._pages.get(tab_id)
        if page is None:
            raise TabGone(f"unknown owned tab {tab_id}")
        return page

    def _health_ok(self, tab_id: str) -> TabHealth:
        h = self._health.get(tab_id)
        if h is None:
            h = TabHealth(tab_id=tab_id)
            self._health[tab_id] = h
        return h

    # -- tabs -------------------------------------------------------------------
    def list_tabs(self, *, window_id: str | None = None,
                  session_id: str | None = None) -> list[dict]:
        self._ensure_live()
        pages = self._run(self._list_pages_async(), timeout=15)
        out = []
        for tab_id, url, title in pages:
            out.append({"tab_id": tab_id, "window_id": "win_owned",
                        "session_id": session_id, "url": url, "title": title,
                        "health": self._health_ok(tab_id).state})
        return out

    async def _list_pages_async(self):
        out = []
        for tab_id, page in list(self._pages.items()):
            try:
                out.append((tab_id, page.url, await page.title()))
            except Exception:
                out.append((tab_id, "", ""))
        return out

    def tab_health(self, tab_id: str) -> TabHealth:
        self._ensure_live(tab_id)
        return self._health_ok(tab_id)

    def new_tab(self, url: str = "about:blank") -> str:
        """Open a new owned tab (tab creation lifecycle event)."""
        self._ensure_live()
        return self._run(self._new_tab_async(url), timeout=30)

    async def _new_tab_async(self, url: str) -> str:
        page = await self._context.new_page()
        tab_id = self._adopt_page(page)
        if url and url != "about:blank":
            await page.goto(url, wait_until="domcontentloaded")
        return tab_id

    def close_tab(self, tab_id: str) -> None:
        page = self._page_for(tab_id)
        self._run(page.close(), timeout=15)

    def reset_context(self) -> None:
        """Context reset: close the context and open a fresh one.

        Clears cookies/storage; all previous tab ids become CLOSED.
        """
        self._ensure_live()
        self._run(self._reset_context_async(), timeout=60)
        self._emit("context.reset", {})

    async def _reset_context_async(self):
        old_pages = list(self._pages.keys())
        await self._context.close()
        if self._mode == "owned-launch":
            self._context = await self._pw.chromium.launch_persistent_context(
                self._profile_dir, headless=self._headless,
                accept_downloads=True)
        else:
            self._context = await self._browser.new_context(
                accept_downloads=True)
        with self._lock:
            for tid in old_pages:
                h = self._health.get(tid)
                if h:
                    h.mark_closed()
            self._pages.clear()
            self._page_tab.clear()
        for page in list(self._context.pages):
            self._adopt_page(page, discovered=True)
        self._context.on("page", lambda p: self._adopt_page(p))

    # -- observation --------------------------------------------------------------
    def observe(self, *, tab_id: str, frame_id: str = MAIN_FRAME,
                session_id: str | None = None,
                window_id: str | None = None) -> BrowserObservation:
        session_id = session_id or self._session_id
        window_id = window_id or self._window_id
        page = self._page_for(tab_id)
        try:
            census = self._run(page.evaluate(_CENSUS_JS), timeout=15)
            url = census.get("url") or page.url
            title = census.get("title", "")
        except Exception as e:
            self._health_ok(tab_id).record_failure(str(e))
            raise
        self._health_ok(tab_id).record_success()
        census = census if isinstance(census, dict) else {}
        return BrowserObservation(
            observation_id=observation_id_for(tab_id, frame_id, url),
            session_id=session_id, window_id=window_id,
            tab_id=tab_id, frame_id=frame_id, url=url, title=title,
            target_state={"node_count": census.get("nodeCount", 0),
                          "interactive_count": census.get("interactiveCount", 0),
                          "ready_state": census.get("readyState", "")})

    def snapshot(self, tab_id: str) -> dict:
        """Full node snapshot (extension/content.js shape) for ingestion."""
        page = self._page_for(tab_id)
        nodes = self._run(page.evaluate(_SNAPSHOT_JS), timeout=30)
        return {"url": page.url, "title": self._run(page.title(), timeout=10),
                "nodes": nodes if isinstance(nodes, list) else []}

    # -- target resolution ----------------------------------------------------------
    def _frame_for(self, tab_id: str, target: ElementTarget):
        """Walk the frame_chain to a Playwright Frame (fail closed)."""
        page = self._page_for(tab_id)
        chain = target.frame_chain or [MAIN_FRAME]
        frame = page.main_frame
        if chain[0] != MAIN_FRAME:
            raise FrameGone(
                f"frame chain must start at 'main', got {chain[0]}")
        known = self._frames.setdefault(tab_id, {})
        # The main frame always maps to the page's live main frame.
        known.setdefault(MAIN_FRAME, frame)
        for fid in chain[1:]:
            nxt = known.get(fid)
            if nxt is None:
                # Refresh the frame map from the live page, then retry once.
                self._run(self._refresh_frames_async(tab_id), timeout=15)
                nxt = self._frames[tab_id].get(fid)
            if nxt is None:
                raise FrameGone(
                    f"frame {fid} not found in tab {tab_id} "
                    f"(chain {chain})")
            frame = nxt
        return frame

    async def _refresh_frames_async(self, tab_id: str):
        page = self._pages.get(tab_id)
        if page is None:
            return
        mapping: dict[str, Any] = {MAIN_FRAME: page.main_frame}
        # Map SYNK frame ids by index path: child_frames order matches
        # window.frames order for same-origin frames.
        def walk(pf, prefix):
            for i, cf in enumerate(pf.child_frames):
                fid = f"{prefix}{i}/" if prefix else f"sub:{i}"
                mapping[fid] = cf
                walk(cf, fid)
        walk(page.main_frame, "")
        self._frames[tab_id] = mapping

    def _locator_for(self, frame, locator: dict):
        strategy, value = locator.get("strategy"), locator.get("value")
        if not value:
            from .browser_runtime import ElementNotFound
            raise ElementNotFound("empty locator value")
        if strategy == "test-id":
            return frame.get_by_test_id(value)
        if strategy == "xpath":
            return frame.locator(f"xpath={value}")
        # css-id / css: Playwright's engine pierces open shadow DOM, so a
        # shadow_path is usually unnecessary here; when present we scope
        # through it explicitly via evaluate instead.
        return frame.locator(value)

    async def _resolve_async(self, target: ElementTarget):
        """Resolve target -> (frame, locator); fail closed on 0/2+ matches."""
        from .browser_runtime import (AmbiguousElement, ElementNotFound)
        frame = await asyncio.to_thread(self._frame_for, target.tab_id,
                                        target)
        locator = target.locator or {}
        if target.shadow_path:
            # Explicit shadow piercing via the shared JS core.
            spec = {"shadowPath": list(target.shadow_path),
                    "locator": dict(locator)}
            res = await frame.evaluate(
                f"{INTERACTION_JS};"
                "(() => { const s = window.__synk.resolveTarget(%s);"
                " return s.ok ? {ok:true} : s; })()"
                % _js_literal(spec))
            if not res.get("ok"):
                if res.get("errorCode") == "AMBIGUOUS_ELEMENT":
                    raise AmbiguousElement(res.get("error"))
                raise ElementNotFound(res.get("error"))
            # Re-scope: evaluate the primitive inside the shadow root via
            # a locator on the last host is unreliable; instead return a
            # marker that callers use with _eval_in_shadow.
            return frame, None, spec
        pl = self._locator_for(frame, locator)
        count = await pl.count()
        if count == 0:
            raise ElementNotFound(
                f"locator {locator.get('strategy')}:{locator.get('value')} "
                f"matched nothing in tab {target.tab_id}")
        if count > 1:
            raise AmbiguousElement(
                f"locator {locator.get('strategy')}:{locator.get('value')} "
                f"matched {count} elements; failing closed")
        return frame, pl, None

    def locate(self, target: ElementTarget) -> dict:
        self._ensure_live(target.tab_id)
        try:
            frame, pl, shadow_spec = self._run(
                self._resolve_async(target), timeout=20)
            if shadow_spec is not None:
                state = self._run(frame.evaluate(
                    f"{INTERACTION_JS};"
                    "(() => { const r = window.__synk.resolveTarget(%s);"
                    " return r.ok ? window.__synk.observeTarget(r.el) : r; })()"
                    % _js_literal(shadow_spec)), timeout=15)
            else:
                state = self._run(frame.evaluate(
                    f"{INTERACTION_JS};"
                    "(() => window.__synk.observeTarget("
                    "document.querySelector(%s)))()" % _js_literal(
                        target.locator.get("value"))), timeout=15)
            self._health_ok(target.tab_id).record_success()
            return {"found": True, "count": 1, "target_state": state}
        except Exception as e:
            self._health_ok(target.tab_id).record_failure(str(e))
            raise

    # -- interaction primitives -------------------------------------------------------
    def _do(self, target: ElementTarget, action_id: str | None,
            command: str, fn, *args) -> BrowserAck:
        """Run a primitive; success is only reported with an observation."""
        self._ensure_live(target.tab_id)
        try:
            observed = self._run(fn(target, *args), timeout=45)
        except Exception as e:
            self._health_ok(target.tab_id).record_failure(str(e))
            return ack_not_executed(command, target, str(e),
                                    _classify_browser_error(e),
                                    action_id)
        self._health_ok(target.tab_id).record_success()
        page = self._pages.get(target.tab_id)
        url = getattr(page, "url", "")
        title = ""
        try:
            title = self._run(page.title(), timeout=10)
        except Exception:
            pass
        return ack_for_observation(command, target, observed, action_id,
                                   url, title)

    def click(self, target: ElementTarget, *,
              action_id: str | None = None) -> BrowserAck:
        return self._do(target, action_id, "click", self._click_async)

    async def _click_async(self, target: ElementTarget) -> dict:
        frame, pl, shadow_spec = await self._resolve_async(target)
        if shadow_spec is not None:
            res = await frame.evaluate(
                f"{INTERACTION_JS};"
                "(() => { const r = window.__synk.resolveTarget(%s);"
                " return r.ok ? window.__synk.robustClick(r.el) : r; })()"
                % _js_literal(shadow_spec))
        else:
            # Playwright's real input pipeline: trusted events, actionability
            # checks (visible, stable, enabled). Then read back post-state.
            await pl.click(timeout=15000)
            res = await frame.evaluate(
                f"{INTERACTION_JS};"
                "(() => window.__synk.observeTarget(document.querySelector(%s)))()"
                % _js_literal(target.locator.get("value")))
        if not res.get("ok", True):
            raise RuntimeError(res.get("error", "click failed"))
        return res.get("observed", {})

    def type(self, target: ElementTarget, text: str, *,
             action_id: str | None = None,
             clear_first: bool = True) -> BrowserAck:
        return self._do(target, action_id, "type", self._type_async, text,
                        clear_first)

    async def _type_async(self, target: ElementTarget, text: str,
                          clear_first: bool) -> dict:
        # __synk is idempotent; install once per frame then use the robust
        # native-setter path (handles React controlled inputs).
        frame, pl, shadow_spec = await self._resolve_async(target)
        await frame.evaluate(INTERACTION_JS)
        if shadow_spec is not None:
            res = await frame.evaluate(
                "(spec, text, clearFirst) => {"
                " const r = window.__synk.resolveTarget(spec);"
                " return r.ok ? window.__synk.robustType(r.el, text,"
                " {clearFirst}) : r; }",
                {"shadowPath": list(target.shadow_path),
                 "locator": dict(target.locator)}, text, clear_first)
        else:
            # Scope the primitive to the single resolved element without
            # re-querying by selector (no TOCTOU between resolve and act).
            res = await frame.evaluate(
                "(sel, text, clearFirst) => {"
                " const els = document.querySelectorAll(sel);"
                " if (els.length !== 1) return {ok:false,"
                "  error:'element changed between resolve and act'};"
                " return window.__synk.robustType(els[0], text,"
                " {clearFirst}); }",
                target.locator.get("value"), text, clear_first)
        if not res.get("ok"):
            raise RuntimeError(res.get("error", "type failed"))
        return res.get("observed", {})

    def select(self, target: ElementTarget, value: str, *,
               action_id: str | None = None) -> BrowserAck:
        return self._do(target, action_id, "select", self._select_async,
                        value)

    async def _select_async(self, target: ElementTarget,
                            value: str) -> dict:
        frame, pl, shadow_spec = await self._resolve_async(target)
        await frame.evaluate(INTERACTION_JS)
        if shadow_spec is not None:
            res = await frame.evaluate(
                "(spec, value) => { const r = window.__synk.resolveTarget(spec);"
                " return r.ok ? window.__synk.robustSelect(r.el, value) : r; }",
                {"shadowPath": list(target.shadow_path),
                 "locator": dict(target.locator)}, value)
        else:
            chosen = await pl.select_option(value=value, timeout=15000)
            if not chosen:
                # Fall back to visible-text match via the robust primitive.
                res = await frame.evaluate(
                    "(sel, value) => { const els = document.querySelectorAll(sel);"
                    " if (els.length !== 1) return {ok:false,"
                    "  error:'element changed between resolve and act'};"
                    " return window.__synk.robustSelect(els[0], value); }",
                    target.locator.get("value"), value)
                if not res.get("ok"):
                    raise RuntimeError(res.get("error",
                                               "option not found: " + value))
                return res.get("observed", {})
            res = await frame.evaluate(
                f"{INTERACTION_JS};"
                "(() => window.__synk.observeTarget(document.querySelector(%s)))()"
                % _js_literal(target.locator.get("value")))
            res = {"ok": True, "observed": res}
        if not res.get("ok"):
            raise RuntimeError(res.get("error", "select failed"))
        return res.get("observed", {})

    def scroll(self, *, tab_id: str, frame_id: str = MAIN_FRAME,
               direction: str = "down", amount: int = 600,
               session_id: str | None = None,
               window_id: str = "win_default",
               action_id: str | None = None) -> BrowserAck:
        target = ElementTarget(session_id, window_id, tab_id, frame_id,
                               [frame_id], [], {"strategy": "css",
                                                 "value": "document"})
        self._ensure_live(tab_id)
        try:
            dy = -abs(amount) if direction == "up" else abs(amount)
            page = self._page_for(tab_id)
            self._run(page.evaluate(f"window.scrollBy(0, {int(dy)})"),
                      timeout=15)
            observed = {"scrolled_by": dy, "url": page.url}
        except Exception as e:
            self._health_ok(tab_id).record_failure(str(e))
            return ack_not_executed("scroll", target, str(e),
                                    _classify_browser_error(e), action_id)
        self._health_ok(tab_id).record_success()
        return ack_for_observation("scroll", target, observed, action_id,
                                   page.url)

    def keypress(self, target: ElementTarget | None, key: str, *,
                 action_id: str | None = None,
                 tab_id: str | None = None,
                 frame_id: str = MAIN_FRAME,
                 session_id: str | None = None,
                 window_id: str = "win_default") -> BrowserAck:
        tid = target.tab_id if target else tab_id
        if not tid:
            raise ValueError("keypress needs a target or an explicit tab_id")
        tgt = target or ElementTarget(session_id, window_id, tid, frame_id,
                                      [frame_id], [], {})
        self._ensure_live(tid)
        try:
            page = self._page_for(tid)
            if target is not None:
                frame, pl, _ = self._run(self._resolve_async(target),
                                         timeout=20)
                awaitable = pl.press(key, timeout=15000)
            else:
                awaitable = page.keyboard.press(key)
            self._run(awaitable, timeout=20)
            observed = {"key": key, "url": page.url}
        except Exception as e:
            self._health_ok(tid).record_failure(str(e))
            return ack_not_executed("press_key", tgt, str(e),
                                    _classify_browser_error(e), action_id)
        self._health_ok(tid).record_success()
        return ack_for_observation("press_key", tgt, observed, action_id,
                                   page.url)

    def navigate(self, url: str, *, tab_id: str,
                 frame_id: str = MAIN_FRAME,
                 session_id: str | None = None,
                 window_id: str = "win_default",
                 action_id: str | None = None,
                 timeout_ms: int = 30000) -> BrowserAck:
        tgt = ElementTarget(session_id, window_id, tab_id, frame_id,
                            [frame_id], [], {})
        self._ensure_live(tab_id)
        try:
            page = self._page_for(tab_id)
            resp = self._run(page.goto(url, wait_until="domcontentloaded",
                                       timeout=timeout_ms), timeout=timeout_ms / 1000 + 10)
            status = getattr(resp, "status", None)
            observed = {"url": page.url, "http_status": status,
                        "navigated": True}
            # Navigation replaces the document: drop cached frame handles.
            self._frames[tab_id] = {MAIN_FRAME: None}
        except Exception as e:
            self._health_ok(tab_id).record_failure(str(e))
            return ack_not_executed("navigate", tgt, str(e),
                                    _classify_browser_error(e), action_id)
        self._health_ok(tab_id).record_success()
        return ack_for_observation("navigate", tgt, observed, action_id,
                                   page.url)

    def wait_for(self, *, tab_id: str, frame_id: str = MAIN_FRAME,
                 condition: str, timeout_ms: int = 10000,
                 session_id: str | None = None,
                 window_id: str | None = None) -> BrowserObservation:
        session_id = session_id or self._session_id
        window_id = window_id or self._window_id
        page = self._page_for(tab_id)
        try:
            if condition == "load":
                self._run(page.wait_for_load_state("load",
                                                   timeout=timeout_ms),
                          timeout=timeout_ms / 1000 + 5)
            elif condition == "idle":
                self._run(page.wait_for_load_state("networkidle",
                                                   timeout=timeout_ms),
                          timeout=timeout_ms / 1000 + 5)
            elif condition.startswith("selector:"):
                self._run(page.wait_for_selector(condition[len("selector:"):],
                                                 timeout=timeout_ms),
                          timeout=timeout_ms / 1000 + 5)
            elif condition.startswith("url:"):
                prefix = condition[len("url:"):]
                self._run(page.wait_for_url(f"{prefix}*",
                                            timeout=timeout_ms),
                          timeout=timeout_ms / 1000 + 5)
            else:
                raise UnsupportedOperation(
                    f"unknown wait condition {condition!r}; use "
                    "'load', 'idle', 'selector:<css>', 'url:<prefix>'")
        except UnsupportedOperation:
            raise
        except Exception as e:
            self._health_ok(tab_id).record_failure(str(e))
            raise TimeoutError(f"wait_for {condition!r} timed out: {e}")
        return self.observe(tab_id=tab_id, frame_id=frame_id,
                            session_id=session_id, window_id=window_id)

    # -- inspection -------------------------------------------------------------
    def screenshot(self, *, tab_id: str, frame_id: str = MAIN_FRAME,
                   session_id: str | None = None,
                   window_id: str | None = None) -> bytes:
        page = self._page_for(tab_id)
        try:
            png = self._run(page.screenshot(type="png"), timeout=30)
        except Exception as e:
            self._health_ok(tab_id).record_failure(str(e))
            raise
        self._health_ok(tab_id).record_success()
        return bytes(png)

    def inspect_network(self, *, tab_id: str,
                        session_id: str | None = None,
                        since_ts: float = 0.0) -> list[dict]:
        self._ensure_live(tab_id)
        return [e for e in self._network.get(tab_id, [])
                if e.get("ts", 0) >= since_ts]

    def inspect_dialogs(self, *, tab_id: str,
                        session_id: str | None = None) -> list[dict]:
        self._ensure_live(tab_id)
        return list(self._dialogs.get(tab_id, []))

    def inspect_downloads(self, *, tab_id: str,
                          session_id: str | None = None) -> list[dict]:
        self._ensure_live(tab_id)
        return list(self._downloads.get(tab_id, []))

    def dismiss_dialog(self, tab_id: str) -> dict:
        """Return the recorded open dialogs for a tab.

        Interactive dismissal is deliberately not part of the
        deterministic primitive set: dialogs are page-blocking modal
        state, and the runtime treats them as first-class observations
        (inspect_dialogs) rather than something to click away silently.
        """
        return {"tab_id": tab_id, "dialogs": self.inspect_dialogs(tab_id=tab_id)}


def _js_literal(value) -> str:
    """Encode a Python value as a JS literal (stdlib json)."""
    import json as _json
    return _json.dumps(value, separators=(",", ":"))


def _classify_browser_error(e: Exception) -> str:
    msg = f"{type(e).__name__}: {e}".lower()
    if "timeout" in msg:
        return "TIMEOUT"
    if "closed" in msg or "crashed" in msg or "disconnected" in msg:
        return "BROWSER_NOT_READY"
    if "no such element" in msg or "not found" in msg \
            or "matched nothing" in msg:
        return "STALE_REFERENCE"
    if "ambiguous" in msg:
        return "STALE_REFERENCE"
    return "ACTION_FAILED"
