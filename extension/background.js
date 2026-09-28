/* Background service worker: harness bridge + consent + logging (spec 5, 10).
 * MV3 service worker: no DOM, no long-lived WebSocket; fetch per task step.
 *
 * Stage C: runTask is a CLOSED LOOP driven by the harness agent endpoints:
 *
 *   /agent/begin  -> mint task_id, pin tab identity
 *   loop:
 *     capture()          OBSERVE (pinned tab only)
 *     /snapshot          push observation; harness versions + trims it
 *     /agent/next        OBSERVE -> UPDATE WORLD -> SELECT CAPABILITY ->
 *                        PLAN -> VALIDATE -> LEASE  => "act" + one action,
 *                        or a decision: success | continue | replan |
 *                        request-human | abort
 *     executeOnPage()    perform the ONE mutation on the pinned tab
 *     capture()          OBSERVE the post-action state
 *     /snapshot          push post-action observation (task_id + action_id)
 *     /agent/report      VERIFY -> DECIDE
 *
 * The harness never trusts "the tool executor said ok": only an
 * independent post-execution observation can move a claim to VERIFIED.
 * The loop continues, replans, asks the user, or aborts based on the
 * typed decision the harness returns.
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

// Capture the PINNED task tab (never the browser's "current tab" mid-task).
async function capture(tabId, windowId, goal = "", extra = {}) {
  const snap = await chrome.tabs.sendMessage(Number(tabId),
    { type: "CAPTURE", goal });
  const view = await harness("/snapshot", {
    url: snap.url, nodes: snap.nodes, goal,
    tab_id: String(tabId), window_id: String(windowId),
    frame_id: "main", ...extra,
  });
  return { snap, view };
}

async function executeOnPage(tabId, action) {
  // One mutation on the pinned tab. The returned result is the BROWSER's
  // actual report (ok / error) -- not an acknowledgement.
  return chrome.tabs.sendMessage(Number(tabId), {
    type: "EXECUTE",
    command: action.tool,
    args: { ...action, selector: action.selector || action.target },
  });
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

const LOOP_HARD_CAP = 60; // local safety net; the harness budget is authoritative

async function runTask(goal, opts = {}) {
  const trail = [];
  const consent = !!opts.userConsented;
  const started = Date.now();

  function note(kind, detail) {
    const e = { ts: Date.now(), goal, kind, ...detail };
    trail.push(e);
    log.push(e);
  }

  // Pin the execution target NOW: every later step addresses this tab
  // explicitly. A user tab-switch mid-task cannot redirect agent actions.
  let taskTab;
  try {
    taskTab = await currentTab();
    if (!taskTab?.id) throw new Error("no active tab");
  } catch (e) {
    return { ok: false, error: `capture failed: ${e.message}` };
  }
  const tabId = String(taskTab.id), windowId = String(taskTab.windowId);

  let begin;
  try {
    begin = await harness("/agent/begin", {
      goal,
      identity: { tab_id: tabId, window_id: windowId, frame_id: "main" },
      max_steps: opts.max_steps || 15,
    });
  } catch (e) {
    return { ok: false, error: `agent begin failed: ${e.message}` };
  }
  const taskId = begin.task_id;
  note("begin", { task_id: taskId, tab_id: tabId });

  for (let i = 0; i < LOOP_HARD_CAP; i++) {
    // OBSERVE: capture the pinned tab and push the observation.
    let view;
    try {
      ({ view } = await capture(tabId, windowId, goal, { task_id: taskId }));
    } catch (e) {
      await tryReport(taskId, null, null, "browser_failed",
        { error_code: "BROWSER_NOT_READY", reason: `capture failed: ${e.message}` });
      return { ok: false, error: `capture failed: ${e.message}`, trail, task_id: taskId };
    }

    // SELECT CAPABILITY + PLAN + VALIDATE + LEASE (server side).
    let next;
    try {
      next = await harness("/agent/next", { task_id: taskId });
    } catch (e) {
      return { ok: false, error: `agent next failed: ${e.message}`, trail, task_id: taskId };
    }
    note("decision", { decision: next.decision, reason: next.reason,
                       steps_used: next.steps_used });

    if (next.decision === "act") {
      const action = next.action || {};
      // EXECUTE: exactly one mutation on the pinned tab.
      let browserResult;
      try {
        browserResult = await executeOnPage(tabId, action);
      } catch (e) {
        browserResult = { ok: false, error: `content script: ${e.message}` };
      }

      // OBSERVE again: the post-action snapshot is the evidence the
      // verifier will judge the claim against. It carries the action
      // identity so evidence ties to this exact mutation.
      try {
        await capture(tabId, windowId, goal,
          { task_id: taskId, action_id: next.action_id });
      } catch (e) {
        await tryReport(taskId, next.action_id, next.claim_id, "browser_failed",
          { error_code: "BROWSER_NOT_READY",
            reason: `post-action capture failed: ${e.message}` });
        return { ok: false, error: `post-action capture failed: ${e.message}`,
                 trail, task_id: taskId };
      }

      // VERIFY + DECIDE (server side).
      const report = await tryReport(
        taskId, next.action_id, next.claim_id,
        browserResult?.ok ? "executed" : "browser_failed",
        browserResult?.ok ? {} : { reason: browserResult?.error || "unknown browser error" });
      note("report", { decision: report?.decision, reason: report?.reason,
                       verification: report?.verification?.result,
                       verified: report?.verified });
      if (report?.decision === "request-human") {
        return { ok: true, needsUser: true, trail, task_id: taskId,
                 question: report.reason, verified: report.verified };
      }
      if (report?.decision === "abort") {
        return { ok: false, error: report.reason, trail, task_id: taskId };
      }
      if (report?.decision === "success") {
        return finish(true);
      }
      continue; // continue | replan: observe again and keep going
    }

    if (next.decision === "success") return finish(true);
    if (next.decision === "request-human") {
      return { ok: true, needsUser: true, trail, task_id: taskId,
               question: next.question || next.reason,
               verified: next.verified };
    }
    if (next.decision === "abort") {
      return { ok: false, error: next.reason, trail, task_id: taskId };
    }
    // continue | replan: loop back to OBSERVE.
  }
  return { ok: false, error: "extension loop hard cap reached", trail,
           task_id: taskId };

  function finish(ok) {
    return { ok, trail, task_id: taskId, tab_id: tabId,
             ms: Date.now() - started };
  }
}

async function tryReport(taskId, actionId, claimId, status, extra = {}) {
  try {
    return await harness("/agent/report", {
      task_id: taskId, action_id: actionId, claim_id: claimId,
      status, ...extra,
    });
  } catch (e) {
    return { decision: "replan", reason: `report failed: ${e.message}` };
  }
}
