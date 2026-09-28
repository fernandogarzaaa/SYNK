"""WorldState: event-sourced canonical browser state (Stage B / mandate Phase 2).

Design: an append-only EventJournal records every typed event. A deterministic
reducer (Event -> State Transition) derives the canonical current state from
the journal. `value.changed` / `dom.changed` therefore UPDATE canonical page
state (element values, disabled/checked/selected flags, dialogs, focus) instead
of merely appending change notes.

Observation versions are monotonic ints per tab plus a sha256 snapshot hash
over canonical JSON. Python hash() is never used for identity.

Backwards-compatible surface is preserved: version, tabs, active_tab,
interaction, human, agent, env, changes, load_full(), apply_event(),
changed_summary(), prompt_section(), snapshot().
"""
from __future__ import annotations

import collections
import hashlib
import json
import time

from .session import canonical_origin, stable_id

FULL_STATE_EVENTS = {"page.loaded", "page.navigated"}

JOURNAL_KEEP = 2000
CHANGES_KEEP = 200

# Canonical per-element state fields the reducer tracks.
ELEMENT_FIELDS = ("value", "checked", "selected", "disabled", "visible",
                  "role", "name", "tag", "selector", "href", "frame_id")


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      default=str)


def snapshot_hash(state: dict) -> str:
    """sha256 over canonical JSON of deterministic state (no timestamps)."""
    return hashlib.sha256(_canonical(state).encode("utf-8")).hexdigest()


def element_key(el: dict) -> str:
    """Stable key for an element: explicit key > selector > fingerprint hash."""
    if el.get("element_key"):
        return str(el["element_key"])
    if el.get("selector"):
        return f"sel:{el['selector']}"
    fp = {k: el.get(k) for k in ("role", "name", "tag", "href")}
    return stable_id("el", _canonical(fp))


class EventJournal:
    """Append-only journal of typed runtime events."""

    def __init__(self, keep: int = JOURNAL_KEEP):
        self._events: collections.deque = collections.deque(maxlen=keep)
        self.seq = 0

    def append(self, type: str, data: dict | None = None) -> dict:
        self.seq += 1
        ev = {"seq": self.seq, "ts": time.time(), "type": type,
              "data": data or {}}
        self._events.append(ev)
        return ev

    def __len__(self) -> int:
        return len(self._events)

    def events(self) -> list[dict]:
        return list(self._events)

    def since(self, seq: int) -> list[dict]:
        return [e for e in self._events if e["seq"] > seq]


def _blank_tab(tab_id: str) -> dict:
    return {"tab_id": tab_id, "url": "", "title": "", "origin": "",
            "viewport": "desktop", "nodes": [],
            "elements": {},          # element_key -> canonical element state
            "focus": None, "modal": None,
            "human": {},             # current_target, recent_actions, intent
            "observation_version": 0, "observation_id": None,
            "snapshot_hash": None}


class WorldState:
    def __init__(self):
        self.journal = EventJournal()
        self.version = 0                       # global monotonic version
        self.tabs: dict[str, dict] = {}        # tab_id -> canonical tab state
        self.active_tab: str = "default"
        self.interaction: dict = {}            # legacy compat mirror
        self.human: dict = {}                  # legacy compat mirror
        self.agent: dict = {}                  # plan, current_action, leases, goal
        self.env: dict = {}                    # network, navigation, downloads, auth
        self.changes: list[str] = []           # human-readable notes (bounded)
        self.updated = time.time()

    # -- ingestion -----------------------------------------------------------
    def load_full(self, url: str, nodes: list, title: str = "",
                  viewport: str = "desktop", tab_id: str = "default",
                  session_id: str | None = None,
                  window_id: str = "win_default") -> int:
        """Full snapshot ingest: journaled as page.loaded and reduced."""
        return self.apply_event({
            "type": "page.loaded",
            "data": {"url": url, "title": title, "nodes": nodes,
                     "viewport": viewport, "tab_id": tab_id,
                     "session_id": session_id, "window_id": window_id,
                     "full": True},
        })

    def apply_event(self, event: dict) -> int:
        """Journal the event, then deterministically reduce it into state."""
        t = event.get("type", "")
        d = event.get("data", {}) or {}
        self.journal.append(t, d)
        self.version += 1
        self._reduce(t, d)
        self.updated = time.time()
        return self.version

    # -- deterministic reducer: Event -> State Transition --------------------
    def _tab(self, tab_id: str) -> dict:
        tab = self.tabs.get(tab_id)
        if tab is None:
            tab = _blank_tab(tab_id)
            self.tabs[tab_id] = tab
        return tab

    def _note(self, text: str) -> None:
        self.changes.append(text)
        del self.changes[:-CHANGES_KEEP]

    def _merge_element(self, tab: dict, el: dict) -> None:
        key = element_key(el)
        cur = tab["elements"].get(key, {"element_key": key})
        for f in ELEMENT_FIELDS:
            if f in el and el[f] is not None:
                cur[f] = el[f]
        tab["elements"][key] = cur

    def _observe(self, tab: dict) -> None:
        """New immutable observation version for the tab's canonical state."""
        tab["observation_version"] += 1
        tab["snapshot_hash"] = snapshot_hash({
            "url": tab["url"], "title": tab["title"],
            "elements": tab["elements"], "modal": tab["modal"],
            "focus": tab["focus"]})
        tab["observation_id"] = stable_id(
            "obs", tab["tab_id"], str(tab["observation_version"]),
            tab["snapshot_hash"])

    def _reduce(self, t: str, d: dict) -> None:
        tab_id = d.get("tab_id") or self.active_tab
        tab = self._tab(tab_id)

        if t in ("page.loaded", "page.navigated"):
            url = d.get("url", "")
            # Navigation to a new document invalidates prior element state.
            if t == "page.navigated" or d.get("full"):
                if url and url != tab["url"]:
                    tab["elements"] = {}
                    tab["modal"] = None
                    tab["focus"] = None
            tab["url"] = url or tab["url"]
            tab["title"] = d.get("title", tab["title"])
            tab["origin"] = canonical_origin(tab["url"])
            tab["viewport"] = d.get("viewport", tab.get("viewport", "desktop"))
            nodes = d.get("nodes")
            if nodes is not None:
                tab["nodes"] = nodes
                for n in nodes:
                    if isinstance(n, dict):
                        self._merge_element(tab, n)
            self.active_tab = tab_id
            self._observe(tab)
            self._note(f"[{tab_id}] {t} -> {tab['url']}")

        elif t == "dom.changed":
            elements = d.get("elements")
            if elements:
                for el in elements:
                    if isinstance(el, dict):
                        self._merge_element(tab, el)
            elif d.get("target"):
                patch = {"selector": d["target"]}
                state = d.get("state")
                if isinstance(state, dict):
                    patch.update({k: v for k, v in state.items()
                                  if k in ELEMENT_FIELDS})
                elif d.get("detail"):
                    patch["value"] = d["detail"]
                self._merge_element(tab, patch)
            self._observe(tab)
            self._note(f"[{tab_id}] dom.changed {d.get('target', '?')}")

        elif t == "value.changed":
            target = d.get("target", "?")
            value = d.get("value", d.get("detail"))
            patch = {"selector": target}
            if value is not None:
                patch["value"] = value
            for f in ("checked", "selected", "disabled"):
                if f in d:
                    patch[f] = d[f]
            self._merge_element(tab, patch)
            self._observe(tab)
            self._note(f"[{tab_id}] value.changed {target}")

        elif t == "focus.changed":
            tab["focus"] = d.get("target")
            self.interaction["focus"] = d.get("target")
            self.interaction["active_tab"] = tab_id
            self._note(f"[{tab_id}] focus = {d.get('target')}")

        elif t == "human.action":
            tab["human"]["current_target"] = d.get("target")
            self.human["current_target"] = d.get("target")
            recent = tab["human"].setdefault("recent_actions", [])
            recent.append(d)
            del recent[:-20]
            self.human.setdefault("recent_actions", []).append(d)
            self.human["recent_actions"] = self.human["recent_actions"][-20:]
            if d.get("intent"):
                tab["human"]["intent"] = d["intent"]
                self.human["intent"] = d["intent"]
            # A human edit of a field updates canonical element state too.
            if d.get("kind") in ("type", "key") and d.get("target"):
                patch = {"selector": d["target"]}
                if d.get("value") is not None:
                    patch["value"] = d["value"]
                self._merge_element(tab, patch)
                self._observe(tab)
            self._note(f"[{tab_id}] human -> {d.get('kind', '?')} on {d.get('target', '?')}")

        elif t == "agent.action":
            self.agent["current_action"] = d
            self._note(f"[{tab_id}] agent -> {d.get('command', '?')} "
                       f"on {d.get('target', d.get('ref', '?'))}")

        elif t in ("dialog.opened", "dialog.closed"):
            tab["modal"] = d.get("target") if t == "dialog.opened" else None
            self.interaction["modal"] = tab["modal"]
            self._observe(tab)
            self._note(f"[{tab_id}] modal {t.split('.')[1]}: {d.get('target')}")

        elif t == "agent.lease":
            self.agent.setdefault("leases", {})[d.get("target", "?")] = d.get("lease")
            self._note(f"[{tab_id}] lease {d.get('lease')} on {d.get('target')}")

        elif t == "agent.goal":
            self.agent["goal"] = d.get("goal")
            self._note(f"agent goal: {d.get('goal')}")

        elif t == "tab.activated":
            self.active_tab = tab_id
            self._note(f"[{tab_id}] activated")

        elif t in ("tab.opened", "tab.closed"):
            if t == "tab.closed":
                self.tabs.pop(tab_id, None)
                if self.active_tab == tab_id:
                    self.active_tab = next(iter(self.tabs), "default")
            self._note(f"[{tab_id}] {t}")

        else:
            self._note(f"[{tab_id}] {t}: {str(d)[:120]}")

    # -- canonical reads -----------------------------------------------------
    def element_state(self, tab_id: str, target: str) -> dict | None:
        """Canonical current state of an element (by selector or key)."""
        tab = self.tabs.get(tab_id)
        if not tab:
            return None
        el = tab["elements"].get(target) or tab["elements"].get(f"sel:{target}")
        return dict(el) if el else None

    def element_value(self, tab_id: str, target: str):
        """Answer 'what is the current value of X' from canonical state."""
        el = self.element_state(tab_id, target)
        return el.get("value") if el else None

    def tab_observation(self, tab_id: str) -> dict | None:
        tab = self.tabs.get(tab_id)
        if not tab:
            return None
        return {"tab_id": tab_id,
                "observation_version": tab["observation_version"],
                "observation_id": tab["observation_id"],
                "snapshot_hash": tab["snapshot_hash"],
                "url": tab["url"], "origin": tab["origin"]}

    @property
    def page(self) -> dict:
        """Active tab's canonical state (compat for precondition checks)."""
        tab = self.tabs.get(self.active_tab) or {}
        flat = {"url": tab.get("url", ""), "title": tab.get("title", ""),
                "origin": tab.get("origin", ""), "modal": tab.get("modal"),
                "focus": tab.get("focus")}
        for key, el in (tab.get("elements") or {}).items():
            if isinstance(el, dict) and "value" in el:
                flat[f"element:{key}"] = el["value"]
        return flat

    def replay(self, events: list[dict]) -> "WorldState":
        """Rebuild state from a journal (event sourcing sanity check)."""
        fresh = WorldState()
        for e in events:
            fresh.apply_event({"type": e["type"], "data": e["data"]})
        return fresh

    # -- legacy reads ----------------------------------------------------------
    def changed_summary(self, max_items: int = 12) -> str:
        items = self.changes[-max_items:]
        extra = (f" (+{len(self.changes) - max_items} more)"
                 if len(self.changes) > max_items else "")
        return "; ".join(items) + extra if items else "no relevant changes"

    def prompt_section(self) -> str:
        a = self.agent
        current_page = self.tabs.get(self.active_tab, {})
        obs = self.tab_observation(self.active_tab) or {}
        return (
            f"WORLD_STATE v{self.version} active_tab={self.active_tab} "
            f"url={current_page.get('url', '?')} "
            f"obs_v{obs.get('observation_version', 0)}\n"
            f"Changed: {self.changed_summary()}\n"
            f"human_focus = {self.human.get('current_target')}\n"
            f"agent_leases = {list(a.get('leases', {}).keys())}\n"
            f"modal = {self.interaction.get('modal')}\n"
            f"No other relevant changes."
        )

    def snapshot(self) -> dict:
        return {"version": self.version,
                "journal_seq": self.journal.seq,
                "tabs": {tid: {"url": t["url"], "title": t["title"],
                               "origin": t["origin"], "nodes": t["nodes"],
                               "viewport": t.get("viewport", "desktop"),
                               "elements": t["elements"],
                               "observation_version": t["observation_version"],
                               "observation_id": t["observation_id"],
                               "snapshot_hash": t["snapshot_hash"]}
                         for tid, t in self.tabs.items()},
                "active_tab": self.active_tab,
                "interaction": self.interaction, "human": self.human,
                "agent": {k: v for k, v in self.agent.items() if k != "plan"},
                "env": self.env, "changes": self.changes[-20:]}
