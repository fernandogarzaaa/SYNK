"""WorldState: the browser as an observable, addressable world (Beta §5).

Event-sourced: full state on load, then typed diffs/events. The LLM receives
"WORLD_STATE vN + changed keys" instead of a full page re-dump every step.
Builds on ContextManager (which still owns ref assignment/trimming).
"""
from __future__ import annotations

import time

FULL_STATE_EVENTS = {"page.loaded", "page.navigated"}


class WorldState:
    def __init__(self):
        self.version = 0
        self.tabs: dict[str, dict] = {}     # tab_id -> {url, title, nodes, viewport}
        self.active_tab: str = "default"
        self.interaction: dict = {}       # focus, selection, active_element, overlays, modals
        self.human: dict = {}              # current_target, active_task, cursor, recent_actions, intent
        self.agent: dict = {}              # plan, current_action, leases, confidence, goal
        self.env: dict = {}                # network, navigation, downloads, auth
        self.changes: list[str] = []       # changed keys since last prompt
        self.updated = time.time()

    # -- ingestion -----------------------------------------------------------
    def load_full(self, url: str, nodes: list, title: str = "",
                  viewport: str = "desktop", tab_id: str = "default") -> int:
        self.version += 1
        self.tabs[tab_id] = {"url": url, "title": title, "nodes": nodes,
                             "viewport": viewport}
        self.active_tab = tab_id
        self.changes = [f"FULL STATE loaded in tab {tab_id}: {url} ({len(nodes)} elements)"]
        self.updated = time.time()
        return self.version

    def apply_event(self, event: dict) -> int:
        """Apply a typed bus event; record only the changed keys."""
        self.version += 1
        t = event.get("type", "")
        d = event.get("data", {})
        tab_id = d.get("tab_id", self.active_tab)
        c = self.changes
        if t in ("dom.changed", "value.changed"):
            c.append(f"[{tab_id}] {d.get('target', '?')} {d.get('detail', 'changed')}")
        elif t == "focus.changed":
            self.interaction["focus"] = d.get("target")
            self.interaction["active_tab"] = tab_id
            c.append(f"[{tab_id}] focus = {d.get('target')}")
        elif t == "human.action":
            self.human["current_target"] = d.get("target")
            self.human.setdefault("recent_actions", []).append(d)
            self.human["recent_actions"] = self.human["recent_actions"][-20:]
            if d.get("intent"):
                self.human["intent"] = d["intent"]
            c.append(f"[{tab_id}] human -> {d.get('kind', '?')} on {d.get('target', '?')}")
        elif t == "agent.action":
            self.agent["current_action"] = d
            c.append(f"[{tab_id}] agent -> {d.get('command', '?')} "
                     f"on {d.get('target', d.get('ref', '?'))}")
        elif t in ("dialog.opened", "dialog.closed"):
            self.interaction["modal"] = d.get("target") if t == "dialog.opened" else None
            c.append(f"[{tab_id}] modal {t.split('.')[1]}: {d.get('target')}")
        elif t in ("page.navigated", "page.loaded"):
            self.tabs.setdefault(tab_id, {})["url"] = d.get("url", "")
            self.active_tab = tab_id
            c.append(f"[{tab_id}] navigation -> {d.get('url', '')}")
        elif t == "agent.lease":
            self.agent.setdefault("leases", {})[d.get("target", "?")] = d.get("lease")
            c.append(f"[{tab_id}] lease {d.get('lease')} on {d.get('target')}")
        elif t == "agent.goal":
            self.agent["goal"] = d.get("goal")
            c.append(f"agent goal: {d.get('goal')}")
        else:
            c.append(f"[{tab_id}] {t}: {str(d)[:120]}")
        self.updated = time.time()
        return self.version

    # -- reads ---------------------------------------------------------------
    def changed_summary(self, max_items: int = 12) -> str:
        items = self.changes[-max_items:]
        extra = f" (+{len(self.changes) - max_items} more)" if len(self.changes) > max_items else ""
        return "; ".join(items) + extra if items else "no relevant changes"

    def prompt_section(self) -> str:
        """Compact world block for the LLM prompt."""
        a = self.agent
        current_page = self.tabs.get(self.active_tab, {})
        return (
            f"WORLD_STATE v{self.version} active_tab={self.active_tab} url={current_page.get('url', '?')}\n"
            f"Changed: {self.changed_summary()}\n"
            f"human_focus = {self.human.get('current_target')}\n"
            f"agent_leases = {list(a.get('leases', {}).keys())}\n"
            f"modal = {self.interaction.get('modal')}\n"
            f"No other relevant changes."
        )

    def snapshot(self) -> dict:
        return {"version": self.version, "tabs": self.tabs, "active_tab": self.active_tab,
                "interaction": self.interaction, "human": self.human,
                "agent": {k: v for k, v in self.agent.items() if k != "plan"},
                "env": self.env, "changes": self.changes[-20:]}
