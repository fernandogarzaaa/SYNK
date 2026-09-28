"""Robust browser interaction primitives (Stage D / mandate Phase 7).

Replaces the fragile ``el.value = ...; el.click(); dispatchEvent(...)``
pattern with primitives that work against real-world pages:

* text entry through the element's NATIVE value setter (the trick that
  makes React/Vue/Svelte controlled inputs actually update), with proper
  focus + input + change events;
* contenteditable via selection + insertText;
* select / checkbox / radio through their real control APIs;
* keyboard interaction as full keydown/keypress/keyup sequences;
* shadow-DOM piercing and frame-chain targeting for ref resolution;
* focus/blur handling around every mutation.

The JavaScript core (``INTERACTION_JS``) is the single source of truth. It
is injected by the CDP/Playwright adapter via ``page.evaluate`` AND
mirrored into ``extension/content.js`` (search for
``MIRRORED FROM harness/interactions.py``); the two must be kept in sync
because the extension is a static file that cannot import Python.

Every primitive returns an observation of the POST-state -- the system
distinguishes "command accepted" from "browser interaction actually
succeeded" structurally: success is only reported together with the
observed post-state that the verifier will judge.

Stdlib only.
"""
from __future__ import annotations

import time

from .browser_runtime import (BrowserAck, BrowserObservation, ElementTarget,
                              observation_id_for)
from .session import MAIN_FRAME, new_id

# ---------------------------------------------------------------------------
# Shared JavaScript core.
#
# Defines window.__synk = { resolveTarget, observeTarget, robustType,
# robustClick, robustSelect, robustCheck, pressKey, scrollBy }.
#
# resolveTarget(spec): spec = {frameId, frameChain, shadowPath, locator}
#   - frame matching is done by the caller (each content-script instance
#     only acts when spec.frameId === its own frame id; the CDP adapter
#     uses page.frame() for the chain). resolveTarget handles the
#     shadow_path INSIDE its document: each entry is a CSS selector for a
#     shadow host whose shadowRoot becomes the next search root.
#   - locator: {strategy, value}; strategies: "test-id" (data-testid),
#     "css-id" (#id), "css", "xpath".
# ---------------------------------------------------------------------------
INTERACTION_JS = r"""(function () {
  if (window.__synk) return window.__synk;

  function byLocator(root, locator) {
    var strategy = locator && locator.strategy;
    var value = locator && locator.value;
    if (!value) return [];
    if (strategy === "test-id") {
      return Array.prototype.slice.call(
        root.querySelectorAll('[data-testid="' + value.replace(/"/g, '\\"') + '"]'));
    }
    if (strategy === "xpath") {
      var out = [];
      try {
        var it = document.evaluate(value, root, null,
          XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
        for (var i = 0; i < it.snapshotLength; i++) out.push(it.snapshotItem(i));
      } catch (e) { /* invalid xpath -> no match (fail closed upstream) */ }
      return out;
    }
    // "css-id" and "css" both evaluate as CSS selectors.
    try { return Array.prototype.slice.call(root.querySelectorAll(value)); }
    catch (e) { return []; }
  }

  function resolveTarget(spec) {
    spec = spec || {};
    var root = document;
    var shadowPath = spec.shadowPath || spec.shadow_path || [];
    for (var i = 0; i < shadowPath.length; i++) {
      var host = root.querySelector(shadowPath[i]);
      if (!host || !host.shadowRoot) {
        return { ok: false, error: "shadow host not found: " + shadowPath[i] };
      }
      root = host.shadowRoot;
    }
    var locator = spec.locator || {};
    if (locator.value) {
      var els = byLocator(root, locator);
      if (els.length === 0) return { ok: false, error: "no such element" };
      if (els.length > 1) {
        return { ok: false, error: "ambiguous locator: " + els.length + " matches",
                 errorCode: "AMBIGUOUS_ELEMENT" };
      }
      return { ok: true, el: els[0] };
    }
    return { ok: false, error: "no locator in target" };
  }

  function isVisible(el) {
    try {
      var r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0 &&
        window.getComputedStyle(el).visibility !== "hidden";
    } catch (e) { return false; }
  }

  function observeTarget(el) {
    var tag = "", type = "";
    try { tag = (el.tagName || "").toLowerCase(); type = (el.type || "").toLowerCase(); } catch (e) {}
    var st = { tag: tag, type: type, visible: isVisible(el),
               disabled: !!el.disabled, url: location.href };
    try {
      if (tag === "input" || tag === "textarea") {
        if (type === "checkbox" || type === "radio") st.checked = !!el.checked;
        else if (type !== "password") st.value = el.value == null ? "" : String(el.value);
        else st.value = ""; // passwords are never read back
      } else if (tag === "select") {
        var opt = el.selectedOptions && el.selectedOptions[0];
        st.value = opt ? (opt.value != null ? opt.value : opt.text) : "";
        st.selectedIndex = el.selectedIndex;
      } else if (el.isContentEditable) {
        st.value = (el.innerText || "").slice(0, 2000);
      } else {
        st.text = ((el.innerText || el.textContent || "").trim()).slice(0, 200);
      }
      st.focused = (document.activeElement === el);
    } catch (e) { st.observeError = String(e && e.message || e); }
    return st;
  }

  function fire(el, Ctor, type, init) {
    init = init || {};
    init.bubbles = init.bubbles !== false;
    init.cancelable = init.cancelable !== false;
    init.composed = true;
    var ev;
    try { ev = new Ctor(type, init); }
    catch (e) { ev = document.createEvent("Event"); ev.initEvent(type, true, true); }
    return el.dispatchEvent(ev);
  }

  // Native value setter: the key to controlled React/Vue/Svelte inputs.
  // Assigning el.value directly bypasses the framework's setter; going
  // through the prototype's native setter makes the framework see it.
  function nativeSetValue(el, value) {
    var proto = null;
    try {
      var tag = (el.tagName || "").toLowerCase();
      if (tag === "textarea") proto = window.HTMLTextAreaElement.prototype;
      else proto = window.HTMLInputElement.prototype;
      var desc = Object.getOwnPropertyDescriptor(proto, "value");
      if (desc && desc.set) { desc.set.call(el, value); return true; }
    } catch (e) { /* fall through */ }
    el.value = value; // last resort: plain assignment
    return false;
  }

  function robustType(el, text, opts) {
    opts = opts || {};
    var tag = (el.tagName || "").toLowerCase();
    var type = (el.type || "").toLowerCase();
    try { el.focus({ preventScroll: false }); } catch (e) { try { el.focus(); } catch (e2) {} }
    if (el.isContentEditable) {
      try {
        el.focus();
        var sel = window.getSelection();
        sel.selectAllChildren(el);
        var okIns = document.execCommand("insertText", false, text);
        if (!okIns) { // fallback: replace text node directly
          el.textContent = text;
          fire(el, Event, "input");
        }
        fire(el, Event, "change");
        fire(el, FocusEvent, "blur");
        return { ok: true, observed: observeTarget(el) };
      } catch (e) { return { ok: false, error: String(e && e.message || e) }; }
    }
    if (tag === "input" && (type === "checkbox" || type === "radio")) {
      return robustCheck(el, text); // text "true"/"false"/"toggle"
    }
    if (tag !== "input" && tag !== "textarea") {
      return { ok: false, error: "robustType needs input/textarea/contenteditable" };
    }
    try {
      if (opts.clearFirst !== false) {
        nativeSetValue(el, "");
        fire(el, Event, "input");
      }
      nativeSetValue(el, text);
      fire(el, Event, "input");
      fire(el, Event, "change");
      try { el.blur(); } catch (e) {}
      fire(el, FocusEvent, "blur");
      return { ok: true, observed: observeTarget(el) };
    } catch (e) { return { ok: false, error: String(e && e.message || e) }; }
  }

  function robustCheck(el, want) {
    // want: "true" | "false" | "toggle" | boolean
    try {
      var target = (want === "toggle") ? !el.checked :
                   (want === true || want === "true");
      if (el.checked !== target) {
        try { el.scrollIntoView({ block: "nearest" }); } catch (e) {}
        fire(el, PointerEvent, "pointerdown");
        fire(el, MouseEvent, "mousedown");
        fire(el, PointerEvent, "pointerup");
        fire(el, MouseEvent, "mouseup");
        fire(el, MouseEvent, "click");
        el.checked = target; // ensure end state even if handlers swallowed it
        fire(el, Event, "input");
        fire(el, Event, "change");
      }
      try { el.blur(); } catch (e) {}
      return { ok: true, observed: observeTarget(el) };
    } catch (e) { return { ok: false, error: String(e && e.message || e) }; }
  }

  function robustClick(el) {
    try { el.scrollIntoView({ block: "center", behavior: "instant" }); } catch (e) {
      try { el.scrollIntoView({ block: "center" }); } catch (e2) {}
    }
    try { el.focus({ preventScroll: true }); } catch (e) {}
    var before = location.href;
    fire(el, PointerEvent, "pointerdown");
    fire(el, MouseEvent, "mousedown");
    fire(el, PointerEvent, "pointerup");
    fire(el, MouseEvent, "mouseup");
    fire(el, MouseEvent, "click");
    var st = observeTarget(el);
    st.navigated = (location.href !== before);
    return { ok: true, observed: st };
  }

  function robustSelect(el, value) {
    var tag = (el.tagName || "").toLowerCase();
    if (tag !== "select") return { ok: false, error: "robustSelect needs <select>" };
    try {
      var idx = -1;
      for (var i = 0; i < el.options.length; i++) {
        var o = el.options[i];
        if (o.value === value || (o.text || "").trim() === (value || "").trim()) { idx = i; break; }
      }
      if (idx < 0) return { ok: false, error: "option not found: " + value };
      el.selectedIndex = idx;
      fire(el, Event, "input");
      fire(el, Event, "change");
      try { el.blur(); } catch (e) {}
      return { ok: true, observed: observeTarget(el) };
    } catch (e) { return { ok: false, error: String(e && e.message || e) }; }
  }

  function pressKey(el, key) {
    var tgt = el || document.activeElement || document.body;
    var init = { key: key, code: key.length === 1 ? "Key" + key.toUpperCase() : key,
                 bubbles: true, cancelable: true, composed: true };
    try {
      fire(tgt, KeyboardEvent, "keydown", init);
      if (key.length === 1) fire(tgt, KeyboardEvent, "keypress", init);
      fire(tgt, KeyboardEvent, "keyup", init);
      return { ok: true, observed: observeTarget(tgt) };
    } catch (e) { return { ok: false, error: String(e && e.message || e) }; }
  }

  window.__synk = {
    resolveTarget: resolveTarget,
    observeTarget: observeTarget,
    robustType: robustType,
    robustCheck: robustCheck,
    robustClick: robustClick,
    robustSelect: robustSelect,
    pressKey: pressKey,
  };
  return window.__synk;
})()
"""


# ---------------------------------------------------------------------------
# Python side: wire format + ack construction.
# ---------------------------------------------------------------------------

# Extension EXECUTE wire format (v2). The content script replies with a
# BROWSER_ACK dict; background.js relays it to /agent/report.
def build_execute_message(command: str, target: ElementTarget,
                          args: dict | None = None,
                          action_id: str | None = None) -> dict:
    """Build the EXECUTE message the extension content script understands."""
    return {
        "type": "EXECUTE",
        "command": command,
        "action_id": action_id,
        "target": {
            "session_id": target.session_id,
            "window_id": target.window_id,
            "tab_id": target.tab_id,
            "frame_id": target.frame_id,
            "frame_chain": list(target.frame_chain),
            "shadow_path": list(target.shadow_path),
            "locator": dict(target.locator),
        },
        "args": dict(args or {}),
    }


def parse_ack_payload(raw: dict) -> BrowserAck:
    """Validate a BROWSER_ACK dict from the extension/CDP adapter.

    Raises ValueError on malformed payloads (fail closed: a malformed ack
    is never treated as a successful execution).
    """
    if not isinstance(raw, dict) or not raw.get("ack"):
        raise ValueError("not a BROWSER_ACK payload")
    obs = None
    if raw.get("observed") is not None:
        o = raw["observed"]
        if not isinstance(o, dict):
            raise ValueError("BROWSER_ACK.observed must be an object")
        obs = BrowserObservation(
            observation_id=o.get("observation_id") or new_id("obs"),
            session_id=raw.get("session_id"),
            window_id=raw.get("window_id", "win_default"),
            tab_id=raw["tab_id"], frame_id=raw.get("frame_id", MAIN_FRAME),
            url=o.get("url", ""), title=o.get("title", ""),
            target_state=o.get("target_state", o),
            captured_at=o.get("captured_at", time.time()))
    return BrowserAck(
        ack_id=raw.get("ack_id") or new_id("ack"),
        action_id=raw.get("action_id"),
        session_id=raw.get("session_id"),
        window_id=raw.get("window_id", "win_default"),
        tab_id=raw["tab_id"], frame_id=raw.get("frame_id", MAIN_FRAME),
        command=raw.get("command", "?"),
        accepted=bool(raw.get("accepted", False)),
        executed=bool(raw.get("executed", False)),
        observed=obs,
        error=raw.get("error"),
        error_code=raw.get("error_code"))


def ack_for_observation(command: str, target: ElementTarget,
                        observed: dict, action_id: str | None,
                        url: str, title: str = "") -> BrowserAck:
    """Build the honest ack: executed=True is inseparable from observation."""
    obs = BrowserObservation(
        observation_id=observation_id_for(target.tab_id, target.frame_id,
                                          url),
        session_id=target.session_id, window_id=target.window_id,
        tab_id=target.tab_id, frame_id=target.frame_id,
        url=url, title=title, target_state=dict(observed))
    return BrowserAck(
        ack_id=new_id("ack"), action_id=action_id,
        session_id=target.session_id, window_id=target.window_id,
        tab_id=target.tab_id, frame_id=target.frame_id,
        command=command, accepted=True, executed=True, observed=obs)


def ack_not_executed(command: str, target: ElementTarget,
                     error: str, error_code: str,
                     action_id: str | None = None) -> BrowserAck:
    return BrowserAck(
        ack_id=new_id("ack"), action_id=action_id,
        session_id=target.session_id, window_id=target.window_id,
        tab_id=target.tab_id, frame_id=target.frame_id,
        command=command, accepted=True, executed=False,
        observed=None, error=error, error_code=error_code)
