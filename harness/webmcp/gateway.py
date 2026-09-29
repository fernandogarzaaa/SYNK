"""WebMCPGateway: real model-context discovery, selection, and invocation (Stage E).

The gateway is the ONLY path through which SYNK touches a page's WebMCP
tools. It enforces the mandate's rules structurally:

* Discovery asks the live page (per tab/session) through the configured
  ``ModelContextTransport``. The old static dispatch table is never
  consulted for availability.
* Before invoking, the gateway verifies the tool is actually advertised
  by the current page's model context. A tool the page did not advertise
  fails closed with ``WEBMCP_TOOL_NOT_ADVERTISED`` -- it never falls
  through to a local table that pretends success.
* Tool handles are scoped to (session, tab, frame, document). A handle
  from another tab is rejected (``WEBMCP_SCOPE_VIOLATION``); a handle
  minted before a navigation fails closed (``WEBMCP_STALE_HANDLE``)
  because the document identity changed.
* Capability selection is deterministic and recorded: every selection
  round writes a ``SelectionRecord`` (candidates, scores, winner,
  rationale) to the audit trail.
* The PARTIAL fallback (built-in fixture table) exists only for pages
  with NO model context at all, only when explicitly enabled, and every
  use is caveated in the audit trail. Fixture results can never verify
  a claim -- the verifier refuses ``partial_fallback`` evidence.

Invocation returns a ``GatewayInvocation`` carrying the ``ToolResult``
plus routing metadata (handle, partial flag, typed error code). The
transaction engine (via ``ToolExecutor``) records the result as
``WEBMCP_RESULT`` evidence and verifies the ``webmcp_result``
postcondition against it.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .fallback import FALLBACK_CAVEAT, LEGACY_FALLBACK_TABLE
from .schema import ToolResult, WebMCPTool
from .scope import (WebMCPScope, current_document_id, scope_for_tab)
from .selection import CapabilitySelector, SelectionRecord
from .transport import (ModelContextTransport, UnsupportedOperation,
                        WebMCPTransportError)
from ..session import new_id


# -- typed gateway errors (surfaced through the transaction error taxonomy) ---
WEBMCP_TOOL_NOT_ADVERTISED = "WEBMCP_TOOL_NOT_ADVERTISED"
WEBMCP_UNAVAILABLE = "WEBMCP_UNAVAILABLE"
WEBMCP_STALE_HANDLE = "WEBMCP_STALE_HANDLE"
WEBMCP_SCOPE_VIOLATION = "WEBMCP_SCOPE_VIOLATION"
WEBMCP_SCHEMA_INVALID = "WEBMCP_SCHEMA_INVALID"


@dataclass
class WebMCPHandle:
    """A bound right to invoke one advertised tool in one document.

    The handle is bound to the PRINCIPAL (session) that discovered it:
    invoking with a different session identity fails closed, even if
    the handle id were somehow replayed. ``trust_level`` records how
    much the harness trusts the advertisement (page-advertised by
    default; only the operator can raise it).
    """
    handle_id: str
    tool_name: str
    scope: WebMCPScope
    principal: str | None = None          # session_id that discovered it
    trust_level: str = "page-advertised"
    advertised_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"handle_id": self.handle_id, "tool_name": self.tool_name,
                "scope": self.scope.to_dict(),
                "principal": self.principal,
                "trust_level": self.trust_level,
                "advertised_at": self.advertised_at}


@dataclass
class GatewayInvocation:
    """Outcome of one gateway invocation attempt."""
    result: ToolResult
    handle: WebMCPHandle | None = None
    partial: bool = False          # True only for the PARTIAL fixture fallback
    caveat: str = ""
    error_code: str | None = None  # transaction-taxonomy code for failures

    @property
    def ok(self) -> bool:
        return self.result.ok

    def to_dict(self) -> dict:
        d = {"ok": self.result.ok, "result": self.result.result,
             "error": self.result.error, "partial": self.partial,
             "error_code": self.error_code,
             "handle": self.handle.to_dict() if self.handle else None}
        if self.caveat:
            d["caveat"] = self.caveat
        return d


def _check_arg_types(args: dict, schema: dict) -> str | None:
    """Minimal input validation against the tool's input_schema.

    Checks required presence plus basic JSON types. This is not a full
    JSON-Schema validator by design: unknown keywords are ignored, and
    anything not provably valid fails closed.
    """
    if not isinstance(args, dict):
        return "args must be an object"
    schema = schema or {}
    for p in (schema.get("required") or []):
        if p not in args:
            return f"missing required param '{p}'"
    props = schema.get("properties") or {}
    for k, v in args.items():
        spec = props.get(k)
        if not isinstance(spec, dict) or "type" not in spec:
            continue
        want = spec["type"]
        ok = ((want == "string" and isinstance(v, str))
              or (want == "number" and isinstance(v, (int, float))
                  and not isinstance(v, bool))
              or (want == "integer" and isinstance(v, int)
                  and not isinstance(v, bool))
              or (want == "boolean" and isinstance(v, bool))
              or (want == "object" and isinstance(v, dict))
              or (want == "array" and isinstance(v, list)))
        if not ok:
            return f"param '{k}' must be {want}"
    return None


class WebMCPGateway:
    """Real WebMCP gateway over a ModelContextTransport."""

    def __init__(self, transport: ModelContextTransport, sessions,
                 *, emit: Callable[[str, dict], Any] | None = None,
                 allow_fallback: bool = False,
                 fallback_table: dict[str, dict] | None = None,
                 policy_engine=None,
                 registry=None):
        self.transport = transport
        self.sessions = sessions
        self.emit = emit or (lambda t, d: None)
        self.allow_fallback = bool(allow_fallback)
        # The opt-in flag wires the legacy fixture table by default; an
        # explicit table may override it. Without opt-in there is no
        # fallback at all.
        self.fallback_table = None
        if self.allow_fallback:
            self.fallback_table = (fallback_table if fallback_table
                                   is not None else default_fallback_table())
        self.policy_engine = policy_engine
        self.registry = registry
        self.selector = CapabilitySelector()
        # scope_key -> {"tools": [normalized], "document_id": str,
        #               "discovered_at": float, "origin": str}
        self._advertised: dict[str, dict] = {}
        # handle_id -> WebMCPHandle
        self._handles: dict[str, WebMCPHandle] = {}

    # -- scoping ---------------------------------------------------------------
    def scope_for(self, session_id: str | None, tab_id: str,
                  frame_id: str = "main") -> WebMCPScope | None:
        return scope_for_tab(self.sessions, tab_id, frame_id, session_id)

    # -- discovery ---------------------------------------------------------------
    def discover(self, scope: WebMCPScope) -> dict:
        """Discover the page's advertised tools for this scope.

        Results are cached per (scope, document): a repeated call for the
        same live document does not re-probe the page; a navigation (new
        document_id) re-discovers. Every discovery round is emitted to the
        audit trail.
        """
        if scope is None:
            return {"ok": False, "available": False,
                    "reason": "unknown tab: no scope (push a snapshot first)"}
        cached = self._advertised.get(scope.key())
        current_doc = current_document_id(self.sessions, scope)
        if cached and (not current_doc or
                       cached.get("document_id") == current_doc):
            return self._discovery_report(scope, cached)
        try:
            res = self.transport.discover(scope)
        except (WebMCPTransportError, UnsupportedOperation) as e:
            return {"ok": False, "available": False,
                    "reason": str(e), "scope": scope.to_dict()}
        except Exception as e:  # fail closed on transport bugs
            return {"ok": False, "available": False,
                    "reason": f"webmcp_error: {e}", "scope": scope.to_dict()}
        entry = {"tools": res.tools,
                 "available": bool(res.available),
                 "document_id": scope.document_id or current_doc,
                 "discovered_at": time.time(),
                 "origin": self._origin_for(scope),
                 "reason": res.reason}
        # Stage F: tool advertisements are untrusted page data. Quarantine
        # (drop + record) any advertisement that fails validation instead
        # of letting it reach selection or invocation.
        entry["tools"], entry["quarantined"] = \
            self._quarantine_tools(scope, res.tools)
        self._advertised[scope.key()] = entry
        self._register_capabilities(entry)
        self._mint_handles(scope, entry["tools"])
        self.emit("webmcp.discovered",
                  {"scope": scope.to_dict(), "available": res.available,
                   "tool_count": len(entry["tools"]),
                   "quarantined": len(entry["quarantined"]),
                   "reason": res.reason})
        return self._discovery_report(scope, entry, available=res.available)

    def _quarantine_tools(self, scope: WebMCPScope,
                          tools: list[dict]) -> tuple[list[dict], list[dict]]:
        """Validate page-advertised tools; quarantine failures.

        Returns (clean_tools, quarantine_records). Every quarantine is
        emitted to the audit trail with a stable quarantine id."""
        from ..contamination import (sanitize_tool_advertisement,
                                    quarantine_id)
        clean, quarantined = [], []
        for t in tools or []:
            tool, reason = sanitize_tool_advertisement(
                t if isinstance(t, dict) else {})
            if tool is None:
                raw_name = (t.get("name") if isinstance(t, dict)
                            else repr(t))[:80]
                qid = quarantine_id(scope.key(), str(raw_name), reason)
                rec = {"quarantine_id": qid, "name": raw_name,
                       "reason": reason}
                quarantined.append(rec)
                self.emit("security.quarantine",
                          {"quarantine_id": qid, "kind": "tool_advertisement",
                           "scope": scope.to_dict(), "name": raw_name,
                           "reason": reason})
            else:
                clean.append(tool)
        return clean, quarantined

    def _discovery_report(self, scope: WebMCPScope, entry: dict,
                          available: bool | None = None) -> dict:
        tools = entry.get("tools", [])
        # Cached entries carry the page-reported availability explicitly:
        # a model context with zero tools is available, NOT unavailable.
        avail = (available if available is not None
                 else bool(entry.get("available", False)))
        return {"ok": True, "available": avail,
                "tools": tools,
                "handles": {t["name"]: scope.handle_id_for(t["name"])
                            for t in tools},
                "scope": scope.to_dict(),
                "reason": entry.get("reason", "") if not avail else ""}

    def advertised_tools(self, scope: WebMCPScope) -> list[dict]:
        entry = self._advertised.get(scope.key()) if scope else None
        if entry is None:
            return []
        current_doc = current_document_id(self.sessions, scope)
        if current_doc and entry.get("document_id") != current_doc:
            return []  # stale after navigation: nothing is advertised
        return list(entry.get("tools", []))

    def _origin_for(self, scope: WebMCPScope) -> str:
        # Stage F: one canonical origin representation everywhere. The
        # policy registry keys origins by bare host (no scheme, no port,
        # lowercased) via policy.origin_of_url; this method must return
        # exactly that form so registrations match discovery.
        from ..policy import origin_of_url
        try:
            url = self.sessions.tab_url(scope.tab_id,
                                        session_id=scope.session_id) or ""
            return origin_of_url(url)
        except Exception:
            return ""

    def _register_capabilities(self, entry: dict) -> None:
        """Mirror advertised tools into the capability registry.

        Best-effort hint for the execution ladder; the scope-bound handle
        set remains authoritative at invoke time. Previous webmcp entries
        for the origin are replaced so removals are honest.
        """
        if self.registry is None:
            return
        from .registry import Capability
        origin = entry.get("origin", "")
        try:
            for c in self.registry.get_for_origin(origin):
                if c.source == "webmcp":
                    self.registry.unregister(origin, c.name)
            for t in entry.get("tools", []):
                self.registry.register(
                    Capability.from_webmcp(origin, WebMCPTool.from_dict(t)))
        except Exception:
            pass

    def _mint_handles(self, scope: WebMCPScope, tools: list[dict]) -> None:
        for t in tools:
            hid = scope.handle_id_for(t["name"])
            self._handles[hid] = WebMCPHandle(
                handle_id=hid, tool_name=t["name"], scope=scope,
                principal=scope.session_id, trust_level="page-advertised")

    # -- selection ---------------------------------------------------------------
    def select(self, goal: str, scope: WebMCPScope,
               *, max_risk: str = "critical") -> SelectionRecord:
        """Rank advertised tools for a goal; record the decision."""
        tools = self.advertised_tools(scope)
        if not tools and scope is not None:
            # Lazy discovery on first selection round.
            self.discover(scope)
            tools = self.advertised_tools(scope)
        rec = self.selector.select(goal, tools,
                                   scope_key=scope.key() if scope else "",
                                   max_risk=max_risk)
        self.emit("webmcp.selected", rec.to_dict())
        return rec

    # -- invocation ---------------------------------------------------------------
    def invoke(self, tool_name: str, args: dict | None,
               scope: WebMCPScope, *,
               task_id: str | None = None,
               action_id: str | None = None,
               user_consented: bool = False,
               goal: str = "",
               agent_lease: str | None = None) -> GatewayInvocation:
        """Invoke an advertised tool through the page's model context.

        Fail-closed order: unknown scope -> stale document -> not
        advertised -> schema -> policy -> transport. The fixture fallback
        is consulted ONLY when the page exposes no model context at all.
        """
        args = args or {}
        if scope is None:
            return self._fail(
                f"{WEBMCP_SCOPE_VIOLATION}: unknown tab/session -- push a "
                "snapshot first",
                "TOOL_NOT_FOUND", scope, tool_name, task_id, action_id)
        if not tool_name:
            return self._fail(
                "WEBMCP_TOOL_NOT_ADVERTISED: no tool name",
                "TOOL_NOT_FOUND", scope, tool_name, task_id, action_id)
        # Stage F: the tool name comes from the agent/planner, but the
        # namespace is page-defined: reject anything that is not a clean
        # identifier before it touches discovery state.
        from ..contamination import (TOOL_NAME_RE, sanitize_tool_result,
                                    validate_input_schema)
        if not TOOL_NAME_RE.match(tool_name):
            inv = self._fail(
                f"{WEBMCP_TOOL_NOT_ADVERTISED}: tool name {tool_name!r} is "
                f"not a valid identifier",
                "TOOL_NOT_FOUND", scope, tool_name, task_id, action_id)
            self.emit("webmcp.rejected", self._audit(inv, scope, tool_name,
                                                     task_id, action_id))
            return inv

        # 1. Handle scoping: the caller's document must be the live one.
        # A scope with an unknown document_id is also stale once the
        # session knows the document: fail closed, never guess.
        current_doc = current_document_id(self.sessions, scope)
        if current_doc and scope.document_id != current_doc:
            inv = self._fail(
                f"{WEBMCP_STALE_HANDLE}: document changed since discovery "
                f"(navigation?); re-discover before invoking '{tool_name}'",
                "NAVIGATION_CHANGED", scope, tool_name, task_id, action_id)
            self.emit("webmcp.rejected", self._audit(inv, scope, tool_name,
                                                     task_id, action_id))
            return inv

        # 2. Availability: what does THIS page advertise, right now?
        advertised = self.advertised_tools(scope)
        page_has_context: bool | None = None
        if not advertised:
            disc = self.discover(scope)
            advertised = self.advertised_tools(scope)
            page_has_context = bool(disc.get("available"))

        tool = next((t for t in advertised if t["name"] == tool_name),
                    None)
        if tool is None:
            # 3. Fail closed -- with exactly one narrow exception: the page
            #    exposes NO model context at all and the operator explicitly
            #    enabled the PARTIAL fixture fallback.
            if page_has_context is False and self._fallback_has(tool_name):
                return self._invoke_fallback(tool_name, args, scope,
                                             task_id, action_id)
            reason = (f"{WEBMCP_TOOL_NOT_ADVERTISED}: tool '{tool_name}' is "
                      f"not advertised by the page's model context "
                      f"(tab {scope.tab_id}, document "
                      f"{scope.document_id or 'unknown'})")
            if page_has_context is False:
                reason = (f"{WEBMCP_UNAVAILABLE}: page exposes no model "
                          f"context; not falling back to the fixture table "
                          f"(enable --webmcp-fallback only if you accept "
                          f"PARTIAL results)")
            inv = self._fail(reason, "TOOL_NOT_FOUND", scope, tool_name,
                             task_id, action_id)
            self.emit("webmcp.rejected", self._audit(inv, scope, tool_name,
                                                     task_id, action_id))
            return inv

        handle = self._handles.get(scope.handle_id_for(tool_name))
        if handle is None or not handle.scope.matches(scope):
            inv = self._fail(
                f"{WEBMCP_SCOPE_VIOLATION}: no live handle for '{tool_name}' "
                f"in this scope", "POLICY_DENIED", scope, tool_name,
                task_id, action_id)
            self.emit("webmcp.rejected", self._audit(inv, scope, tool_name,
                                                     task_id, action_id))
            return inv
        # Stage F: capability privilege model. The handle is bound to the
        # principal (session) that discovered it; a different principal
        # invoking it fails closed.
        if (handle.principal is not None and scope.session_id is not None
                and handle.principal != scope.session_id):
            inv = self._fail(
                f"{WEBMCP_SCOPE_VIOLATION}: handle for '{tool_name}' was "
                f"discovered by session '{handle.principal}', not "
                f"'{scope.session_id}'",
                "POLICY_DENIED", scope, tool_name, task_id, action_id)
            self.emit("webmcp.rejected", self._audit(inv, scope, tool_name,
                                                     task_id, action_id))
            return inv

        # 4. Schema validation against the tool's DECLARED input_schema
        # (fail closed). Page-supplied args are untrusted data.
        schema_err = validate_input_schema(args, tool.get("input_schema"))
        if schema_err:
            inv = self._fail(
                f"{WEBMCP_SCHEMA_INVALID}: {schema_err}",
                "SCHEMA_INVALID", scope, tool_name, task_id, action_id)
            self.emit("webmcp.rejected", self._audit(inv, scope, tool_name,
                                                     task_id, action_id))
            return inv

        # 5. Policy (ownership / consent / safety) when wired.
        if self.policy_engine is not None:
            cap = self._capability_for(tool, scope)
            decision = self.policy_engine.check(
                cap, goal, user_consented, agent_lease=agent_lease)
            if not decision.allowed:
                code = ("CONSENT_REQUIRED" if decision.requires_consent
                        else "POLICY_DENIED")
                prefix = ("REQUIRES_CONSENT" if decision.requires_consent
                          else "POLICY_DENIED")
                inv = self._fail(f"{prefix}: {decision.reason}", code,
                                 scope, tool_name, task_id, action_id)
                self.emit("webmcp.rejected",
                          self._audit(inv, scope, tool_name, task_id,
                                      action_id))
                return inv

        # 6. Real invocation through the page's model context.
        try:
            result = self.transport.invoke(handle, args)
        except UnsupportedOperation as e:
            inv = self._fail(str(e), "BROWSER_NOT_READY", scope, tool_name,
                             task_id, action_id)
            self.emit("webmcp.rejected", self._audit(inv, scope, tool_name,
                                                     task_id, action_id))
            return inv
        except Exception as e:  # transport bugs fail closed
            result = ToolResult(new_id("wmc"), False,
                                error=f"webmcp_error: {e}")
        # Stage F: the page's result is untrusted data. Redact secrets and
        # cap its size before it enters the evidence pipeline; the raw
        # page bytes are never stored or re-dispatched.
        from ..contamination import classify_text, quarantine_id
        clean = sanitize_tool_result(result.result)
        probe = clean if isinstance(clean, str) else json.dumps(
            clean, default=str, ensure_ascii=False)
        scan = classify_text(probe)
        if scan["injection"]:
            qid = quarantine_id(scope.key(), tool_name, probe)
            self.emit("security.quarantine",
                      {"quarantine_id": qid, "source": "webmcp_result",
                       "tool_name": tool_name,
                       "scope": scope.to_dict(),
                       "markers": scan["markers"]})
            clean = ("[QUARANTINED: instruction-like content in tool "
                     f"result removed; id {qid}]")
        result = ToolResult(result.request_id, result.ok,
                            result=clean, error=result.error)
        inv = GatewayInvocation(result=result, handle=handle,
                                error_code=None if result.ok
                                else "ACTION_FAILED")
        self.emit("webmcp.invoked",
                  {**self._audit(inv, scope, tool_name, task_id, action_id),
                   "ok": result.ok})
        return inv

    # -- fallback (PARTIAL, opt-in) --------------------------------------------------
    def _fallback_has(self, tool_name: str) -> bool:
        return (self.allow_fallback and self.fallback_table is not None
                and tool_name in self.fallback_table)

    def _invoke_fallback(self, tool_name: str, args: dict,
                         scope: WebMCPScope, task_id: str | None,
                         action_id: str | None) -> GatewayInvocation:
        result = ToolResult(new_id("wmc"), True,
                            result=dict(self.fallback_table[tool_name]))
        inv = GatewayInvocation(result=result, handle=None, partial=True,
                                caveat=FALLBACK_CAVEAT)
        self.emit("webmcp.partial_fallback",
                  {**self._audit(inv, scope, tool_name, task_id, action_id),
                   "caveat": FALLBACK_CAVEAT})
        return inv

    # -- helpers -----------------------------------------------------------------------
    def _capability_for(self, tool: dict, scope: WebMCPScope):
        from .registry import Capability
        handle = self._handles.get(scope.handle_id_for(tool["name"]))
        return Capability.from_webmcp(
            self._origin_for(scope), WebMCPTool.from_dict(tool),
            discovered_by=(handle.principal if handle else scope.session_id),
            trust_level=(handle.trust_level if handle else "page-advertised"))

    @staticmethod
    def _fail(reason: str, code: str, scope: WebMCPScope | None,
              tool_name: str, task_id: str | None,
              action_id: str | None) -> GatewayInvocation:
        return GatewayInvocation(
            result=ToolResult(new_id("wmc"), False, error=reason),
            error_code=code)

    @staticmethod
    def _audit(inv: GatewayInvocation, scope: WebMCPScope | None,
               tool_name: str, task_id: str | None,
               action_id: str | None) -> dict:
        return {"tool": tool_name, "task_id": task_id,
                "action_id": action_id,
                "scope": scope.to_dict() if scope else None,
                "ok": inv.result.ok, "error": inv.result.error,
                "error_code": inv.error_code, "partial": inv.partial}


def default_fallback_table() -> dict[str, dict]:
    """The legacy fixture table (only meaningful with --webmcp-fallback)."""
    return dict(LEGACY_FALLBACK_TABLE)


# Re-exported for callers that build ToolCalls against the gateway.
__all__ = ["WebMCPGateway", "WebMCPHandle", "GatewayInvocation",
           "CapabilitySelector", "SelectionRecord",
           "WEBMCP_TOOL_NOT_ADVERTISED", "WEBMCP_UNAVAILABLE",
           "WEBMCP_STALE_HANDLE", "WEBMCP_SCOPE_VIOLATION",
           "WEBMCP_SCHEMA_INVALID", "default_fallback_table"]
