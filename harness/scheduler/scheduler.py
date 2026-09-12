"""Task Graph and Parallel Scheduler for Beta.3.
Manages independent agent tasks across multiple WorldState tabs.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional, Dict, List, Set
import time

@dataclass
class AgentTask:
    task_id: str
    tab_id: str
    intent: str
    dependencies: Set[str] = field(default_factory=set)
    status: str = "pending" # pending | running | completed | failed
    result: Any = None

class ParallelScheduler:
    def __init__(self, ownership_graph):
        self.ownership = ownership_graph
        self.tasks: Dict[str, AgentTask] = {}
        self.running_tasks: Set[str] = set()

    def submit(self, task_id: str, tab_id: str, intent: str, deps: Set[str] = None):
        existing = self.tasks.get(task_id)
        if existing and existing.status in ("pending", "running"):
            raise ValueError(f"task {task_id} already {existing.status}")
        self.tasks[task_id] = AgentTask(
            task_id=task_id, tab_id=tab_id, intent=intent,
            dependencies=set(deps or set())
        )

    def _tab_blocked(self, tab_id: str) -> bool:
        try:
            state = self.ownership.state_of(f"tab:{tab_id}")
        except Exception:
            return False
        return state in ("HUMAN_OWNED", "CONFLICT")

    def get_ready_tasks(self) -> List[AgentTask]:
        """Returns tasks whose dependencies are met and tabs are available."""
        ready = []
        for tid, task in self.tasks.items():
            if task.status != "pending":
                continue
            deps_met = True
            for d in task.dependencies:
                dep = self.tasks.get(d)
                if dep is None or dep.status != "completed":
                    deps_met = False
                    break
            if not deps_met:
                continue
            if self._tab_blocked(task.tab_id):
                continue
            ready.append(task)
        return ready

    def mark_running(self, task_id: str):
        if task_id in self.tasks:
            self.tasks[task_id].status = "running"
            self.running_tasks.add(task_id)

    def mark_completed(self, task_id: str, result: Any = None):
        if task_id in self.tasks:
            self.tasks[task_id].status = "completed"
            self.tasks[task_id].result = result
            self.running_tasks.discard(task_id)

    def mark_failed(self, task_id: str, error: Any = None):
        if task_id in self.tasks:
            self.tasks[task_id].status = "failed"
            if error is not None:
                self.tasks[task_id].result = {"error": str(error)}
            self.running_tasks.discard(task_id)

    def run_ready(self, plan_fn) -> List[AgentTask]:
        """Execute all ready tasks via plan_fn(task) -> result (plan-only, no auto-act)."""
        ran = []
        for task in self.get_ready_tasks():
            self.mark_running(task.task_id)
            try:
                result = plan_fn(task)
                self.mark_completed(task.task_id, result)
            except Exception as e:
                self.mark_failed(task.task_id, e)
            ran.append(self.tasks[task.task_id])
        return ran
