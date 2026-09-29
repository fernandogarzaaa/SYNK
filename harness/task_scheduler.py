"""Stage G task scheduler (Phase 9): an HONEST scheduler.

Execution mode: SEQUENTIAL only. Tasks run one at a time, in submission
order, each through the closed-loop execution gateway with per-action
verification. This module never claims concurrency it cannot execute.

Why no parallel mode: the browser runtimes, ToolExecutor, AgentLoop, and
server STATE are not thread-safe (a single CDP/extension connection, a
shared ContextManager, one world journal). Real concurrency would require
(a) a thread-safe BrowserRuntime per tab with isolated CDP sessions,
(b) per-tab agent contexts that never share mutable state, and
(c) the lease system below, which already exists and is the dispatch
gate. Until (a) and (b) exist, LIMITED/SEQUENTIAL are the only honest
modes, so this scheduler implements SEQUENTIAL and says so.

Lease contract with the transaction engine: the scheduler holds
"tab:<id>" for the running task (task_id=T). The engine's per-action
leases nest under that tab lease ONLY for the same task id
(LeaseManager.acquire(..., nest_under_task_id=T)); actions from any
other task, the human, or a conflict still refuse. The scheduler
releases the tab lease when the task finishes.

What the scheduler does provide:
  - task submission (structured spec or pre-compiled actions),
  - status query, cancellation,
  - per-task budgets (max_steps, max_seconds),
  - lease-aware dispatch: a task only starts when its tab lease is free;
    otherwise it waits in "blocked" with a recorded reason,
  - dependency gating (a task runs after its dependencies complete),
  - optional model-routing decision recorded per task.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

# The only implemented execution mode.
EXECUTION_MODE = "SEQUENTIAL"

QUEUED, BLOCKED, RUNNING = "queued", "blocked", "running"
COMPLETED, FAILED, CANCELLED = "completed", "failed", "cancelled"

_TERMINAL = (COMPLETED, FAILED, CANCELLED)


@dataclass
class ScheduledTask:
    task_id: str
    goal: str
    actions: list[dict]
    tab_id: str
    session_id: str | None = None
    window_id: str = "win_default"
    frame_id: str = "main"
    page_url: str = ""
    origin: str | None = None
    user_consented: bool = False
    status: str = QUEUED
    depends_on: list[str] = field(default_factory=list)
    max_steps: int = 25
    max_seconds: float = 300.0
    requirements: dict = field(default_factory=dict)  # model-routing input
    routing: dict | None = None                        # routing decision
    result: dict | None = None
    block_reason: str = ""
    cancel_requested: bool = False
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "goal": self.goal,
            "status": self.status, "tab_id": self.tab_id,
            "session_id": self.session_id, "window_id": self.window_id,
            "frame_id": self.frame_id, "page_url": self.page_url,
            "origin": self.origin, "action_count": len(self.actions),
            "depends_on": list(self.depends_on),
            "budgets": {"max_steps": self.max_steps,
                        "max_seconds": self.max_seconds},
            "requirements": dict(self.requirements),
            "routing": self.routing,
            "result": self.result, "block_reason": self.block_reason,
            "cancel_requested": self.cancel_requested,
            "created_at": self.created_at, "started_at": self.started_at,
            "finished_at": self.finished_at,
            "execution_mode": EXECUTION_MODE,
        }


class TaskScheduler:
    """Sequential, lease-aware task queue.

    exec_fn contract for run_next(): exec_fn(task: dict) -> dict with at
    least {"ok": bool}. The scheduler calls it once per dispatched task;
    between dispatch and completion no other task runs.
    """

    def __init__(self, leases=None):
        self.leases = leases  # LeaseManager or None (tests)
        self.tasks: dict[str, ScheduledTask] = {}

    # -- submission ------------------------------------------------------------
    def submit(self, spec: dict, *, compiler=None, policy=None,
               router=None) -> ScheduledTask:
        """Submit a task. spec keys:

        goal (str, required), tab_id, session_id, window_id, frame_id,
        page_url, origin, user_consented,
        spec (compiler input; compiled first, typed errors propagate) OR
        actions (pre-compiled/validated list),
        depends_on (list of task ids), budgets {"max_steps","max_seconds"},
        requirements (model-routing input; routed when a router is given).
        """
        if not isinstance(spec, dict):
            raise ValueError("task spec must be a dict")
        goal = spec.get("goal", "")
        if not goal:
            raise ValueError("task spec must declare 'goal'")
        actions: list[dict] = []
        if spec.get("spec") is not None:
            if compiler is None:
                from .compiler import compile_spec as _compile
                compiler = _compile
            plan = compiler(spec["spec"], policy=policy,
                            task_id=spec.get("task_id", "t_new"))
            actions = plan["actions"]
        elif isinstance(spec.get("actions"), list):
            actions = [dict(a) for a in spec["actions"]]
        budgets = spec.get("budgets", {}) or {}
        max_steps = max(1, int(budgets.get("max_steps", 25)))
        max_seconds = budgets.get("max_seconds", 300.0)
        try:
            max_seconds = float(max_seconds)
        except (TypeError, ValueError):
            raise ValueError("budgets.max_seconds must be a number")
        if max_seconds <= 0:
            raise ValueError("budgets.max_seconds must be positive")
        if len(actions) > max_steps:
            raise ValueError(
                f"task has {len(actions)} actions but max_steps={max_steps} "
                "(budget exceeded at submit time)")
        depends_on = [str(d) for d in (spec.get("depends_on") or [])]
        for d in depends_on:
            if d not in self.tasks:
                raise ValueError(f"unknown dependency task '{d}'")
        task_id = spec.get("task_id") or f"t_{uuid.uuid4().hex[:12]}"
        existing = self.tasks.get(task_id)
        if existing is not None and existing.status not in _TERMINAL:
            raise ValueError(f"task {task_id} already {existing.status}")
        requirements = dict(spec.get("requirements") or {})
        routing = None
        if requirements and router is not None:
            routing = router.route(requirements, task_id=task_id)
        task = ScheduledTask(
            task_id=task_id, goal=str(goal), actions=actions,
            tab_id=spec.get("tab_id", "default"),
            session_id=spec.get("session_id"),
            window_id=spec.get("window_id", "win_default"),
            frame_id=spec.get("frame_id", "main"),
            page_url=spec.get("page_url", ""),
            origin=spec.get("origin"),
            user_consented=bool(spec.get("user_consented", False)),
            depends_on=depends_on,
            max_steps=max_steps, max_seconds=max_seconds,
            requirements=requirements, routing=routing)
        self.tasks[task_id] = task
        return task

    # -- queries -----------------------------------------------------------------
    def status(self, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        if task is None:
            return {"ok": False, "error": "not found", "task_id": task_id}
        d = task.to_dict()
        d["ok"] = True
        return d

    def cancel(self, task_id: str) -> bool:
        """Cancel a queued/blocked task immediately. A running task gets a
        cancel flag that the runner's exec_fn is expected to honor between
        steps; the scheduler records the request either way."""
        task = self.tasks.get(task_id)
        if task is None or task.status in _TERMINAL:
            return False
        if task.status == RUNNING:
            task.cancel_requested = True
            return True
        task.status = CANCELLED
        task.finished_at = time.time()
        return True

    def cancel_requested(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        return bool(task and task.cancel_requested)

    # -- dispatch ------------------------------------------------------------------
    def _deps_met(self, task: ScheduledTask) -> tuple[bool, str]:
        for d in task.depends_on:
            dep = self.tasks.get(d)
            if dep is None or dep.status != COMPLETED:
                return False, f"waiting on {d}"
        return True, ""

    def _try_acquire_tab(self, task: ScheduledTask):
        """Lease-aware dispatch: the tab lease must be free. Returns
        (lease_record | None, block_reason)."""
        if self.leases is None:
            return {"lease": "test", "target": f"tab:{task.tab_id}"}, ""
        rec = self.leases.acquire(
            f"tab:{task.tab_id}", intent=f"task:{task.task_id}",
            task_id=task.task_id, ttl=min(30.0, task.max_seconds))
        if rec is None:
            try:
                st = self.leases.ownership.state_of(f"tab:{task.tab_id}")
            except Exception:
                st = "unknown"
            return None, f"tab lease busy (ownership state: {st})"
        return rec, ""

    def run_next(self, exec_fn: Callable[[dict], dict]) -> dict | None:
        """Dispatch the next ready task (oldest first), SEQUENTIAL.

        Returns the finished task's dict, a blocked-task dict, or None when
        nothing is dispatchable. Time budgets are enforced around exec_fn;
        step budgets were enforced at submit time and are carried in the
        task dict for exec_fn to honor.
        """
        ready = [t for t in self.tasks.values() if t.status in (QUEUED, BLOCKED)]
        ready.sort(key=lambda t: t.created_at)
        for task in ready:
            ok, reason = self._deps_met(task)
            if not ok:
                continue
            if task.cancel_requested:
                task.status = CANCELLED
                task.finished_at = time.time()
                return task.to_dict()
            lease, block_reason = self._try_acquire_tab(task)
            if lease is None:
                task.status = BLOCKED
                task.block_reason = block_reason
                return {"task_id": task.task_id, "status": BLOCKED,
                        "block_reason": block_reason}
            task.status = RUNNING
            task.block_reason = ""
            task.started_at = time.time()
            try:
                if task.cancel_requested:
                    raise _Cancelled("cancel requested before dispatch")
                result = exec_fn(self._task_view(task))
                if not isinstance(result, dict):
                    result = {"ok": False, "error": "exec_fn returned "
                                                   f"{type(result).__name__}"}
            except _Cancelled as e:
                result = {"ok": False, "error": str(e), "cancelled": True}
            except Exception as e:  # exec_fn failures are task failures
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            finally:
                try:
                    if self.leases is not None and isinstance(lease, dict) \
                            and lease.get("lease"):
                        self.leases.release(f"tab:{task.tab_id}",
                                            lease["lease"])
                except Exception:
                    pass
            task.finished_at = time.time()
            elapsed = task.finished_at - (task.started_at or task.finished_at)
            if elapsed > task.max_seconds:
                task.status = FAILED
                task.result = {"ok": False, "error": "BUDGET_EXCEEDED",
                               "elapsed_s": round(elapsed, 2),
                               "max_seconds": task.max_seconds}
            elif result.get("cancelled"):
                task.status = CANCELLED
                task.result = result
            elif result.get("ok"):
                task.status = COMPLETED
                task.result = result
            else:
                task.status = FAILED
                task.result = result
            return task.to_dict()
        return None

    def _task_view(self, task: ScheduledTask) -> dict:
        return {
            "task_id": task.task_id, "goal": task.goal,
            "actions": [dict(a) for a in task.actions],
            "tab_id": task.tab_id, "session_id": task.session_id,
            "window_id": task.window_id, "frame_id": task.frame_id,
            "page_url": task.page_url, "origin": task.origin,
            "user_consented": task.user_consented,
            "max_steps": task.max_steps, "max_seconds": task.max_seconds,
            "routing": task.routing,
            "execution_mode": EXECUTION_MODE,
        }


class _Cancelled(Exception):
    pass
