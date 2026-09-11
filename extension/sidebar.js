/* Sidebar UI logic: task pane, progress, consent, transparent log (spec 6). */
const $ = (id) => document.getElementById(id);
const statusEl = $("status"), trailEl = $("trail"), logEl = $("log");
let paused = false;

function setStatus(t) { statusEl.textContent = t; }

function addTrail(item) {
  const li = document.createElement("li");
  li.textContent = typeof item === "string" ? item : JSON.stringify(item, null, 1);
  trailEl.prepend(li);
}

function addLog(entry) {
  const li = document.createElement("li");
  li.textContent = `[${new Date(entry.ts).toLocaleTimeString()}] ${entry.kind}: ` +
    JSON.stringify(entry.action || entry.res || entry.q || entry).slice(0, 300);
  logEl.prepend(li);
}

$("runBtn").addEventListener("click", async () => {
  const goal = $("goal").value.trim();
  if (!goal) { setStatus("Enter a goal first."); return; }
  setStatus("Working on it… (you can keep browsing; I'll pause if you interact)");
  trailEl.innerHTML = "";
  try {
    const res = await chrome.runtime.sendMessage({
      type: "RUN_TASK", goal, userConsented: $("consent").checked,
    });
    if (!res?.ok) { setStatus(`Failed: ${res?.error || "unknown"}`); return; }
    (res.trail || []).forEach(addTrail);
    // Beta: show routing + workflow prep + conflicts.
    const meta = [`tier: ${res.tier || "?"}`,
                  `exec level: ${res.execution_level ?? "?"}`];
    if (res.workflow?.matched) meta.push(`workflow: ${res.workflow.workflow} (${res.workflow.suggestion})`);
    if (res.conflicts) meta.push(`conflicts: ${res.conflicts}`);
    addTrail(meta.join(" | "));
    if (res.needsUser) setStatus(`Needs you: ${res.question}`);
    else setStatus(`Done (model: ${res.model || "?"}). Review the log.`);
    refreshLog();
  } catch (e) {
    setStatus(`Harness unreachable — is 'python -m harness.server' running? (${e.message})`);
  }
});

$("pauseBtn").addEventListener("click", async () => {
  paused = !paused;
  $("pauseBtn").textContent = paused ? "Resume agent" : "Pause agent";
  try {
    await fetch("http://127.0.0.1:18080/human", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ active: paused }),
    });
  } catch {}
  setStatus(paused ? "Agent paused — you have full control." : "Agent resumed.");
});

$("forgetBtn").addEventListener("click", async () => {
  if (!confirm("Erase everything the agent remembers about you?")) return;
  try {
    await fetch("http://127.0.0.1:18080/memory/forget", { method: "POST" });
    setStatus("Memory erased.");
  } catch { setStatus("Harness unreachable."); }
});

async function refreshLog() {
  try {
    const { log } = await chrome.runtime.sendMessage({ type: "GET_LOG" });
    logEl.innerHTML = "";
    (log || []).slice().reverse().forEach(addLog);
  } catch {}
}
// Keyboard navigable by default (native buttons/inputs); no custom shortcuts hijacked.
refreshLog();
