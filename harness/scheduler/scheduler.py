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
        self.tasks[task_id] = AgentTask(
            task_id=task_id, tab_id=tab_id, intent=intent, 
            dependencies=deps or set()
        )

    def get_ready_tasks(self) -> List[AgentTask]:
        """Returns tasks whose dependencies are met and tabs are available."""
        ready = []
        for tid, task in self.tasks.items():
            if task.status == "pending":
                # Check dependencies
                deps_met = all(self.tasks[d].status == "completed" for d in task.dependencies)
                if deps_met:
                    # Check if tab is not human-locked (simplified for Beta.3 prototype)
                    # In real system, we check ownership.is_free(task.tab_id)
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
            self.running_tasks.remove(task_id)

    def mark_failed(self, task_id: str):
        if task_id in self.tasks:
            self.tasks[task_id].status = "failed"
            self.running_tasks.remove(task_id)
