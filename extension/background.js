/* Background service worker: harness bridge + consent + logging (spec 5, 10).
 * MV3 service worker: no DOM, no long-lived WebSocket; fetch per task step.
 */
const HARNESS = "http://127.0.0.1:18080";
const log = [];

async function harness(path, body) {
  const r = await fetch(HARNESS + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) throw new Error(`harness ${path}: HTTP ${r.status}`);
  return r.json();
}

async function currentTab() {
  const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
  return t;
}

async function capture(goal = "") {
  const tab = await currentTab();
  if (!tab?.id) throw new Error("no active tab");
  const snap = await chrome.tabs.sendMessage(tab.id, { type: "CAPTURE", goal });
  // also push to harness for trimming/versioning; tab identity is explicit
  // so the harness never has to infer the execution target later.
  const view = await harness("/snapshot", {
    url: snap.url, nodes: snap.nodes, goal,
    tab_id: String(tab.id), window_id: String(tab.windowId),
    frame_id: "main",
  });
  return { tab, view };
}

async function executeOnPage(tabId, command, args) {
  return chrome.tabs.sendMessage(tabId, { type: "EXECUTE", command, args });
}

// Resolve a validated command's ref -> selector via the harness snapshot view.
function selectorFor(view, ref) {
  const n = (view.nodes || []).find((x) => x.ref === ref);
  return n?.selector || null;
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  (async () => {
    if (msg.type === "HUMAN_ACTIVE") {
      // Forward pause flag; best-effort (harness may be offline).
      try { await harness("/human", { active: msg.active }); } catch {}
      reply({ ok: true });
    } else if (msg.type === "RUN_TASK") {
      const out = await runTask(msg.goal, msg);
      reply(out);
    } else if (msg.type === "GET_LOG") {
      reply({ log: log.slice(-100) });
    } else if (msg.type === "GET_TAB_ID") {
      // Content scripts cannot read their own tab id; the background relays
      // it so every extension-originated event carries explicit tab identity.
      reply({ tab_id: sender?.tab?.id ?? null,
              window_id: sender?.tab?.windowId ?? null });
    }
  })().catch((e) => reply({ ok: false, error: String(e?.message || e) }));
  return true; // async reply
});

async function runTask(goal, opts = {}) {
  const trail = [];
  const consent = !!opts.userConsented;
  let view, taskTab;
  try {
    // Pin the execution target NOW: every later step addresses this tab
    // explicitly. The browser's "current tab" is never consulted again,
    // so a user tab-switch mid-task cannot redirect agent actions.
    ({ tab: taskTab, view } = await capture(goal));
  } catch (e) {
    return { ok: false, error: `capture failed: ${e.message}` };
  }
  const tabId = String(taskTab.id), windowId = String(taskTab.windowId);
  const plan = await harness("/plan", { goal });
  const actions = plan.actions || [];
  for (const a of actions) {
    if (a.tool === "ask_user") {
      trail.push({ kind: "ask_user", question: a.question });
      log.push({ ts: Date.now(), goal, kind: "ask_user", q: a.question });
      return { ok: true, needsUser: true, question: a.question, trail, model: plan.model,
               tier: plan.tier, execution_level: plan.execution_level,
               workflow: plan.workflow_preparation };
    }
    if (a.tool === "bulk") {
      const res = await harness("/act", {
        actions: a.actions, page_url: view.url, user_consented: consent,
        tab_id: tabId, window_id: windowId, frame_id: "main",
      });
      // execute each allowed command on the PINNED task tab
      for (const c of res.results?.[0]?.executed || []) {
        const sel = c.args?.ref ? selectorFor(view, c.args.ref) : c.args?.selector;
        if (c.command === "navigate" || c.command === "snapshot") continue;
        await executeOnPage(taskTab.id, c.command,
          { ...c.args, selector: sel || c.args?.selector });
      }
      trail.push({ kind: "bulk", result: res.results?.[0] });
      log.push({ ts: Date.now(), goal, kind: "bulk", res: res.results?.[0] });
      continue;
    }
    const res = await harness("/transact", {
      action: { ...a, intent: goal, lease_ttl: 2.0,
                target: a.target || (a.ref != null ? selectorFor(view, a.ref)
                                                   : a.selector || "?") },
      page_url: view.url, user_consented: consent,
      tab_id: tabId, window_id: windowId, frame_id: "main",
    });
    const r = res.results?.[0];
    trail.push({ kind: "transact", action: a, result: r });
    log.push({ ts: Date.now(), goal, kind: "transact", action: a, result: r });
    if (r?.verdict === "replan" || r?.verdict === "request_ownership") {
      // Beta: conflict -> replan or surface to user, never blind-retry.
      const q = `Conflict on ${a.target || a.ref}: ${r.error}. Replan or take over?`;
      trail.push({ kind: "ask_user", question: q });
      return { ok: true, needsUser: true, question: q, trail,
               model: plan.model, tier: plan.tier,
               execution_level: plan.execution_level,
               workflow: plan.workflow_preparation,
               conflicts: (res.conflicts || 0) + 1 };
    }
    if (r?.ok && r?.command && !["snapshot", "navigate", "ask_user", "summarize"].includes(r.command)) {
      // Pinned tab: never re-resolve "current tab" mid-task.
      const sel = r.args?.ref ? selectorFor(view, r.args.ref) : r.args?.selector;
      await executeOnPage(taskTab.id, r.command, { ...r.args, selector: sel || r.args?.selector });
    }
    if (!r?.ok) break; // spec 2: ask user rather than stubbornly retry
  }
  return { ok: true, trail, model: plan.model, tier: plan.tier,
           tier_reason: plan.tier_reason,
           execution_level: plan.execution_level,
           workflow: plan.workflow_preparation };
}
