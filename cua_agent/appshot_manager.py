"""
appshot_manager.py — Manages appshots (state snapshots with diffs).

Each appshot captures the state at a point in time and computes
what changed since the previous observation. This is critical for
text-only agents — instead of re-reading the full state each turn,
they can see a concise diff.

Appshot format:
{
    "id": int,
    "summary": "Window 'Safari' | 3 fields visible",
    "type": "initial" | "state_change" | "no_change",
    "diff": {
        "type": "initial" | "state_change" | "no_change",
        "added_count": 0,
        "removed_count": 0,
        "focus_changed_to": "Button='OK'",
        "changed_elements": [...]
    },
    "important": bool,
    "timestamp": float
}
"""

from __future__ import annotations

import time
from typing import Any


class AppshotManager:
    """Generates and tracks appshots (state snapshots with diffs)."""

    def __init__(self):
        self._shot_id: int = 0
        self._previous_state: dict | None = None

    def take_snapshot(self, appstate: dict) -> dict:
        """Take an appshot of the current state.

        Computes a diff from the previous state (if any) and returns
        a structured snapshot.

        Args:
            appstate: The full accessibility tree state dict.

        Returns:
            An appshot dict with summary, diff, and metadata.
        """
        self._shot_id += 1
        prev = self._previous_state
        self._previous_state = appstate

        diff = self._compute_diff(prev, appstate)
        summary = self._build_summary(appstate, diff)

        return {
            "id": self._shot_id,
            "summary": summary,
            "type": diff.get("type", "initial"),
            "diff": diff,
            "important": diff.get("type") != "no_change",
            "timestamp": time.time(),
        }

    def reset(self) -> None:
        """Reset the shot counter and clear previous state."""
        self._shot_id = 0
        self._previous_state = None

    # ── Internal ──

    def _compute_diff(
        self, prev: dict | None, curr: dict
    ) -> dict:
        """Compute what changed between two appstates.

        For text-only LLMs, the diff focuses on:
        - New elements that appeared (modal dialogs, dropdowns)
        - Elements that disappeared (dismissed menus)
        - Focus changes (what's now selected/focused)
        - Value changes (text entered, sliders moved)
        """
        if prev is None:
            return {
                "type": "initial",
                "added_count": 0,
                "removed_count": 0,
                "focus_changed_to": None,
                "changed_elements": [],
            }

        prev_titles = self._flatten_elements(prev)
        curr_titles = self._flatten_elements(curr)

        prev_set = {e["key"] for e in prev_titles}
        curr_set = {e["key"] for e in curr_titles}

        added = curr_set - prev_set
        removed = prev_set - curr_set

        # Find focus changes
        curr_focused = [e for e in curr_titles if e.get("focused")]
        prev_focused = [e for e in prev_titles if e.get("focused")]
        focus_change = None
        if curr_focused != prev_focused:
            if curr_focused:
                f = curr_focused[0]
                focus_change = f"{f.get('role', '?')}='{f.get('title', '')}'"

        # Find value changes
        changed = []
        curr_by_key = {e["key"]: e for e in curr_titles}
        prev_by_key = {e["key"]: e for e in prev_titles}
        common = curr_set & prev_set
        for key in common:
            cv = curr_by_key[key].get("value")
            pv = prev_by_key[key].get("value")
            if cv != pv and (cv or pv):
                changed.append({
                    "key": key,
                    "value_was": pv,
                    "value_now": cv,
                })

        return {
            "type": "state_change" if (added or removed or focus_change or changed)
                    else "no_change",
            "added_count": len(added),
            "removed_count": len(removed),
            "focus_changed_to": focus_change,
            "changed_elements": changed[:5],  # Limit to 5
        }

    def _build_summary(self, appstate: dict, diff: dict) -> str:
        """Build a human-readable summary line for the appshot."""
        role = appstate.get("role", "").replace("AX", "")
        title = appstate.get("title", "")

        d = diff
        if d.get("type") == "initial":
            return f"{role} '{title}' | initial state"
        elif d.get("type") == "no_change":
            return f"{role} '{title}' | no change"
        else:
            parts = [f"{role} '{title}'"]
            if d.get("added_count"):
                parts.append(f"+{d['added_count']}")
            if d.get("removed_count"):
                parts.append(f"-{d['removed_count']}")
            if d.get("focus_changed_to"):
                parts.append(f"focus→{d['focus_changed_to']}")
            if d.get("changed_elements"):
                parts.append(f"{len(d['changed_elements'])} values changed")
            return " | ".join(parts)

    def _flatten_elements(self, node: dict) -> list[dict]:
        """Flatten tree to flat keyed elements for diff comparison."""
        result: list[dict] = []

        def _walk(n: dict, _depth: int = 0):
            role = n.get("role", "")
            title = n.get("title", "") or n.get("label", "") or ""
            value = n.get("value")
            focused = n.get("focused", False)
            pos = n.get("pos")
            # Build a reasonably unique key
            pos_part = ""
            if pos:
                pos_part = f"@{pos.get('x', 0):.0f},{pos.get('y', 0):.0f}"
            key = f"{role}|{title[:40]}{pos_part}"
            if role:
                result.append({
                    "key": key,
                    "role": role,
                    "title": title,
                    "value": value,
                    "focused": focused,
                })
            for child in n.get("children", []):
                _walk(child, _depth + 1)

        _walk(node)
        return result
