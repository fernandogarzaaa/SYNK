/* Content script: semantic page perception + guarded execution (spec 4, 5, 9).
 * - Captures accessibility-ish snapshot (role/name/tag/selector/interactive).
 * - Runs in isolated JS world; only executes pre-defined commands from harness.
 * - Reports human activity so the agent pauses (human priority).
 * - CSP-safe: no remote scripts, no eval; messaging via chrome.runtime only.
 *
 * Stage D: the interaction core below (window.__synk) is MIRRORED FROM
 * harness/interactions.py INTERACTION_JS -- keep the two in sync. The
 * EXECUTE handler now performs a ROBUST primitive (native value setter
 * for React/Vue/Svelte inputs, contenteditable support, shadow-DOM
 * piercing, frame targeting) and replies with a real BROWSER_ACK that
 * carries the OBSERVED POST-STATE. executed=true is only ever sent
 * together with that observation -- the harness never has to take the
 * executor's word for it.
 */
(() => {
  const HARNESS = "http://127.0.0.1:18080";
  let lastSent = 0;

  // Explicit runtime identity: content scripts cannot read chrome.tabs, so
  // the background relays our tab id once at load. Every event and snapshot
  // we push carries (tab_id, frame_id); the harness never has to guess.
  let TAB_ID = null, WINDOW_ID = null;
  const FRAME_ID = (() => {
    if (window === window.top) return "main";
    let h = 0;
    const s = location.href;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
    return "sub:" + (h >>> 0).toString(16);
  })();
  try {
    chrome.runtime.sendMessage({ type: "GET_TAB_ID" }, (r) => {
      if (r) { TAB_ID = r.tab_id != null ? String(r.tab_id) : null;
               WINDOW_ID = r.window_id != null ? String(r.window_id) : null; }
    });
  } catch { /* background unreachable */ }

  function identity() {
    return { tab_id: TAB_ID || "default",
             window_id: WINDOW_ID || "win_default",
             frame_id: FRAME_ID };
  }

  // ------------------------------------------------------------------
  // Robust interaction core.
  // MIRRORED FROM harness/interactions.py INTERACTION_JS -- keep in sync.
  // ------------------------------------------------------------------
  const __synk = (() => {
    function byLocator(root, locator) {
      const strategy = locator && locator.strategy;
      const value = locator && locator.value;
      if (!value) return [];
      if (strategy === "test-id") {
        return Array.prototype.slice.call(
          root.querySelectorAll('[data-testid="' + value.replace(/"/g, '\\"') + '"]'));
      }
      if (strategy === "xpath") {
        const out = [];
        try {
          const it = document.evaluate(value, root, null,
            XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
          for (let i = 0; i < it.snapshotLength; i++) out.push(it.snapshotItem(i));
        } catch (e) { /* invalid xpath -> no match (fail closed upstream) */ }
        return out;
      }
      try { return Array.prototype.slice.call(root.querySelectorAll(value)); }
      catch (e) { return []; }
    }

    function resolveTarget(spec) {
      spec = spec || {};
      let root = document;
      const shadowPath = spec.shadowPath || spec.shadow_path || [];
      for (let i = 0; i < shadowPath.length; i++) {
        const host = root.querySelector(shadowPath[i]);
        if (!host || !host.shadowRoot) {
          return { ok: false, error: "shadow host not found: " + shadowPath[i] };
        }
        root = host.shadowRoot;
      }
      const locator = spec.locator || {};
      if (locator.value) {
        const els = byLocator(root, locator);
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
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 &&
          window.getComputedStyle(el).visibility !== "hidden";
      } catch (e) { return false; }
    }

    function observeTarget(el) {
      let tag = "", type = "";
      try { tag = (el.tagName || "").toLowerCase(); type = (el.type || "").toLowerCase(); } catch (e) {}
      const st = { tag, type, visible: isVisible(el),
                   disabled: !!el.disabled, url: location.href };
      try {
        if (tag === "input" || tag === "textarea") {
          if (type === "checkbox" || type === "radio") st.checked = !!el.checked;
          else if (type !== "password") st.value = el.value == null ? "" : String(el.value);
          else st.value = ""; // passwords are never read back
        } else if (tag === "select") {
          const opt = el.selectedOptions && el.selectedOptions[0];
          st.value = opt ? (opt.value != null ? opt.value : opt.text) : "";
          st.selectedIndex = el.selectedIndex;
        } else if (el.isContentEditable) {
          st.value = (el.innerText || "").slice(0, 2000);
        } else {
          st.text = ((el.innerText || el.textContent || "").trim()).slice(0, 200);
        }
        st.focused = (document.activeElement === el);
      } catch (e) { st.observeError = String((e && e.message) || e); }
      return st;
    }

    function fire(el, Ctor, type, init) {
      init = init || {};
      init.bubbles = init.bubbles !== false;
      init.cancelable = init.cancelable !== false;
      init.composed = true;
      let ev;
      try { ev = new Ctor(type, init); }
      catch (e) { ev = document.createEvent("Event"); ev.initEvent(type, true, true); }
      return el.dispatchEvent(ev);
    }

    // Native value setter: the key to controlled React/Vue/Svelte inputs.
    function nativeSetValue(el, value) {
      let proto = null;
      try {
        const tag = (el.tagName || "").toLowerCase();
        if (tag === "textarea") proto = window.HTMLTextAreaElement.prototype;
        else proto = window.HTMLInputElement.prototype;
        const desc = Object.getOwnPropertyDescriptor(proto, "value");
        if (desc && desc.set) { desc.set.call(el, value); return true; }
      } catch (e) { /* fall through */ }
      el.value = value; // last resort: plain assignment
      return false;
    }

    function robustType(el, text, opts) {
      opts = opts || {};
      const tag = (el.tagName || "").toLowerCase();
      const type = (el.type || "").toLowerCase();
      try { el.focus({ preventScroll: false }); } catch (e) { try { el.focus(); } catch (e2) {} }
      if (el.isContentEditable) {
        try {
          el.focus();
          const sel = window.getSelection();
          sel.selectAllChildren(el);
          const okIns = document.execCommand("insertText", false, text);
          if (!okIns) {
            el.textContent = text;
            fire(el, Event, "input");
          }
          fire(el, Event, "change");
          fire(el, FocusEvent, "blur");
          return { ok: true, observed: observeTarget(el) };
        } catch (e) { return { ok: false, error: String((e && e.message) || e) }; }
      }
      if (tag === "input" && (type === "checkbox" || type === "radio")) {
        return robustCheck(el, text);
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
      } catch (e) { return { ok: false, error: String((e && e.message) || e) }; }
    }

    function robustCheck(el, want) {
      try {
        const target = (want === "toggle") ? !el.checked :
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
      } catch (e) { return { ok: false, error: String((e && e.message) || e) }; }
    }

    function robustClick(el) {
      try { el.scrollIntoView({ block: "center", behavior: "instant" }); } catch (e) {
        try { el.scrollIntoView({ block: "center" }); } catch (e2) {}
      }
      try { el.focus({ preventScroll: true }); } catch (e) {}
      const before = location.href;
      fire(el, PointerEvent, "pointerdown");
      fire(el, MouseEvent, "mousedown");
      fire(el, PointerEvent, "pointerup");
      fire(el, MouseEvent, "mouseup");
      fire(el, MouseEvent, "click");
      const st = observeTarget(el);
      st.navigated = (location.href !== before);
      return { ok: true, observed: st };
    }

    function robustSelect(el, value) {
      const tag = (el.tagName || "").toLowerCase();
      if (tag !== "select") return { ok: false, error: "robustSelect needs <select>" };
      try {
        let idx = -1;
        for (let i = 0; i < el.options.length; i++) {
          const o = el.options[i];
          if (o.value === value || (o.text || "").trim() === (value || "").trim()) { idx = i; break; }
        }
        if (idx < 0) return { ok: false, error: "option not found: " + value };
        el.selectedIndex = idx;
        fire(el, Event, "input");
        fire(el, Event, "change");
        try { el.blur(); } catch (e) {}
        return { ok: true, observed: observeTarget(el) };
      } catch (e) { return { ok: false, error: String((e && e.message) || e) }; }
    }

    function pressKey(el, key) {
      const tgt = el || document.activeElement || document.body;
      const init = { key, code: key.length === 1 ? "Key" + key.toUpperCase() : key,
                     bubbles: true, cancelable: true, composed: true };
      try {
        fire(tgt, KeyboardEvent, "keydown", init);
        if (key.length === 1) fire(tgt, KeyboardEvent, "keypress", init);
        fire(tgt, KeyboardEvent, "keyup", init);
        return { ok: true, observed: observeTarget(tgt) };
      } catch (e) { return { ok: false, error: String((e && e.message) || e) }; }
    }

    return { resolveTarget, observeTarget, robustType, robustCheck,
             robustClick, robustSelect, pressKey };
  })();

  // Beta: fire-and-forget typed events -> harness Event Bus / WorldState.
  function sendEvent(type, data) {
    try {
      fetch(`${HARNESS}/event`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type,
          data: { url: location.href, ...identity(), ...(data || {}) } }),
      }).catch(() => {});
    } catch { /* harness offline */ }
  }

  function selector(el) {
    if (el.id) return `#${el.id}`;
    const parts = [];
    let cur = el;
    for (let i = 0; i < 4 && cur && cur !== document.body; i++) {
      let s = cur.tagName.toLowerCase();
      if (cur.className && typeof cur.className === "string") {
        const c = cur.className.trim().split(/\s+/)[0];
        if (c) s += `.${c}`;
      }
      parts.unshift(s);
      cur = cur.parentElement;
    }
    return parts.join(" > ");
  }

  function roleOf(el) {
    const explicit = el.getAttribute && el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === "button") return "button";
    if (tag === "a") return "link";
    if (tag === "input") {
      const t = (el.type || "text").toLowerCase();
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (t === "password") return "passwordbox";
      return "textbox";
    }
    if (tag === "textarea") return "textbox";
    if (tag === "select") return "combobox";
    if (tag === "form") return "form";
    if (/^h[1-6]$/.test(tag)) return "heading";
    if (tag === "img") return "image";
    return tag;
  }

  function nameOf(el) {
    return (
      el.getAttribute?.("aria-label") ||
      el.innerText?.trim().slice(0, 80) ||
      el.value ||
      el.placeholder ||
      el.title ||
      el.name ||
      ""
    ).trim().slice(0, 120);
  }

  function captureNodes() {
    const els = document.querySelectorAll(
      "a,button,input,select,textarea,form,[role],[aria-label],h1,h2,h3"
    );
    const nodes = [];
    els.forEach((el) => {
      const r = el.getBoundingClientRect();
      const tag = el.tagName.toLowerCase();
      const visible = !(r.width === 0 && r.height === 0);
      const node = {
        role: roleOf(el),
        name: nameOf(el),
        tag,
        selector: selector(el),
        interactive: ["a", "button", "input", "select", "textarea"].includes(tag),
        // Element state for fingerprinting / fail-closed ref validation.
        // Values are page state, reported as data; never trusted as instructions.
        visible,
        disabled: !!el.disabled,
        href: tag === "a" ? (el.getAttribute("href") || "") : "",
        frame_id: FRAME_ID,
      };
      if (tag === "input" || tag === "textarea") {
        const t = (el.type || "text").toLowerCase();
        if (t === "checkbox" || t === "radio") node.checked = !!el.checked;
        else if (t !== "password") node.value = el.value ?? "";
        // password values are never captured
        if (t === "password") node.value = "";
        node.selected = !!el.selected;
      }
      if (tag === "select") {
        const opt = el.selectedOptions && el.selectedOptions[0];
        node.value = opt ? (opt.value ?? opt.text) : "";
        node.selected = el.selectedIndex >= 0;
      }
      if (tag === "option") node.selected = !!el.selected;
      nodes.push(node);
      if (nodes.length >= 800) return;
    });
    return nodes;
  }

  // ------------------------------------------------------------------
  // Stage E: WebMCP model-context support.
  //
  // Real discovery of the page's model context, probed per page load.
  // The probe reads the browser-exposed navigator.modelContext API, which
  // IS visible from the content-script isolated world when the browser
  // implements it (like navigator.geolocation). A page-script polyfill in
  // the main JS world is NOT visible across the isolated-world boundary;
  // discovery honestly reports only what this script can actually invoke.
  // When the API is absent we report available:false -- never an empty
  // success, never a guessed tool list.
  // ------------------------------------------------------------------
  async function probeModelContext() {
    const mc = (typeof navigator !== "undefined" && navigator)
               ? navigator.modelContext : undefined;
    if (!mc) {
      return { available: false, tools: [],
               unavailable_reason: "webmcp_unavailable" };
    }
    let raw = [];
    try {
      // availableTools may be sync or async; await handles both.
      if (typeof mc.availableTools === "function") raw = await mc.availableTools();
      else if (Array.isArray(mc.tools)) raw = mc.tools;
    } catch (e) {
      return { available: false, tools: [],
               unavailable_reason: "probe failed: " + String((e && e.message) || e) };
    }
    // Declarative fallback: script[type="webmcp-tool"] blocks.
    try {
      document.querySelectorAll('script[type="webmcp-tool"]').forEach((s) => {
        try {
          const t = JSON.parse(s.textContent || "{}");
          if (t && t.name) raw.push(t);
        } catch { /* malformed block: skip, fail closed per tool */ }
      });
    } catch { /* querySelector unavailable: skip */ }
    const tools = [];
    const seen = new Set();
    for (const t of (Array.isArray(raw) ? raw : [])) {
      if (!t || typeof t.name !== "string" || !t.name || seen.has(t.name)) continue;
      seen.add(t.name);
      tools.push({ name: t.name,
                   description: typeof t.description === "string" ? t.description : "",
                   schema: (t.schema && typeof t.schema === "object") ? t.schema : {},
                   risk: t.risk || "low" });
    }
    return { available: true, tools };
  }

  // Honest invocation through the page's model context. Only called for a
  // tool the probe advertised; unknown names fail closed here too.
  async function doWebMCPInvoke(toolName, args = {}, action_id = null) {
    const rec = {
      ok: false, tool: toolName, action_id: action_id || null,
      result: null, error: null, error_code: null, ...identity(),
    };
    const probe = await probeModelContext();
    if (!probe.available) {
      rec.error = "webmcp_unavailable: page exposes no model context";
      rec.error_code = "WEBMCP_UNAVAILABLE";
      return rec;
    }
    if (!probe.tools.some((t) => t.name === toolName)) {
      rec.error = `webmcp tool not advertised by page: ${toolName}`;
      rec.error_code = "WEBMCP_TOOL_NOT_ADVERTISED";
      return rec;
    }
    const mc = navigator.modelContext;
    if (typeof mc.invokeTool !== "function") {
      rec.error = "webmcp_unavailable: model context has no invokeTool";
      rec.error_code = "WEBMCP_UNAVAILABLE";
      return rec;
    }
    try {
      const timeout = new Promise((_, rej) =>
        setTimeout(() => rej(new Error("webmcp invoke timed out")), 30000));
      const result = await Promise.race(
        [Promise.resolve(mc.invokeTool(toolName, args || {})), timeout]);
      if (result && result.ok === false) {
        rec.error = result.error || "tool reported failure";
        rec.error_code = "ACTION_FAILED";
        rec.result = result.result !== undefined ? result.result : null;
        return rec;
      }
      rec.ok = true;
      rec.result = (result && result.result !== undefined)
                   ? result.result : (result === undefined ? null : result);
      return rec;
    } catch (e) {
      rec.error = String((e && e.message) || e);
      rec.error_code = "ACTION_FAILED";
      return rec;
    }
  }

  async function pushSnapshot(goal = "") {
    const now = Date.now();
    if (now - lastSent < 800) return; // throttle
    lastSent = now;
    const nodes = captureNodes();
    // Stage E: page-reported model-context tools ride with the snapshot
    // so the harness gateway discovers them per document generation.
    let webmcp = null;
    try { webmcp = await probeModelContext(); } catch { webmcp = null; }
    try {
      await fetch(`${HARNESS}/snapshot`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: location.href, nodes, goal,
                               webmcp, ...identity() }),
      });
    } catch { /* harness offline: sidebar still works */ }
  }

  function highlight(sel, ms = 2500, color = "#7c3aed") {
    try {
      const el = document.querySelector(sel);
      if (!el) return false;
      const prev = el.style.outline;
      el.style.outline = `3px solid ${color}`;
      el.scrollIntoView({ block: "center", behavior: "smooth" });
      setTimeout(() => { el.style.outline = prev; }, ms);
      return true;
    } catch { return false; }
  }

  // Stage D: honest command execution. Returns a BROWSER_ACK dict.
  // executed=true is ONLY set together with the observed post-state,
  // captured AFTER the DOM operation ran. Failures carry typed
  // error_codes the harness maps into its error taxonomy.
  function doCommand(command, target, args = {}, action_id = null) {
    const ack = {
      ack: true,
      ack_id: "ack_" + Math.random().toString(16).slice(2, 10),
      action_id: action_id || null,
      command,
      accepted: true,      // validated + whitelisted command
      executed: false,     // flips only with an observation below
      observed: null,
      error: null,
      error_code: null,
      ...identity(),
    };
    const fail = (error, error_code) => ({ ...ack, error, error_code });

    // Frame targeting: this content-script instance only acts when the
    // message names its own frame. Sibling frames stay silent so exactly
    // one frame replies (broadcast EXECUTE, single responder).
    const wantFrame = (target && target.frame_id) || "main";
    if (wantFrame !== FRAME_ID) return null; // not our frame: no reply

    const spec = {
      frameId: FRAME_ID,
      shadowPath: (target && target.shadow_path) || [],
      locator: (target && target.locator) || {},
    };
    // Backwards compat: older harness messages carry only args.selector.
    if (!spec.locator.value && args.selector) {
      spec.locator = { strategy: "css", value: args.selector };
    }
    const found = __synk.resolveTarget(spec);
    if (!found.ok) {
      return fail(found.error,
        found.errorCode === "AMBIGUOUS_ELEMENT" ? "AMBIGUOUS_ELEMENT"
                                                : "STALE_REFERENCE");
    }
    const el = found.el;
    try { highlight(spec.locator.value); } catch (e) {}

    let res;
    try {
      switch (command) {
        case "click":
          res = __synk.robustClick(el);
          break;
        case "type":
          res = __synk.robustType(el, args.text ?? "",
                                  { clearFirst: args.clear_first !== false });
          break;
        case "select":
          res = __synk.robustSelect(el, args.value ?? "");
          break;
        case "press_key":
          res = __synk.pressKey(el, args.key || "Enter");
          break;
        case "scroll":
          window.scrollBy(0, args.direction === "up" ? -600 : 600);
          res = { ok: true, observed: { scrolled: true, url: location.href } };
          break;
        case "focus":
          el.focus();
          res = { ok: true, observed: __synk.observeTarget(el) };
          break;
        case "hover":
          el.dispatchEvent(new MouseEvent("mouseover",
            { bubbles: true, composed: true }));
          res = { ok: true, observed: __synk.observeTarget(el) };
          break;
        default:
          return fail(`content: unsupported '${command}'`, "TOOL_NOT_FOUND");
      }
    } catch (e) {
      return fail(String((e && e.message) || e), "ACTION_FAILED");
    }
    if (!res.ok) return fail(res.error || "primitive failed", "ACTION_FAILED");

    // The DOM operation ran; capture the post-state NOW. This is the
    // evidence the verifier will judge -- not our word that it ran.
    ack.executed = true;
    ack.observed = {
      observation_id: "obs_" + Date.now().toString(36) +
                      Math.random().toString(16).slice(2, 8),
      url: location.href,
      target_state: res.observed || {},
      captured_at: Date.now(),
    };
    return ack;
  }

  // Human-activity detection -> ownership events (Beta); the pause flag
  // remains as a coarse fallback (Alpha compat).
  let humanTimer = null;
  function humanActive(ev) {
    try {
      const t = ev?.target;
      const sel = t && t.tagName ? selector(t) : undefined;
      chrome.runtime.sendMessage({ type: "HUMAN_ACTIVE", active: true });
      if (sel) {
        const kind = ev.type === "click" ? "click" : ev.type === "keydown" ? "key" : "type";
        sendEvent("human.action", { kind, target: sel });
        if (ev.type === "click") sendEvent("click.human", { target: sel });
      }
      clearTimeout(humanTimer);
      humanTimer = setTimeout(() =>
        chrome.runtime.sendMessage({ type: "HUMAN_ACTIVE", active: false }), 3000);
    } catch { /* ignore */ }
  }
  ["click", "keydown", "input"].forEach((ev) =>
    document.addEventListener(ev, humanActive, { passive: true, capture: true }));

  // Beta: focus + value + dialog tracking for InteractionState.
  document.addEventListener("focusin", (ev) => {
    try { sendEvent("focus.changed", { target: selector(ev.target) }); } catch {}
  }, { passive: true, capture: true });
  document.addEventListener("input", (ev) => {
    try {
      const t = ev.target;
      const isPassword = t && (t.type || "").toLowerCase() === "password";
      const data = { target: selector(t), detail: "value edited by human" };
      // Canonical state needs the new value; passwords are never reported.
      if (!isPassword && t && "value" in t && typeof t.value === "string") {
        data.value = t.value.slice(0, 500);
      }
      if (t && "checked" in t) data.checked = !!t.checked;
      if (t && "disabled" in t) data.disabled = !!t.disabled;
      sendEvent("value.changed", data);
    } catch {}
  }, { passive: true, capture: true });
  new MutationObserver((muts) => {
    for (const m of muts) {
      for (const n of m.addedNodes) {
        if (n.nodeType === 1 &&
            (n.tagName === "DIALOG" || n.getAttribute?.("role") === "dialog" ||
             (n.className || "").toString().includes("modal"))) {
          sendEvent("dialog.opened", { target: selector(n) });
        }
      }
    }
  }).observe(document.documentElement, { childList: true, subtree: true });

  chrome.runtime.onMessage.addListener(async (msg, _sender, reply) => {
    if (msg.type === "CAPTURE") {
      reply({ url: location.href, nodes: captureNodes(),
              webmcp: await probeModelContext(), ...identity() });
      pushSnapshot(msg.goal || "");
      return true;
    }
    if (msg.type === "EXECUTE") {
      // Single-responder broadcast: only the target frame replies.
      const res = doCommand(msg.command, msg.target || {},
                            msg.args || {}, msg.action_id || null);
      if (res === null) return false; // not our frame: stay silent
      pushSnapshot();
      reply(res);
      return true;
    }
    if (msg.type === "HIGHLIGHT") {
      // Beta: purple = agent-owned target, blue = human-owned region.
      reply({ ok: highlight(msg.selector, 2500, msg.color || "#7c3aed") });
      return true;
    }
    // Stage E: WebMCP closed-loop path. Discovery reports the page's real
    // model-context tools (or honest unavailability); invocation runs only
    // in the target frame (single-responder broadcast, like EXECUTE).
    if (msg.type === "WEBMCP_DISCOVER") {
      reply({ ...await probeModelContext(), ...identity() });
      return true;
    }
    if (msg.type === "WEBMCP_INVOKE") {
      const wantFrame = (msg.target && msg.target.frame_id) || "main";
      if (wantFrame !== FRAME_ID) return false; // not our frame: silent
      doWebMCPInvoke(msg.tool_name, msg.args || {},
                     msg.action_id || null).then((rec) => {
        pushSnapshot();
        reply(rec);
      });
      return true; // async reply
    }
  });

  // Initial snapshot on load (viewport-at-a-time for long pages, spec 9).
  window.addEventListener("load", () => setTimeout(() => pushSnapshot(), 600));
})();
