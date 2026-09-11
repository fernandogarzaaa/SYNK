"""Event bus: browser becomes event-driven, not polling-driven (Beta §14).

Typed events flow: Event Collector -> bus -> HumanTracker / WorldState /
AgentHarness -> Policy Engine -> Orchestrator. Stdlib only, keeps last N.
"""
from __future__ import annotations

import collections
import time

EVENT_TYPES = (
    "page.loaded", "page.navigated", "dom.changed", "value.changed",
    "focus.changed", "input.changed", "click.human", "scroll.human",
    "human.action", "dialog.opened", "dialog.closed", "download.started",
    "auth.detected", "agent.action", "agent.action_failed",
    "agent.action_verified", "agent.lease", "agent.goal", "network.result",
)

KEEP = 200


class EventBus:
    def __init__(self):
        self._subs: dict[str, list] = {}
        self.events: collections.deque = collections.deque(maxlen=KEEP)
        self.seq = 0

    def subscribe(self, event_type: str, fn) -> None:
        self._subs.setdefault(event_type, []).append(fn)

    def emit(self, type: str, data: dict | None = None) -> dict:
        self.seq += 1
        ev = {"seq": self.seq, "ts": time.time(), "type": type,
              "data": data or {}}
        self.events.append(ev)
        for fn in self._subs.get(type, []) + self._subs.get("*", []):
            try:
                fn(ev)
            except Exception:
                pass  # a subscriber must never break the bus
        return ev

    def recent(self, n: int = 50, type: str | None = None) -> list[dict]:
        evs = list(self.events)
        if type:
            evs = [e for e in evs if e["type"] == type]
        return evs[-n:]
