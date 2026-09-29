"""Model-context transport (Stage E): the real WebMCP wire protocol.

WebMCP (W3C "Model Context Protocol for the Web" proposal shape) exposes a
page's tools through ``navigator.modelContext``: the page advertises what
the model may call, and the model invokes tools through the page. SYNK
never trusts a static table for this: discovery asks the live page, per
tab/session, and invocation goes back through the page's own model
context.

Transports (all implement ``ModelContextTransport``):

* ``CdpModelContextTransport`` -- REAL code path (UNVERIFIED against a
  live page: no ``navigator.modelContext`` existed in the build
  environment). Evaluates the probe/invoke JS in the page through the
  SYNK-owned browser (Playwright ``page.evaluate``).
  Used in ``--use-cdp`` / ``--cdp-endpoint`` mode.
* ``ExtensionSnapshotTransport`` -- REAL (page-reported, async). Reads the
  tool list the extension content script observed in the page and pushed
  with its snapshot. Invocation is NOT supported here: the harness cannot
  synchronously drive the user's attached browser; WebMCP tools in
  extension mode are invoked through the closed-loop agent
  (``/agent/*`` -> content script ``WEBMCP_INVOKE``). ``invoke()`` raises
  ``UnsupportedOperation`` saying exactly that.
* ``FakeModelContextTransport`` -- explicit TEST DOUBLE. Scripted tools
  and results for the regression suite. It is never constructed outside
  tests; nothing labels its output as a live page.

The old static dispatch table (``WebMCPAdapter._execute_tool``'s mock
results) is NOT a transport. It survives only as an explicit, opt-in
PARTIAL fallback (see ``fallback.py``) for pages with no model context
at all, and every fallback use is caveated in the audit trail.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from .schema import ToolResult
from ..session import new_id


# -- typed transport errors ------------------------------------------------------
class WebMCPTransportError(Exception):
    """Base for transport failures."""


class WebMCPUnavailable(WebMCPTransportError):
    """The page exposes no model context (honest, not a fallback)."""


class UnsupportedOperation(WebMCPTransportError):
    """The transport cannot perform this operation by architecture."""


# -- discovery result --------------------------------------------------------------
@dataclass
class WebMCPDiscoveryResult:
    """What the page said about its model context."""
    available: bool
    tools: list[dict] = field(default_factory=list)  # normalized tool dicts
    reason: str = ""          # set when not available / on error
    scope_key: str = ""

    def to_dict(self) -> dict:
        return {"available": self.available,
                "tools": self.tools,
                "reason": self.reason,
                "scope_key": self.scope_key}


def normalize_tool_dict(t: Any) -> dict | None:
    """Normalize one advertised tool to the canonical dict shape.

    Returns None for malformed entries (they are dropped, never trusted).
    """
    if not isinstance(t, dict):
        return None
    name = t.get("name")
    if not name or not isinstance(name, str):
        return None
    schema = t.get("input_schema") or t.get("inputSchema") or {}
    if not isinstance(schema, dict):
        schema = {}
    ann = t.get("annotations") or {}
    if not isinstance(ann, dict):
        ann = {}
    return {
        "name": name,
        "description": str(t.get("description", "")),
        "input_schema": schema,
        "annotations": {
            "readOnlyHint": bool(ann.get("readOnlyHint", False)),
            "idempotentHint": bool(ann.get("idempotentHint", False)),
            "destructiveHint": bool(ann.get("destructiveHint", False)),
            "openWorldHint": bool(ann.get("openWorldHint", False)),
        },
    }


# -- page-side probe (runs INSIDE the page) ------------------------------------------
# Defensive against proposal-shape drift: tries availableTools(), then a
# .tools array, then <script type="webmcp-tool"> declarations. Anything
# else -> available:false with an honest reason. Never throws out.
MODEL_CONTEXT_PROBE_JS = """(async () => {
  const out = { available: false, tools: [], reason: "" };
  const norm = (t) => {
    if (!t || typeof t !== "object" || typeof t.name !== "string" || !t.name) return null;
    const ann = (t.annotations && typeof t.annotations === "object") ? t.annotations : {};
    return {
      name: t.name,
      description: String(t.description || ""),
      input_schema: (t.input_schema && typeof t.input_schema === "object") ? t.input_schema
                  : (t.inputSchema && typeof t.inputSchema === "object") ? t.inputSchema : {},
      annotations: {
        readOnlyHint: !!ann.readOnlyHint,
        idempotentHint: !!ann.idempotentHint,
        destructiveHint: !!ann.destructiveHint,
        openWorldHint: !!ann.openWorldHint,
      },
    };
  };
  try {
    const mc = (typeof navigator !== "undefined") ? navigator.modelContext : undefined;
    if (!mc) { out.reason = "webmcp_unavailable: navigator.modelContext not present"; return out; }
    let raw = null;
    if (typeof mc.availableTools === "function") {
      raw = await mc.availableTools();
    } else if (Array.isArray(mc.tools)) {
      raw = mc.tools;
    } else {
      const tags = Array.from(document.querySelectorAll('script[type="webmcp-tool"]'));
      raw = tags.map((el) => { try { return JSON.parse(el.textContent); } catch (e) { return null; } })
                 .filter(Boolean);
      if (!raw.length) {
        out.reason = "webmcp_unavailable: modelContext present but exposes no tool listing API";
        return out;
      }
    }
    const list = Array.isArray(raw) ? raw : [];
    out.tools = list.map(norm).filter(Boolean);
    out.available = true;
  } catch (e) {
    out.reason = "webmcp_error: " + String((e && e.message) || e);
  }
  return out;
})()"""

# Invoke through the page's model context. Playwright passes one arg object.
# NOTE: this must be a FUNCTION EXPRESSION, not an IIFE: Playwright
# calls the evaluated function with the arg object. An IIFE would run
# with undefined args (found by live-browser verification; the fake
# transport never evaluates this JS).
MODEL_CONTEXT_INVOKE_JS = """(async ({ toolName, args }) => {
  try {
    const mc = (typeof navigator !== "undefined") ? navigator.modelContext : undefined;
    if (!mc || typeof mc.invokeTool !== "function") {
      return { ok: false, error: "webmcp_unavailable: modelContext.invokeTool not present" };
    }
    const result = await mc.invokeTool(toolName, args || {});
    return { ok: true, result: (result === undefined ? null : result) };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
})"""


# -- transport interface ---------------------------------------------------------------
class ModelContextTransport(ABC):
    """How the gateway talks to a page's model context."""

    @abstractmethod
    def discover(self, scope) -> WebMCPDiscoveryResult:
        """Ask the page what tools its model context advertises."""

    @abstractmethod
    def invoke(self, handle, args: dict) -> ToolResult:
        """Invoke an advertised tool through the page's model context."""


class CdpModelContextTransport(ModelContextTransport):
    """REAL transport over the SYNK-owned browser.

    ``evaluate`` is ``(tab_id, frame_id, js, js_arg) -> Any``; the server
    wires it to ``OwnedBrowserRuntime.evaluate_js``. Every discovery and
    invocation runs the probe JS inside the live page.

    UNVERIFIED against a live page: no ``navigator.modelContext`` was
    available in the build environment, so this path has only been
    exercised through the ``FakeModelContextTransport`` test double at
    the gateway layer.
    """

    def __init__(self, evaluate: Callable[..., Any]):
        self._evaluate = evaluate

    def discover(self, scope) -> WebMCPDiscoveryResult:
        try:
            raw = self._evaluate(scope.tab_id, scope.frame_id,
                                 MODEL_CONTEXT_PROBE_JS, None)
        except Exception as e:
            return WebMCPDiscoveryResult(
                available=False, reason=f"webmcp_error: {e}",
                scope_key=scope.key())
        if not isinstance(raw, dict):
            return WebMCPDiscoveryResult(
                available=False,
                reason="webmcp_error: probe returned non-object",
                scope_key=scope.key())
        tools = [t for t in (normalize_tool_dict(x)
                             for x in (raw.get("tools") or [])) if t]
        # The page's availability report is authoritative: a model
        # context with zero tools is available, NOT unavailable.
        return WebMCPDiscoveryResult(
            available=bool(raw.get("available")),
            tools=tools,
            reason="" if raw.get("available") else str(
                raw.get("reason") or "webmcp_unavailable"),
            scope_key=scope.key())

    def invoke(self, handle, args: dict) -> ToolResult:
        request_id = new_id("wmc")
        try:
            raw = self._evaluate(handle.scope.tab_id, handle.scope.frame_id,
                                 MODEL_CONTEXT_INVOKE_JS,
                                 {"toolName": handle.tool_name,
                                  "args": args or {}})
        except Exception as e:
            return ToolResult(request_id, False,
                              error=f"webmcp_error: {e}")
        if not isinstance(raw, dict):
            return ToolResult(request_id, False,
                              error="webmcp_error: invoke returned non-object")
        if raw.get("ok"):
            return ToolResult(request_id, True, result=raw.get("result"))
        return ToolResult(request_id, False,
                          error=str(raw.get("error") or "tool reported failure"))


class ExtensionSnapshotTransport(ModelContextTransport):
    """Page-REPORTED tools via extension snapshot pushes (attached mode).

    The content script probes ``navigator.modelContext`` in the page and
    pushes the result with every snapshot; this transport reads that
    cache. Discovery is real (the page said it); invocation is architecturally
    impossible here -- the harness cannot synchronously call into the
    user's attached browser -- so ``invoke()`` raises UnsupportedOperation
    naming the supported path (the closed-loop agent's WEBMCP_INVOKE).
    """

    def __init__(self, get_snapshot_tools: Callable[[str], dict | None]):
        # get_snapshot_tools(scope_key) ->
        #   {"available": bool, "tools": [...], "reason": str} | None
        self._get = get_snapshot_tools

    def discover(self, scope) -> WebMCPDiscoveryResult:
        try:
            cached = self._get(scope.key())
        except Exception as e:
            return WebMCPDiscoveryResult(
                available=False, reason=f"webmcp_error: {e}",
                scope_key=scope.key())
        if not cached:
            return WebMCPDiscoveryResult(
                available=False,
                reason="webmcp_unavailable: no snapshot from the page yet; "
                       "push a snapshot (or run the agent loop) first",
                scope_key=scope.key())
        tools = [t for t in (normalize_tool_dict(x)
                             for x in (cached.get("tools") or [])) if t]
        # The cache carries the page-reported availability explicitly: a
        # model context with zero tools is available, NOT unavailable.
        # Legacy cache entries without the flag fall back to bool(tools).
        available = (cached.get("available") if "available" in cached
                     else bool(tools))
        if not available:
            return WebMCPDiscoveryResult(
                available=False,
                reason=str(cached.get("reason") or "webmcp_unavailable"),
                scope_key=scope.key())
        return WebMCPDiscoveryResult(available=True, tools=tools,
                                     scope_key=scope.key())

    def invoke(self, handle, args: dict) -> ToolResult:
        raise UnsupportedOperation(
            "extension mode cannot invoke WebMCP tools synchronously: the "
            "harness cannot call into the user's attached browser. Use the "
            "closed-loop agent (/agent/next returns a webmcp_invoke action; "
            "the extension content script performs WEBMCP_INVOKE in-page), "
            "or run the SYNK-owned browser (--use-cdp).")


class FakeModelContextTransport(ModelContextTransport):
    """Explicit TEST DOUBLE for the regression suite.

    Scripted per-tool results; records every call. Never constructed
    outside tests -- nothing about its output is presented as a live page.
    """

    def __init__(self, tools: list[dict] | None = None,
                 results: dict[str, dict] | None = None,
                 *, available: bool = True,
                 reason: str = "test-double: no model context scripted"):
        self._tools = [t for t in (normalize_tool_dict(x)
                                   for x in (tools or [])) if t]
        self._results = results or {}
        # Honor the scripted flag exactly: a page can expose a model
        # context with zero advertised tools (available=True, tools=[]).
        # That state must NOT enable the fixture fallback.
        self._available = bool(available)
        self._reason = reason
        self.discover_calls: list = []
        self.invoke_calls: list = []

    def discover(self, scope) -> WebMCPDiscoveryResult:
        self.discover_calls.append(scope.key())
        return WebMCPDiscoveryResult(
            available=self._available, tools=list(self._tools),
            reason="" if self._available else self._reason,
            scope_key=scope.key())

    def invoke(self, handle, args: dict) -> ToolResult:
        self.invoke_calls.append((handle.handle_id, handle.tool_name,
                                  dict(args or {})))
        request_id = new_id("wmc")
        scripted = self._results.get(handle.tool_name)
        if scripted is None:
            return ToolResult(request_id, False,
                              error="test-double: no scripted result for "
                                    f"{handle.tool_name}")
        if scripted.get("ok"):
            return ToolResult(request_id, True,
                              result=scripted.get("result"))
        return ToolResult(request_id, False,
                          error=str(scripted.get("error") or "scripted failure"))
