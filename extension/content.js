/* Content script: semantic page perception + guarded execution (spec 4, 5, 9).
 * - Captures accessibility-ish snapshot (role/name/tag/selector/interactive).
 * - Runs in isolated JS world; only executes pre-defined commands from harness.
 * - Reports human activity so the agent pauses (human priority).
 * - CSP-safe: no remote scripts, no eval; messaging via chrome.runtime only.
 */
(() => {
  const HARNESS = "http://127.0.0.1:18080";
  let lastSent = 0;

  // Beta: fire-and-forget typed events -> harness Event Bus / WorldState.
  function sendEvent(type, data) {
    try {
      fetch(`${HARNESS}/event`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type, data: { url: location.href, ...(data || {}) } }),
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
      if (r.width === 0 && r.height === 0) return; // skip hidden
      const tag = el.tagName.toLowerCase();
      nodes.push({
        role: roleOf(el),
        name: nameOf(el),
        tag,
        selector: selector(el),
        interactive: ["a", "button", "input", "select", "textarea"].includes(tag),
      });
      if (nodes.length >= 800) return;
    });
    return nodes;
  }

  async function pushSnapshot(goal = "") {
    const now = Date.now();
    if (now - lastSent < 800) return; // throttle
    lastSent = now;
    const nodes = captureNodes();
    try {
      await fetch(`${HARNESS}/snapshot`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: location.href, nodes, goal }),
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

  function doCommand(cmd, args = {}) {
    // Whitelisted DOM ops only (spec 5: never execute arbitrary code).
    switch (cmd) {
      case "click": {
        const el = document.querySelector(args.selector || "");
        if (!el) return { ok: false, error: "no such element" };
        highlight(args.selector);
        el.click();
        return { ok: true };
      }
      case "type": {
        const el = document.querySelector(args.selector || "");
        if (!el) return { ok: false, error: "no such element" };
        highlight(args.selector);
        el.focus();
        el.value = args.text ?? "";
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
        return { ok: true };
      }
      case "select": {
        const el = document.querySelector(args.selector || "");
        if (!el) return { ok: false, error: "no such element" };
        el.value = args.value ?? "";
        el.dispatchEvent(new Event("change", { bubbles: true }));
        return { ok: true };
      }
      case "scroll":
        window.scrollBy(0, args.direction === "up" ? -600 : 600);
        return { ok: true };
      case "press_key":
        document.activeElement?.dispatchEvent(
          new KeyboardEvent("keydown", { key: args.key || "Enter", bubbles: true }));
        return { ok: true };
      case "focus": {
        const el = document.querySelector(args.selector || "");
        el?.focus();
        return { ok: !!el };
      }
      case "hover": {
        const el = document.querySelector(args.selector || "");
        el?.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
        return { ok: !!el };
      }
      default:
        return { ok: false, error: `content: unsupported '${cmd}'` };
    }
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
      sendEvent("value.changed",
        { target: selector(ev.target), detail: "value edited by human" });
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

  chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
    if (msg.type === "CAPTURE") {
      reply({ url: location.href, nodes: captureNodes() });
      pushSnapshot(msg.goal || "");
      return true;
    }
    if (msg.type === "EXECUTE") {
      const res = doCommand(msg.command, msg.args || {});
      pushSnapshot();
      reply(res);
      return true;
    }
    if (msg.type === "HIGHLIGHT") {
      // Beta: purple = agent-owned target, blue = human-owned region.
      reply({ ok: highlight(msg.selector, 2500, msg.color || "#7c3aed") });
      return true;
    }
  });

  // Initial snapshot on load (viewport-at-a-time for long pages, spec 9).
  window.addEventListener("load", () => setTimeout(() => pushSnapshot(), 600));
})();
