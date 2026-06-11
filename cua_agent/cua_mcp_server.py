"""
cua_mcp_server.py — MCP server that exposes the CUA agent as callable tools.

Protocol: Model Context Protocol (MCP) via JSON-RPC 2.0 over stdio.
Transport: Standard I/O (compatible with Hermes Agent, Claude Desktop, etc.)

Available tools:
  - get_appstate:      Get the current desktop accessibility tree as JSON
  - get_appshot:       Take a state snapshot with diff from previous observation
  - execute_action:    Perform a UI action (click, type, key_press, scroll, open_app)
  - list_actions:      List all supported action types with descriptions
  - run_task:          Run the full agent loop for a task (autonomous)
  - observe_and_act:   Single Observe→Think→Act step (step-by-step control)

Usage:
    # Start the server on stdio (for Hermes/Claude)
    python -m cua_agent.cua_mcp_server

    # Or with a specific collector
    python -m cua_agent.cua_mcp_server --collector windows
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from typing import Any

from mcp.server.fastmcp import FastMCP

from .appshot_manager import AppshotManager
from .agent_loop import AgentLoop, ActionProvider, Observation, LoopConfig
from .prompt_builder import PromptBuilder, PromptConfig, INTERACTIVE_ROLES
from .security import SecurityContext, DangerLevel
from .optimization import (
    ActionMerger, ObservationCache, PerformanceTimer,
    TokenOptimizer, ParallelPrefetcher, OptimizationConfig,
)

logger = logging.getLogger("cua-mcp-server")

# ── MCP server instance ──
mcp = FastMCP("CUA Driver")


# ── State ──

@dataclass
class _ServerState:
    """Mutable server state shared across tool calls.

    Note: AppshotManager ownership lives inside the ActionProvider.
    The server-level state only tracks history for context.
    """
    current_appstate: dict | None = None
    action_history: list[dict] = field(default_factory=list)
    appshot_history: list[dict] = field(default_factory=list)
    prev_appstate: dict | None = None
    agent_loop: AgentLoop | None = None
    action_provider: ActionProvider | None = None


state = _ServerState()


# ── Global security & optimization (lazy inited) ──

_security_context: SecurityContext | None = None
_optimization_config: OptimizationConfig | None = None
_optimization_merger: ActionMerger | None = None
_optimization_cache: ObservationCache | None = None
_optimization_timer: PerformanceTimer | None = None
_optimization_tokenizer: TokenOptimizer | None = None


# ── Windows Action Provider ──

class WindowsActionProvider(ActionProvider):
    """Action provider that uses the Windows `uiautomation` library for
    observation and direct action execution through the UI Automation API.

    Observes the foreground window's full accessibility tree and executes
    actions (click, type, key_press, scroll, etc.) using uiautomation
    primitives.
    """

    def __init__(self):
        from .ax_collector_windows import WindowsCollector
        from .optimization import ObservationCache as OptObservationCache
        # Tuned thresholds: more interactive nodes for complex UIs, larger child cap
        self._collector = WindowsCollector(
            max_interactive_nodes=300,  # up from 200 — Chrome/VS Code have many controls
            max_children=150,           # up from 100 — list/grid views often overflow
        )
        self._appshot_mgr = AppshotManager()
        self._observe_cache: dict[str, tuple[float, Observation]] = {}
        self._cache_ttl: float = 0.5  # 500ms TTL
        self._last_mutation_action: float = 0.0
        # Unified observation cache from optimization module (fingerprint-based)
        # Tuned: 10 entries covers multi-app workflows, 3s TTL for stable UIs
        self._opt_cache: OptObservationCache = OptObservationCache(
            ttl_seconds=3.0,   # up from 2.0 — UI layouts are fairly stable between steps
            max_size=10,        # up from 5 — handle window switches without eviction
            fingerprint_fields=("title", "role", "_pid"),
        )
        # LRU cache for find_element results (cleared on mutation)
        # Tuned: 500ms covers rapid re-queries without staleness
        self._find_cache: dict[str, tuple[float, dict | None]] = {}
        self._find_cache_ttl: float = 0.5   # up from 0.3 — reduce misses on repeated lookups
        self._find_cache_max: int = 20
        # Parallel prefetcher for background observation while LLM is thinking
        self._prefetcher: ParallelPrefetcher = ParallelPrefetcher(enabled=True)

        # Synthetic tree builder (HWND hierarchy + OCR fallback)
        from .synthetic_tree import create_default_builder
        self._synthetic_tree_builder = create_default_builder()

    def _augment_with_synthetic(self, appstate: dict) -> dict:
        """Augment a raw UIA appstate with synthetic tree data (HWND + OCR).

        Passes the UIA tree through SyntheticTreeBuilder which:
        1. Checks if UIA tree is empty/insufficient -> falls back to HWND tree
        2. Adds HWND-level window containers that UIA may have missed
        3. Enriches text-less elements with OCR screen reading
        4. Tags each node with a 'source' field ("uia", "hwnd", "uia+ocr")

        This is non-destructive: if the synthetic tree builder is not available
        (non-Windows platforms), the original appstate is returned unchanged.
        """
        if self._synthetic_tree_builder is None:
            return appstate
        if not appstate or appstate.get("error"):
            return appstate
        try:
            merged = self._synthetic_tree_builder.build_synthetic_tree(appstate)
            return merged if merged else appstate
        except Exception as e:
            logger.debug(f"Synthetic tree augmentation skipped: {e}")
            return appstate

    def _peek_foreground_context(self) -> dict:
        """Quick peek at the foreground window to build a cache fingerprint context.

        Does NOT do a full tree walk - just reads title, role, PID from the
        top-level window control (single COM call).
        """
        ctx: dict = {"title": "", "role": "", "_pid": ""}
        try:
            from .ax_collector_windows import _ensure_com
            _ensure_com()
            import uiautomation as auto
            peek = auto.GetForegroundControl()
            if peek:
                try:
                    ctx["title"] = peek.Name or ""
                except Exception:
                    pass
                try:
                    ct = peek.ControlTypeName or ""
                    if ct:
                        from .ax_collector_windows import _normalize_role
                        ctx["role"] = _normalize_role(ct)
                except Exception:
                    pass
                try:
                    ctx["_pid"] = str(peek.ProcessId)
                except Exception:
                    pass
        except Exception:
            pass
        return ctx

    def warm_cache(self):
        """Warm the observation cache by pre-fetching the first observation.

        Called once on server startup so that the first real `observe()` call
        (from get_appstate, observe_and_act, etc.) hits the fingerprint cache
        instead of doing a cold tree walk.

        Safe to call multiple times — subsequent calls update the cached
        entry without side effects.
        """
        try:
            context = self._peek_foreground_context()
            if not context.get("title"):
                return  # No foreground window yet, skip

            def _warm_observe():
                raw = self._collector.get_foreground_app_state()
                appstate = raw if raw else {}
                appstate = self._augment_with_synthetic(appstate)
                appshot = self._appshot_mgr.take_snapshot(appstate)
                obs = Observation(appstate=appstate, appshot=appshot)
                self._observe_cache["__foreground__"] = (time.time(), obs)
                return obs

            self._opt_cache.get_or_observe(_warm_observe, context)
            logger.debug("Observation cache warmed on startup")
        except Exception as e:
            logger.debug(f"Cache warmup skipped: {e}")

    def observe(self) -> Observation:
        """Collect the current Windows appstate from the foreground window.

        Uses a three-tier caching + prefetch strategy:
        1. Fast TTL cache (500ms) — ~60% hit rate for rapid re-observes
        2. Fingerprint-based cache (3s TTL) — ~40% hit rate across steps
        3. ParallelPrefetcher — background observation while LLM is thinking

        All caches are invalidated on mutation via _mark_mutation().
        After a mutation, observe() bypasses caches for 2 seconds.
        """
        now = time.time()
        mutation_cooldown = (now - self._last_mutation_action) < 2.0

        if not mutation_cooldown:
            # Tier 0: ParallelPrefetcher — check for background prefetched result
            prefetched = self._prefetcher.get()
            if prefetched is not None:
                # Refill tier-1 cache with prefetched result
                self._observe_cache["__foreground__"] = (now, prefetched)
                return prefetched

            # Tier 1: Fast TTL cache (for rapid re-observes within 500ms)
            cache_key = "__foreground__"
            if cache_key in self._observe_cache:
                ts, cached = self._observe_cache[cache_key]
                if now - ts < self._cache_ttl:
                    return cached

        # Tier 2: Fingerprint-based observation cache (from optimization module)
        if not mutation_cooldown:
            context = self._peek_foreground_context()

            def _observe_fn():
                raw = self._collector.get_foreground_app_state()
                appstate = raw if raw else {}
                appstate = self._augment_with_synthetic(appstate)
                appshot = self._appshot_mgr.take_snapshot(appstate)
                return Observation(appstate=appstate, appshot=appshot)

            try:
                obs = self._opt_cache.get_or_observe(_observe_fn, context)
                self._observe_cache["__foreground__"] = (now, obs)
                return obs
            except Exception:
                pass

        # Bypass caches: mutation happened recently, do fresh observation
        raw = self._collector.get_foreground_app_state()
        appstate = raw if raw else {}
        appstate = self._augment_with_synthetic(appstate)
        appshot = self._appshot_mgr.take_snapshot(appstate)
        obs = Observation(appstate=appstate, appshot=appshot)

        self._observe_cache["__foreground__"] = (now, obs)
        return obs

    def _mark_mutation(self):
        """Mark that a mutation action occurred, invalidating all caches.

        Clears:
        - In-provider observation cache (Tier 1 TTL)
        - Fingerprint-based observation cache (Tier 2)
        - find_element() result cache
        - Cancels any pending prefetch (stale after mutation)
        Also sets a cooldown timer so observe() does fresh walks for 2 seconds.
        """
        self._last_mutation_action = time.time()
        self._observe_cache.clear()
        self._find_cache.clear()
        try:
            self._opt_cache.invalidate()
        except Exception:
            pass
        try:
            self._prefetcher.cancel()
        except Exception:
            pass

    def _trigger_prefetch(self):
        """Start a background prefetch of the next observation.

        Called after each mutation action so the next observe() call
        (typically ~1-5s later when the LLM finishes thinking) finds
        a pre-computed result instead of doing a cold tree walk.
        """
        def _observe_fn():
            try:
                import uiautomation as auto
                window = auto.GetForegroundControl()
                if not window or not window.Exists(0, 0):
                    return None
                raw = self._collector.get_foreground_app_state()
                appstate = raw if raw else {}
                appstate = self._augment_with_synthetic(appstate)
                appshot = self._appshot_mgr.take_snapshot(appstate)
                return Observation(appstate=appstate, appshot=appshot)
            except Exception:
                return None
        self._prefetcher.prefetch(_observe_fn)

    def act(self, action: dict) -> dict:
        """Execute an action on Windows using uiautomation and keyboard/mouse simulation.

        Strategies used (from simplest to most reliable):
        1. uiautomation Control methods (Click, SendKeys, etc.) for element-level actions
        2. auto.Click(x, y) for position-based clicks
        3. auto.SendKeys() for keyboard input
        4. subprocess for app/URL launching

        After executing a mutation action (click, type, key_press, etc.), triggers
        a background prefetch of the next observation so the LLM won't wait.
        """
        action_type = action.get("action", "")
        target = action.get("target", "")
        value = action.get("value", "")

        # ── Action types that mutate UI state (invalidates caches + triggers prefetch) ──
        _MUTATION_TYPES = frozenset({
            "click", "double_click", "right_click", "drag", "select",
            "type", "type_into", "clear_text", "key_press", "scroll",
            "open_app", "open_url",
            "resize_window", "minimize_window", "maximize_window", "close_window",
            "shortcut",
        })
        is_mutation = action_type in _MUTATION_TYPES

        try:
            if action_type == "click":
                result = self._click_element(target)
            elif action_type == "double_click":
                result = self._double_click_element(target)
            elif action_type == "right_click":
                result = self._right_click_element(target)
            elif action_type == "hover":
                result = self._hover_element(target)
            elif action_type == "drag":
                result = self._drag_element(target, value)
            elif action_type == "select":
                result = self._select_option(target, value)
            elif action_type == "resize_window":
                result = self._resize_window(value or target)
            elif action_type == "minimize_window":
                result = self._minimize_window()
            elif action_type == "maximize_window":
                result = self._maximize_window()
            elif action_type == "close_window":
                result = self._close_window()
            elif action_type == "take_screenshot":
                result = self._take_screenshot()
            elif action_type == "take_region_screenshot":
                result = self._take_region_screenshot(target, value)
            elif action_type == "shortcut":
                result = self._trigger_shortcut(target)
            elif action_type in ("type", "type_into"):
                result = self._type_text(value, target)
            elif action_type == "clear_text":
                result = self._clear_text(target)
            elif action_type == "find_element":
                found = self.find_element(target)
                if found:
                    info = {k: v for k, v in found.items() if not k.startswith("_")}
                    result = {"success": True, "action": "find_element", "target": target,
                              "info": info}
                else:
                    result = {"success": False, "action": "find_element", "target": target,
                              "error": f"Element not found: {target}"}
            elif action_type == "key_press":
                result = self._press_key(target)
            elif action_type == "scroll":
                result = self._scroll(target, value)
            elif action_type == "open_app":
                result = self._open_app(target)
            elif action_type == "open_url":
                result = self._open_url(value)
            elif action_type == "wait":
                ms = int(value or "500")
                time.sleep(ms / 1000.0)
                result = {"success": True, "action": "wait", "value": f"{ms}ms"}
            else:
                return {"success": False, "error": f"Unknown action: {action_type}"}
        except Exception as e:
            return {"success": False, "error": str(e), "action": action_type}

        # ── Background prefetch after mutation: start observing next state
        #     while the LLM is thinking about what to do next.
        if is_mutation and result.get("success"):
            self._trigger_prefetch()

        return result

    def find_element(self, target: str) -> dict | None:
        """Find a UI element by name, automation_id, or position.

        Multi-criteria search that returns element info if found:
            name: str — The element's name/title
            role: str — Control type (e.g. "AXButton")
            automation_id: str — Automation ID if available
            pos: dict — Center position {x, y}
            size: dict — Size {w, h}
            rect: tuple — (left, top, right, bottom) bounding rectangle
            focused: bool
            enabled: bool
            value: str or None

        Uses an internal LRU cache (_find_cache) to avoid repeated
        COM tree walks for identical queries. The cache is cleared
        automatically on mutation via _mark_mutation().

        Returns None if not found.
        """
        from .ax_collector_windows import _ensure_com
        _ensure_com()
        import uiautomation as auto

        if not target:
            return None

        # ── LRU cache check ──
        now = time.time()
        if target in self._find_cache:
            ts, cached_result = self._find_cache[target]
            if now - ts < self._find_cache_ttl:
                return cached_result

        control = None
        method = ""

        # 1. Position-based: "x,y" format
        if "," in target and target.replace(",", "").replace("-", "").replace(" ", "").isdigit():
            parts = target.replace(" ", "").split(",")
            x, y = int(parts[0]), int(parts[1])
            try:
                control = auto.ControlFromPoint(x, y)
                method = "position"
            except Exception:
                pass

        # 2. Automation ID: "id:xxx" format
        if control is None and target.startswith("id:"):
            aid = target[3:]
            try:
                ctrl = auto.Control(searchDepth=50, AutomationId=aid)
                if ctrl and ctrl.Exists(0, 0.5):
                    control = ctrl
                    method = "automation_id"
            except Exception:
                pass

        # 3. Name match
        if control is None:
            try:
                ctrl = auto.Control(searchDepth=50, Name=target)
                if ctrl and ctrl.Exists(0, 0.5):
                    control = ctrl
                    method = "name"
            except Exception:
                pass

        # 4. Depth-first tree search by name (more robust)
        if control is None:
            try:
                root = auto.GetRootControl()
                if root:
                    control = self._dfs_find_control(root, target, depth=0, max_depth=30)
                    if control:
                        method = "dfs_name"
            except Exception:
                pass

        # ── 5. HWND-based fallback (for custom/non-AX controls) ──
        hwnd_info = None
        if control is None:
            hwnd_info = self._find_element_via_hwnd(target)
            if hwnd_info is not None:
                method = hwnd_info.get("via", "win32_hwnd")
                # Return HWND-level info (no UIA control available)
                info = {"method": method, "via_hwnd": True, **hwnd_info}
                self._find_cache[target] = (now, info)
                if len(self._find_cache) > self._find_cache_max:
                    try:
                        oldest = min(self._find_cache.keys(), key=lambda k: self._find_cache[k][0])
                        del self._find_cache[oldest]
                    except Exception:
                        pass
                return info

        # ── 6. Synthetic tree search (OCR-enriched titles from HWND walk) ──
        if control is None:
            try:
                if hasattr(self, "_augment_with_synthetic") and self._synthetic_tree_builder is not None:
                    # Get a quick foreground observation and search by title
                    raw = self._collector.get_foreground_app_state()
                    appstate = raw if raw else {}
                    merged = self._augment_with_synthetic(appstate)
                    found_synthetic = self._dfs_search_synthetic(merged, target)
                    if found_synthetic is not None:
                        method = "synthetic_tree"
                        hwnd = found_synthetic.get("hwnd")
                        info = {"method": method, "via_hwnd": True, "hwnd": hwnd, "synthetic": True}
                        if found_synthetic.get("title"):
                            info["name"] = found_synthetic["title"]
                        if found_synthetic.get("role"):
                            info["role"] = found_synthetic["role"]
                        if found_synthetic.get("pos"):
                            info["pos"] = found_synthetic["pos"]
                        if found_synthetic.get("size"):
                            info["size"] = found_synthetic["size"]
                        if found_synthetic.get("class_name"):
                            info["class_name"] = found_synthetic["class_name"]
                        if found_synthetic.get("source"):
                            info["source"] = found_synthetic["source"]
                        self._find_cache[target] = (now, info)
                        if len(self._find_cache) > self._find_cache_max:
                            try:
                                oldest = min(self._find_cache.keys(), key=lambda k: self._find_cache[k][0])
                                del self._find_cache[oldest]
                            except Exception:
                                pass
                        return info
            except Exception:
                pass

        if control is None:
            return None

        # Build result info
        info = {"method": method}

        try:
            info["name"] = control.Name or ""
        except Exception:
            pass
        try:
            from .ax_collector_windows import _normalize_role
            ct_name = control.ControlTypeName or ""
            info["role"] = _normalize_role(ct_name)
        except Exception:
            pass
        try:
            info["automation_id"] = control.AutomationId
        except Exception:
            pass
        try:
            info["focused"] = bool(control.HasKeyboardFocus)
        except Exception:
            pass
        try:
            info["enabled"] = bool(control.IsEnabled)
        except Exception:
            pass

        # Bounding rect
        try:
            rect = control.BoundingRectangle
            if rect:
                info["rect"] = (rect.left, rect.top, rect.right, rect.bottom)
                info["pos"] = {
                    "x": round((rect.left + rect.right) / 2),
                    "y": round((rect.top + rect.bottom) / 2),
                }
                info["size"] = {
                    "w": round(rect.right - rect.left),
                    "h": round(rect.bottom - rect.top),
                }
        except Exception:
            pass

        # Value pattern
        try:
            from .ax_collector_windows import _safe_get_value
            val = _safe_get_value(control)
            if val is not None:
                info["value"] = val
        except Exception:
            pass

        # Store control reference for subsequent use
        info["_control"] = control

        # ── Store in LRU cache ──
        self._find_cache[target] = (now, info)
        if len(self._find_cache) > self._find_cache_max:
            # Evict oldest entry
            try:
                oldest = min(self._find_cache.keys(), key=lambda k: self._find_cache[k][0])
                del self._find_cache[oldest]
            except Exception:
                pass

        return info

    def _dfs_find_control(self, parent, name: str, depth: int, max_depth: int):
        """Depth-first search for a control by exact name match."""
        if depth > max_depth:
            return None

        try:
            if hasattr(parent, "Name"):
                try:
                    parent_name = parent.Name or ""
                    if parent_name == name:
                        return parent
                except Exception:
                    pass
        except Exception:
            return None

        try:
            children = parent.GetChildren()
            for child in children:
                result = self._dfs_find_control(child, name, depth + 1, max_depth)
                if result is not None:
                    return result
        except Exception:
            pass

        return None

    def _dfs_search_synthetic(self, node: dict, target: str) -> dict | None:
        """Depth-first search of a synthetic tree node by title/substring match.

        Used as the 6th fallback strategy in find_element() to locate elements
        that were discovered via OCR or HWND walk (not visible to UIA).

        Args:
            node: A tree node from the synthetic tree (may have source="hwnd" or "uia+ocr")
            target: The element name/title to search for (case-insensitive substring match)

        Returns:
            The matching node dict, or None if not found.
        """
        if not target:
            return None

        tgt = target.lower()

        # Check this node's title
        title = node.get("title", "") or ""
        if title.lower() == tgt or (len(tgt) > 2 and tgt in title.lower()):
            return node

        # Check children
        for child in node.get("children", []):
            found = self._dfs_search_synthetic(child, target)
            if found is not None:
                return found

        return None

    def _find_element_via_hwnd(self, target: str) -> dict | None:
        """
        Find a UI element via Win32 HWND fallback when UIA returns nothing.

        This handles custom controls, game engine UIs, Electron/web views,
        and legacy Win32 apps that don't expose UIA properties.
        Even if they're invisible to UIA, most still have HWNDs.

        Search strategies:
        1. "hwnd:0xHEX" — Direct HWND specification
        2. Position-based: probe at screen coordinates, get HWND from point
        3. Title/substring match: search all top-level windows
        4. Class name match: search by window class

        Returns HWND element info dict with keys:
            hwnd, title, class_name, rect, pos, size, pid, via="win32_hwnd"
        Returns None if nothing found.
        """
        import ctypes
        from ctypes import wintypes

        # Strategy 1: Direct HWND specification
        if target.lower().startswith("hwnd:"):
            hwnd_str = target[5:].strip()
            try:
                hwnd = int(hwnd_str, 16) if hwnd_str.startswith("0x") else int(hwnd_str)
                info = _hwnd_element_info(hwnd)
                if info.get("title") or info.get("class_name"):
                    return info
            except (ValueError, Exception):
                pass

        # Strategy 2: Position-based — get HWND at screen coordinates
        if "," in target and target.replace(",", "").replace("-", "").replace(" ", "").isdigit():
            parts = target.replace(" ", "").split(",")
            x, y = int(parts[0]), int(parts[1])
            hwnd = _hwnd_from_point(x, y)
            if hwnd:
                info = _hwnd_element_info(hwnd)
                if info.get("title") or info.get("class_name"):
                    return info
                # Also check child windows under this point
                children = _hwnd_get_child_windows(hwnd)
                for child in children:
                    cr = child.get("rect")
                    if cr and cr[0] <= x <= cr[2] and cr[1] <= y <= cr[3]:
                        if child.get("title") or child.get("class_name"):
                            return child

        # Strategy 3: Title match — search top-level windows by title/substring
        try:
            found: dict | None = None

            def _enum_proc(hwnd: int, lparam: int) -> bool:
                nonlocal found
                try:
                    buf = ctypes.create_unicode_buffer(512)
                    ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
                    title = (buf.value or "").lower()
                    tgt = target.lower()
                    if title == tgt or tgt in title:
                        found = _hwnd_element_info(hwnd)
                        return False
                except Exception:
                    pass
                return True

            cb_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            callback = cb_type(_enum_proc)
            ctypes.windll.user32.EnumWindows(callback, 0)

            if found is not None:
                return found
        except Exception:
            pass

        # Strategy 4: Class name match — search by window class
        try:
            found2: dict | None = None

            def _enum_class_proc(hwnd: int, lparam: int) -> bool:
                nonlocal found2
                try:
                    cb = ctypes.create_unicode_buffer(256)
                    ctypes.windll.user32.GetClassNameW(hwnd, cb, 256)
                    cls = (cb.value or "").lower()
                    tgt = target.lower()
                    if cls == tgt or tgt in cls:
                        found2 = _hwnd_element_info(hwnd)
                        return False
                except Exception:
                    pass
                return True

            callback2 = cb_type(_enum_class_proc)
            ctypes.windll.user32.EnumWindows(callback2, 0)

            if found2 is not None:
                return found2
        except Exception:
            pass

        return None

    def get_focused_element_info(self) -> dict:
        """Get structured info about the currently focused UI element.

        Returns:
            Dict with name, role, automation_id, pos, size, rect,
            focused, enabled, value, and supported actions.
            Returns error dict if nothing is focused.
        """
        import uiautomation as auto
        try:
            focused = auto.GetFocusedControl()
            if not focused or not focused.Exists(0, 0):
                return {"error": "No focused element found", "focused": False}

            info = {"focused": True}

            try:
                info["name"] = focused.Name or ""
            except Exception:
                pass
            try:
                from .ax_collector_windows import _normalize_role
                ct_name = focused.ControlTypeName or ""
                info["role"] = _normalize_role(ct_name)
            except Exception:
                pass
            try:
                info["automation_id"] = focused.AutomationId
            except Exception:
                pass
            try:
                info["enabled"] = bool(focused.IsEnabled)
            except Exception:
                pass

            try:
                rect = focused.BoundingRectangle
                if rect:
                    info["rect"] = (rect.left, rect.top, rect.right, rect.bottom)
                    info["pos"] = {
                        "x": round((rect.left + rect.right) / 2),
                        "y": round((rect.top + rect.bottom) / 2),
                    }
                    info["size"] = {
                        "w": round(rect.right - rect.left),
                        "h": round(rect.bottom - rect.top),
                    }
            except Exception:
                pass

            try:
                from .ax_collector_windows import _safe_get_value
                val = _safe_get_value(focused)
                if val is not None:
                    info["value"] = val
            except Exception:
                pass

            try:
                from .ax_collector_windows import _safe_get_supported_patterns
                pats = _safe_get_supported_patterns(focused)
                if pats:
                    info["actions"] = pats
            except Exception:
                pass

            return info
        except Exception as e:
            return {"error": str(e), "focused": False}

    def _click_element(self, target: str) -> dict:
        """Click an element by its position or reference using find_element().

        For elements found via HWND fallback (via_hwnd=True), uses _hwnd_click()
        which combines BringWindowToTop + mouse_event + WM_LBUTTONDOWN/UP for
        reliable interaction with custom/non-AX controls.
        """
        self._mark_mutation()
        import uiautomation as auto

        # Use find_element for robust search
        found = self.find_element(target)
        if found is not None:
            pos = found.get("pos")
            hwnd = found.get("hwnd")
            is_hwnd = found.get("via_hwnd", False)

            if pos:
                if is_hwnd and hwnd:
                    # HWND fallback: use Win32 API for reliable click
                    _hwnd_click(hwnd, pos["x"], pos["y"])
                else:
                    auto.Click(pos["x"], pos["y"])
                return {
                    "success": True,
                    "action": "click",
                    "target": target,
                    "method": found.get("method", "find_element"),
                    "found_name": found.get("name", ""),
                    "found_role": found.get("role", ""),
                }
            # Element found but no position — try direct Click()
            ctrl = found.get("_control")
            if ctrl:
                try:
                    ctrl.Click()
                    return {"success": True, "action": "click", "target": target, "method": "direct_click"}
                except Exception:
                    pass

        # Fallback: click at the center of the focused element
        try:
            focused = auto.GetFocusedControl()
            if focused and focused.Exists(0, 0):
                rect = focused.BoundingRectangle
                if rect:
                    x = (rect.left + rect.right) // 2
                    y = (rect.top + rect.bottom) // 2
                    auto.Click(x, y)
                    return {"success": True, "action": "click", "target": target, "method": "focused_center"}
        except Exception:
            pass

        # Last resort: just left-click at current mouse position
        auto.Click(*auto.GetCursorPos())
        return {"success": True, "action": "click", "target": target, "method": "cursor_pos"}

    def _double_click_element(self, target: str) -> dict:
        """Double-click an element using find_element().

        For HWND-via elements, uses _hwnd_click() twice for reliable interaction.
        """
        self._mark_mutation()
        import uiautomation as auto

        found = self.find_element(target)
        if found is not None:
            pos = found.get("pos")
            hwnd = found.get("hwnd")
            is_hwnd = found.get("via_hwnd", False)
            if pos:
                if is_hwnd and hwnd:
                    _hwnd_click(hwnd, pos["x"], pos["y"])
                    time.sleep(0.05)
                    _hwnd_click(hwnd, pos["x"], pos["y"])
                else:
                    auto.Click(pos["x"], pos["y"])
                    auto.Click(pos["x"], pos["y"])
                return {"success": True, "action": "double_click", "target": target,
                        "method": found.get("method", "find_element")}

        # Fallback
        pos = auto.GetCursorPos()
        auto.Click(*pos)
        auto.Click(*pos)
        return {"success": True, "action": "double_click", "target": target, "method": "cursor_pos"}

    def _right_click_element(self, target: str) -> dict:
        """Right-click an element using find_element().

        For HWND-via elements, uses Win32 mouse_event for right-click since
        the element may not respond to uiautomation RightClick().
        """
        self._mark_mutation()
        import uiautomation as auto

        found = self.find_element(target)
        if found is not None:
            pos = found.get("pos")
            hwnd = found.get("hwnd")
            is_hwnd = found.get("via_hwnd", False)
            if pos:
                if is_hwnd and hwnd:
                    # HWND right-click via unified _hwnd_click
                    _hwnd_click(hwnd, pos["x"], pos["y"], button="right")
                else:
                    auto.RightClick(pos["x"], pos["y"])
                return {"success": True, "action": "right_click", "target": target,
                        "method": found.get("method", "find_element")}

        # Fallback
        pos = auto.GetCursorPos()
        auto.RightClick(*pos)
        return {"success": True, "action": "right_click", "target": target, "method": "cursor_pos"}

    def _hover_element(self, target: str) -> dict:
        """Hover over an element to reveal tooltips/previews.

        Moves the mouse cursor over the element without clicking.
        Waits briefly for tooltips to appear.

        Args:
            target: Element name, position ("200,300"), or automation ID ("id:xxx")

        Returns:
            Dict with success status and element info at hover position.
        """
        import uiautomation as auto

        found = self.find_element(target)
        if found is not None:
            pos = found.get("pos")
            if pos:
                auto.MoveTo(pos["x"], pos["y"])
                time.sleep(0.3)  # Brief pause for tooltip to render
                # Get element info at the hovered position
                try:
                    hovered = auto.ControlFromPoint(pos["x"], pos["y"])
                    hover_info = {
                        "name": getattr(hovered, "Name", "") or "",
                        "role": getattr(hovered, "ControlTypeName", "") or "",
                    }
                except Exception:
                    hover_info = {}
                return {
                    "success": True,
                    "action": "hover",
                    "target": target,
                    "hover_pos": pos,
                    "hovered_element": found.get("name", ""),
                    "hover_info": hover_info,
                }
            # Element found but no position — try SetFocus to trigger hover
            ctrl = found.get("_control")
            if ctrl:
                try:
                    ctrl.SetFocus()
                    return {"success": True, "action": "hover", "target": target,
                            "method": "set_focus"}
                except Exception:
                    pass

        # Fallback: hover at current mouse position + small offset
        try:
            cur_x, cur_y = auto.GetCursorPos()
            auto.MoveTo(cur_x + 10, cur_y + 10)
            return {"success": True, "action": "hover", "target": target, "method": "cursor_offset"}
        except Exception as e:
            return {"success": False, "error": str(e), "action": "hover"}

    def _drag_element(self, source: str, target: str) -> dict:
        """Drag from a source element to a target position or element.

        Uses win32 mouse_event API for reliable drag simulation.
        Moves in incremental steps (10 steps) for smooth dragging.

        Args:
            source: Source element name, position ("200,300"), or automation ID
            target: Target position ("400,500") or element name to drop on

        Returns:
            Dict with success status, source/target positions, and path info.
        """
        import uiautomation as auto
        import ctypes

        # Find source position
        source_info = self.find_element(source)
        if not source_info or not source_info.get("pos"):
            return {"success": False, "error": f"Source element not found: {source}",
                    "action": "drag"}

        sx, sy = source_info["pos"]["x"], source_info["pos"]["y"]

        # Find target position
        tx, ty = self._resolve_drop_target(target)
        if tx is None or ty is None:
            return {"success": False, "error": f"Could not determine target: {target}",
                    "action": "drag"}

        # Perform drag
        auto.MoveTo(sx, sy)
        time.sleep(0.2)

        # Mouse down
        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
        time.sleep(0.1)

        # Move in steps for smooth drag
        steps = max(5, min(20, int(abs(tx - sx) / 20 + abs(ty - sy) / 20)))
        for i in range(1, steps + 1):
            mx = sx + (tx - sx) * i // steps
            my = sy + (ty - sy) * i // steps
            ctypes.windll.user32.SetCursorPos(mx, my)
            time.sleep(0.01)

        # Small pause before drop
        time.sleep(0.15)

        # Mouse up
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP

        return {
            "success": True,
            "action": "drag",
            "source": source,
            "source_name": source_info.get("name", ""),
            "target": target,
            "source_pos": {"x": sx, "y": sy},
            "target_pos": {"x": tx, "y": ty},
            "steps": steps,
        }

    def _select_option(self, target: str, value: str) -> dict:
        """Select an option from a dropdown/combobox by value.

        Clicks the target element (combobox/dropdown) to expand it,
        then finds and clicks the option with the matching value.

        Args:
            target: Combobox/dropdown element name or position
            value: The option text/value to select

        Returns:
            Dict with success status and selection info.
        """
        import uiautomation as auto
        import time

        if not value:
            return {"success": False, "error": "No value provided for select",
                    "action": "select"}

        # Click the combobox/dropdown to expand it
        click_result = self._click_element(target)
        if not click_result.get("success"):
            return {"success": False, "error": f"Could not click target '{target}' to open dropdown",
                    "action": "select", "value": value}

        time.sleep(0.3)  # Wait for dropdown to open

        # Try to find the option element by name
        option = self.find_element(value)
        if option and option.get("pos"):
            # Click the option
            pos = option["pos"]
            auto.Click(pos["x"], pos["y"])
            return {
                "success": True,
                "action": "select",
                "target": target,
                "value": value[:80],
                "option_pos": pos,
                "option_name": option.get("name", ""),
                "method": "click_option",
            }

        # Fallback: try selecting via keyboard (Alt+Down arrow, Tab to option, Enter)
        auto.SendKeys("{Alt}{Down}")
        time.sleep(0.2)
        # Try typing the value to auto-complete the selection
        safe = value[:50].replace("{", "{{").replace("}", "}}")
        auto.SendKeys(safe)
        time.sleep(0.2)
        auto.SendKeys("{Tab}")
        return {
            "success": True,
            "action": "select",
            "target": target,
            "value": value[:80],
            "method": "keyboard_fallback",
        }

    def _resize_window(self, size_spec: str) -> dict:
        """Resize the foreground window to specified dimensions.

        Args:
            size_spec: Dimensions in format "width,height" (e.g., "1024,768")

        Returns:
            Dict with success status and new dimensions.
        """
        import ctypes
        import time

        if not size_spec or "," not in size_spec:
            return {"success": False, "error": f"Invalid size format: '{size_spec}'. Use 'width,height' (e.g., '1024,768')",
                    "action": "resize_window"}

        try:
            parts = size_spec.replace(" ", "").split(",")
            width = int(parts[0])
            height = int(parts[1])
        except (ValueError, IndexError):
            return {"success": False, "error": f"Could not parse dimensions: '{size_spec}'",
                    "action": "resize_window"}

        if width < 200 or height < 100:
            return {"success": False, "error": f"Dimensions too small: {width}x{height}. Minimum 200x100.",
                    "action": "resize_window"}
        if width > 7680 or height > 4320:
            return {"success": False, "error": f"Dimensions too large: {width}x{height}. Maximum 7680x4320.",
                    "action": "resize_window"}

        try:
            # Get foreground window handle
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                return {"success": False, "error": "No foreground window found to resize",
                        "action": "resize_window"}

            # Keep current position, set new size
            from ctypes import wintypes
            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            x, y = rect.left, rect.top

            flags = 0x0004  # SWP_NOMOVE = 0x0002, SWP_NOZORDER = 0x0004
            ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, width, height, flags)
            time.sleep(0.2)

            return {
                "success": True,
                "action": "resize_window",
                "width": width,
                "height": height,
                "x": x,
                "y": y,
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to resize window: {e}",
                    "action": "resize_window"}

    def _minimize_window(self) -> dict:
        """Minimize the foreground window."""
        self._mark_mutation()
        import ctypes
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                return {"success": False, "error": "No foreground window found",
                        "action": "minimize_window"}
            ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE = 6
            return {"success": True, "action": "minimize_window"}
        except Exception as e:
            return {"success": False, "error": str(e), "action": "minimize_window"}

    def _maximize_window(self) -> dict:
        """Maximize the foreground window."""
        self._mark_mutation()
        import ctypes
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                return {"success": False, "error": "No foreground window found",
                        "action": "maximize_window"}
            ctypes.windll.user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE = 3
            return {"success": True, "action": "maximize_window"}
        except Exception as e:
            return {"success": False, "error": str(e), "action": "maximize_window"}

    def _close_window(self) -> dict:
        """Close the foreground window gracefully via WM_CLOSE."""
        self._mark_mutation()
        import ctypes
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                return {"success": False, "error": "No foreground window found",
                        "action": "close_window"}
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE = 0x0010
            return {"success": True, "action": "close_window"}
        except Exception as e:
            return {"success": False, "error": str(e), "action": "close_window"}

    def _resolve_drop_target(self, target: str) -> tuple[int | None, int | None]:
        """Resolve a drop target string to x,y coordinates.

        Accepts element name (looked up via find_element) or "x,y" position string.

        Returns (x, y) or (None, None) if unresolvable.
        """
        # Try find_element first (element name or id:xxx)
        target_info = self.find_element(target)
        if target_info and target_info.get("pos"):
            pos = target_info["pos"]
            return pos["x"], pos["y"]

        # Try parsing as raw "x,y" position
        if "," in target:
            parts = target.replace(" ", "").split(",")
            try:
                return int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                pass

        return None, None

    def _type_text(self, text: str, target: str = "") -> dict:
        """Type text, optionally clicking a target element first.

        If target is provided, clicks the target before typing.
        This ensures text goes into the right field.
        """
        import uiautomation as auto

        # If target specified, click it first to focus
        if target:
            click_result = self._click_element(target)
            if not click_result.get("success"):
                return {"success": False, "error": f"Could not click target '{target}' before typing",
                        "action": "type"}
            time.sleep(0.2)  # Small delay for focus to settle

        # Escape special characters for SendKeys
        safe = text.replace("{", "{{").replace("}", "}}")
        for known in ("Ctrl", "Alt", "Shift", "Tab", "Enter", "Esc", "Backspace",
                       "Delete", "Up", "Down", "Left", "Right", "Home", "End",
                       "PageUp", "PageDown", "F1", "F2", "F3", "F4", "F5",
                       "F6", "F7", "F8", "F9", "F10", "F11", "F12"):
            safe = safe.replace("{{" + known + "}}", "{" + known + "}")
        auto.SendKeys(safe)

        result = {"success": True, "action": "type", "value": text[:80]}
        if target:
            result["target"] = target
        return result

    def _clear_text(self, target: str = "") -> dict:
        """Clear a text field (click to focus, select all, delete).

        If target is provided, clicks the element first.
        """
        import uiautomation as auto

        if target:
            click_result = self._click_element(target)
            if not click_result.get("success"):
                return {"success": False, "error": f"Could not click target '{target}' to clear",
                        "action": "clear_text"}
            time.sleep(0.2)

        # Select all (Ctrl+A) then Delete
        auto.SendKeys("{Ctrl}a")
        time.sleep(0.1)
        auto.SendKeys("{Delete}")

        result = {"success": True, "action": "clear_text"}
        if target:
            result["target"] = target
        return result

    def _press_key(self, key_combo: str) -> dict:
        """Press a key or keyboard shortcut using SendKeys.

        Examples:
            "enter"         → {Enter}
            "tab"           → {Tab}
            "escape"        → {Esc}
            "ctrl+c"        → {Ctrl}c
            "ctrl+shift+s"  → {Ctrl}{Shift}s
            "alt+f4"        → {Alt}{F4}
            "win+d"         → {Win}d
        """
        import uiautomation as auto

        # Map common names to SendKeys format
        key_map = {
            "enter": "{Enter}", "return": "{Enter}",
            "tab": "{Tab}",
            "escape": "{Esc}", "esc": "{Esc}",
            "delete": "{Delete}", "backspace": "{Backspace}",
            "space": " ",
            "up": "{Up}", "down": "{Down}", "left": "{Left}", "right": "{Right}",
            "home": "{Home}", "end": "{End}",
            "pageup": "{PgUp}", "pagedown": "{PgDn}",
            "f1": "{F1}", "f2": "{F2}", "f3": "{F3}", "f4": "{F4}",
            "f5": "{F5}", "f6": "{F6}", "f7": "{F7}", "f8": "{F8}",
            "f9": "{F9}", "f10": "{F10}", "f11": "{F11}", "f12": "{F12}",
        }

        combo = key_combo.lower().replace(",", "+").replace(" ", "")

        # If it's a simple key with known mapping
        if combo in key_map:
            auto.SendKeys(key_map[combo])
            return {"success": True, "action": "key_press", "target": key_combo}

        # Parse modifier+key combos
        # Modifier map: short → SendKeys format
        modifier_map = {
            "ctrl": "{Ctrl}", "control": "{Ctrl}",
            "alt": "{Alt}",
            "shift": "{Shift}",
            "win": "{Win}", "windows": "{Win}", "cmd": "{Win}",
        }

        parts = combo.split("+")
        modifiers = []
        key = ""

        for p in parts:
            if p in modifier_map:
                modifiers.append(modifier_map[p])
            elif p in key_map:
                key = key_map[p].strip("{}")
            else:
                # Use as-is for single characters
                key = p.upper() if len(p) == 1 else p.capitalize()

        if modifiers:
            modifier_str = "".join(modifiers)
            auto.SendKeys(f"{modifier_str}{key}")
            return {"success": True, "action": "key_press", "target": key_combo, "method": "modifiers"}

        # Fallback: try the key directly
        try:
            auto.SendKeys(key_map.get(combo, combo))
        except Exception:
            auto.SendKeys("{" + combo.capitalize() + "}")
        return {"success": True, "action": "key_press", "target": key_combo, "method": "direct"}

    def _scroll(self, direction: str, amount: str) -> dict:
        """Scroll in a direction using mouse wheel or keyboard simulation."""
        self._mark_mutation()
        import uiautomation as auto
        amt = max(1, min(10, int(amount or "3")))

        direction = direction.lower()
        if direction == "up":
            for _ in range(amt):
                auto.WheelUp(1)
            return {"success": True, "action": "scroll", "target": "up", "value": str(amt)}
        elif direction == "down":
            for _ in range(amt):
                auto.WheelDown(1)
            return {"success": True, "action": "scroll", "target": "down", "value": str(amt)}
        elif direction == "left":
            for _ in range(amt):
                auto.SendKeys("{Left}")
            return {"success": True, "action": "scroll", "target": "left", "value": str(amt), "note": "arrow keys"}
        elif direction == "right":
            for _ in range(amt):
                auto.SendKeys("{Right}")
            return {"success": True, "action": "scroll", "target": "right", "value": str(amt), "note": "arrow keys"}

    def _open_app(self, app_name_or_path: str) -> dict:
        """Open an application by name or path."""
        self._mark_mutation()
        try:
            subprocess.Popen(
                ["start", "", app_name_or_path],
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            try:
                subprocess.Popen(
                    app_name_or_path,
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                return {"success": False, "error": f"Failed to open app: {e}"}
        time.sleep(1.5)
        return {"success": True, "action": "open_app", "target": app_name_or_path}

    def _open_url(self, url: str) -> dict:
        """Open a URL in the default browser."""
        self._mark_mutation()
        webbrowser.open(url)
        time.sleep(1.0)
        return {"success": True, "action": "open_url", "value": url}

    def _take_screenshot(self) -> dict:
        """Capture the current screen as a base64-encoded PNG image.

        Uses Pillow (PIL) ImageGrab to capture the full screen.
        Returns the image as a base64 data URI string for embedding.

        Returns:
            Dict with success, screenshot (base64 data URI), width, height,
            and format info.
        """
        import io
        import base64
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            b64 = base64.b64encode(buf.read()).decode("utf-8")
            w, h = img.size
            return {
                "success": True,
                "action": "take_screenshot",
                "screenshot": f"data:image/png;base64,{b64}",
                "width": w,
                "height": h,
                "format": "png",
                "note": "Base64-encoded PNG screenshot. Decode or embed as data URI.",
            }
        except ImportError:
            return {"success": False, "action": "take_screenshot",
                    "error": "Pillow (PIL) not installed. Install with: pip install Pillow"}
        except Exception as e:
            return {"success": False, "action": "take_screenshot",
                    "error": f"Screenshot failed: {e}"}

    def _take_region_screenshot(self, target: str, value: str) -> dict:
        """Capture a screenshot of a specific screen region.

        Accepts a region specifier:
          - "x,y,width,height" — Direct pixel region (e.g., "100,200,400,300")
          - Element name — Captures the bounding rect of a named element
          - "id:xxx" — Captures the bounding rect of an automation ID target

        Uses Pillow (PIL) ImageGrab for capture and crops to the region.

        Returns:
            Dict with success, screenshot (base64 data URI), region info,
            width, height, and format.
        """
        import io
        import base64
        try:
            from PIL import ImageGrab

            # Determine region (x, y, width, height)
            region = None
            source_desc = ""

            # Check if target is a "x,y,w,h" spec
            if target:
                parts = target.replace(" ", "").split(",")
                if len(parts) == 4 and all(p.replace("-", "").isdigit() for p in parts):
                    rx, ry, rw, rh = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
                    if rw > 0 and rh > 0:
                        region = (rx, ry, rx + rw, ry + rh)
                        source_desc = f"{rw}x{rh} at ({rx},{ry})"

                # If value has coords, try that too
                if region is None and value:
                    vparts = value.replace(" ", "").split(",")
                    if len(vparts) == 4 and all(p.replace("-", "").isdigit() for p in vparts):
                        rx, ry, rw, rh = int(vparts[0]), int(vparts[1]), int(vparts[2]), int(vparts[3])
                        if rw > 0 and rh > 0:
                            region = (rx, ry, rx + rw, ry + rh)
                            source_desc = f"{rw}x{rh} at ({rx},{ry})"

            # Try resolving as an element name to get its bounding rect
            if region is None and target:
                found = self.find_element(target)
                if found and found.get("rect"):
                    rect = found["rect"]
                    # rect is (left, top, right, bottom)
                    region = (rect[0], rect[1], rect[2], rect[3])
                    w = rect[2] - rect[0]
                    h = rect[3] - rect[1]
                    name = found.get("name", target)
                    source_desc = f"element '{name}' ({w}x{h})"

            if region is None:
                # Fall back to full screen
                img = ImageGrab.grab()
                source_desc = "full screen (no region specified)"
            else:
                img = ImageGrab.grab(bbox=region)

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            b64 = base64.b64encode(buf.read()).decode("utf-8")
            w, h = img.size

            return {
                "success": True,
                "action": "take_region_screenshot",
                "target": target or "",
                "screenshot": f"data:image/png;base64,{b64}",
                "region": source_desc,
                "width": w,
                "height": h,
                "format": "png",
            }
        except ImportError:
            return {"success": False, "action": "take_region_screenshot",
                    "error": "Pillow (PIL) not installed. Install with: pip install Pillow"}
        except Exception as e:
            return {"success": False, "action": "take_region_screenshot",
                    "error": f"Region screenshot failed: {e}"}

    def _trigger_shortcut(self, shortcut_name: str) -> dict:
        """Trigger a named keyboard shortcut.

        Maps human-readable shortcut names to platform-specific key combos.
        Supports:
          - Standard shortcuts: copy, paste, cut, select_all, save, find, undo, redo
          - Window management: close_tab, new_tab, switch_tab, refresh
          - System: lock_screen, task_switcher, screenshot, open_run
          - Or any raw key combo string (passed through to key_press)

        Args:
            shortcut_name: Semantic shortcut name or raw key combo

        Returns:
            Dict with success status and the resolved key combo.
        """
        # Shortcut map: name → key combo (SendKeys format)
        # Uses Ctrl on all platforms (Windows)
        shortcut_map = {
            # Text editing
            "copy": "{Ctrl}c",
            "paste": "{Ctrl}v",
            "cut": "{Ctrl}x",
            "select_all": "{Ctrl}a",
            "undo": "{Ctrl}z",
            "redo": "{Ctrl}y",
            "save": "{Ctrl}s",
            "find": "{Ctrl}f",
            "replace": "{Ctrl}h",
            "find_next": "{F3}",
            "new_document": "{Ctrl}n",
            "open_file": "{Ctrl}o",
            "print": "{Ctrl}p",
            "bold": "{Ctrl}b",
            "italic": "{Ctrl}i",
            "underline": "{Ctrl}u",

            # Navigation
            "close_tab": "{Ctrl}{F4}",
            "close_window": "{Alt}{F4}",
            "new_tab": "{Ctrl}t",
            "switch_next_tab": "{Ctrl}{Tab}",
            "switch_prev_tab": "{Ctrl}{Shift}{Tab}",
            "refresh": "{F5}",
            "hard_refresh": "{Ctrl}{F5}",
            "go_back": "{Alt}{Left}",
            "go_forward": "{Alt}{Right}",

            # System
            "lock_screen": "{Win}l",
            "task_switcher": "{Alt}{Tab}",
            "show_desktop": "{Win}d",
            "screenshot": "{Win}{Shift}s",
            "open_run": "{Win}r",
            "open_settings": "{Win}i",
            "open_search": "{Win}s",
            "open_file_explorer": "{Win}e",
            "minimize_all": "{Win}m",

            # Dev shortcuts
            "comment": "{Ctrl}{K}{Ctrl}{C}",
            "uncomment": "{Ctrl}{K}{Ctrl}{U}",
            "format_code": "{Shift}{Alt}f",
            "build": "{Ctrl}{Shift}b",
            "run": "{F5}",
            "debug": "{F5}",  # Will be intercepted differently by IDEs
            "step_over": "{F10}",
            "step_into": "{F11}",
            "toggle_breakpoint": "{F9}",
        }

        key = shortcut_name.lower().replace(" ", "_")

        if key in shortcut_map:
            combo = shortcut_map[key]
            import uiautomation as auto
            auto.SendKeys(combo)
            return {
                "success": True,
                "action": "shortcut",
                "target": shortcut_name,
                "key_combo": combo,
                "resolved_to": f"'{shortcut_name}' → {combo}",
            }

        # Fall through to key_press handler for raw combos
        return self._press_key(shortcut_name)


class StubActionProvider(ActionProvider):
    """Action provider that logs actions instead of executing them.
    Useful for testing or non-Windows platforms.
    """

    def __init__(self):
        self._shot_mgr = AppshotManager()
        self._state = {"role": "Desktop", "title": "Test", "children": []}

    def observe(self) -> Observation:
        appshot = self._shot_mgr.take_snapshot(self._state)
        return Observation(appstate=self._state, appshot=appshot)

    def act(self, action: dict) -> dict:
        logger.info(f"[STUB] execute: {json.dumps(action)}")
        action_type = action.get("action")
        result = {"success": True, "action": action_type, "stub": True}

        # Return simulated data for info/get queries
        if action_type == "get_focused_info":
            return {"success": True, "action": "get_focused_info",
                    "name": "Stub Focused Element", "role": "AXButton",
                    "focused": True, "enabled": True}
        if action_type == "clear_text":
            target = action.get("target", "")
            result["target"] = target

        if action_type == "hover":
            target = action.get("target", "")
            # Simulate hover: return info about element at target position
            found = self.find_element(target)
            if found:
                info = {k: v for k, v in found.items() if not k.startswith("_")}
                return {"success": True, "action": "hover", "target": target,
                        "hovered_element": info.get("name", ""), "info": info}
            return {"success": False, "action": "hover", "target": target,
                    "error": "Element not found"}

        if action_type == "drag":
            source = action.get("target", "")
            target = action.get("value", "")
            source_info = self.find_element(source)
            if not source_info:
                return {"success": False, "action": "drag", "source": source,
                        "target": target, "error": f"Source not found: {source}"}
            target_info = self.find_element(target)
            if not target_info:
                # Try parsing as position
                if "," in target:
                    parts = target.replace(" ", "").split(",")
                    try:
                        tx, ty = int(parts[0]), int(parts[1])
                        target_info = {"pos": {"x": tx, "y": ty}}
                    except (ValueError, IndexError):
                        pass
                if not target_info:
                    return {"success": False, "action": "drag", "source": source,
                            "target": target, "error": f"Target not found or invalid: {target}"}
            return {
                "success": True, "action": "drag",
                "source": source,
                "source_pos": source_info.get("pos", {}),
                "source_name": source_info.get("name", ""),
                "target": target,
                "target_pos": target_info.get("pos", {}),
                "steps": 10,
            }

        if action_type == "select":
            target = action.get("target", "")
            value = action.get("value", "")
            if not value:
                return {"success": False, "action": "select", "target": target,
                        "error": "No value provided for selection"}
            # Simulate: click target, find option by value, return selection info
            source_info = self.find_element(target)
            if not source_info:
                return {"success": False, "action": "select", "target": target,
                        "value": value, "error": f"Combobox not found: {target}"}
            option_info = self.find_element(value)
            option_pos = option_info.get("pos", {}) if option_info else None
            return {
                "success": True, "action": "select",
                "target": target,
                "value": value,
                "option_pos": option_pos,
                "option_name": option_info.get("name", "") if option_info else "",
                "method": "stub",
            }

        if action_type == "resize_window":
            size_spec = action.get("value", "") or action.get("target", "")
            if not size_spec or "," not in size_spec:
                return {"success": False, "action": "resize_window",
                        "error": f"Invalid size: '{size_spec}'. Use 'width,height'"}
            try:
                parts = size_spec.replace(" ", "").split(",")
                w, h = int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                return {"success": False, "action": "resize_window",
                        "error": f"Could not parse: '{size_spec}'"}
            return {
                "success": True, "action": "resize_window",
                "width": w, "height": h,
            }

        if action_type == "find_element":
            target = action.get("target", "")
            found = self.find_element(target)
            if found:
                info = {k: v for k, v in found.items() if not k.startswith("_")}
                return {"success": True, "action": "find_element", "target": target, "info": info}
            return {"success": False, "action": "find_element", "target": target,
                    "error": f"Element not found: {target}"}

        if action_type == "type_into":
            # Stub: simulate clicking target then typing
            target = action.get("target", "")
            value = action.get("value", "")
            if target:
                click = self.find_element(target)
                if not click:
                    return {"success": False, "action": "type_into", "target": target,
                            "error": f"Element not found: {target}"}
            result["target"] = target
            result["value"] = value[:80]

        if action_type == "minimize_window":
            return {"success": True, "action": "minimize_window"}

        if action_type == "maximize_window":
            return {"success": True, "action": "maximize_window"}

        if action_type == "close_window":
            return {"success": True, "action": "close_window"}

        if action_type == "take_screenshot":
            # Stub: return a simulated screenshot result
            return {
                "success": True,
                "action": "take_screenshot",
                "screenshot": "data:image/png;base64,stub_screenshot_data",
                "width": 1920,
                "height": 1080,
                "format": "png",
            }

        if action_type == "take_region_screenshot":
            target = action.get("target", "")
            value = action.get("value", "")
            return {
                "success": True,
                "action": "take_region_screenshot",
                "target": target,
                "screenshot": "data:image/png;base64,stub_region_screenshot_data",
                "region": f"stub region: {target or 'full screen'}",
                "width": 800,
                "height": 600,
                "format": "png",
            }

        if action_type == "shortcut":
            target = action.get("target", "")
            return {
                "success": True,
                "action": "shortcut",
                "target": target,
                "key_combo": f"stub:{target}",
                "resolved_to": f"'{target}' (stub)",
            }

        return result

    def find_element(self, target: str) -> dict | None:
        """Stub: simulate finding an element for testing."""
        if not target:
            return None
        # Simulate position-based lookup
        if "," in target and target.replace(",", "").replace("-", "").replace(" ", "").isdigit():
            parts = target.replace(" ", "").split(",")
            return {
                "method": "position",
                "name": f"Element at {target}",
                "role": "AXButton",
                "pos": {"x": int(parts[0]), "y": int(parts[1])},
                "size": {"w": 100, "h": 30},
                "focused": True,
                "enabled": True,
            }
        # Simulate name-based lookup
        return {
            "method": "name",
            "name": target,
            "role": "AXButton",
            "pos": {"x": 500, "y": 400},
            "size": {"w": 100, "h": 30},
            "focused": False,
            "enabled": True,
        }

    def get_focused_element_info(self) -> dict:
        """Stub: simulate focused element info for testing."""
        return {
            "focused": True,
            "name": "Stub Focused Element",
            "role": "AXButton",
            "enabled": True,
            "pos": {"x": 500, "y": 400},
            "size": {"w": 100, "h": 30},
        }


# ── MCP Tool: get_element_info ──

@mcp.tool()
def get_element_info(
    target: str,
    include_tree: bool = False,
) -> str:
    """Find a UI element by name, automation ID, or position and return its info.

    Args:
        target: Element name ("Submit"), automation ID ("id:myButton"),
                or position ("200,300")
        include_tree: If True, includes the element's children as a mini accessibility tree

    Returns:
        JSON with element info: name, role, position, size, focused, enabled, value
        Returns error if element not found.
    """
    provider = _get_provider()
    if not hasattr(provider, "find_element"):
        return json.dumps({"error": "find_element not available on this provider",
                           "provider": type(provider).__name__})

    try:
        info = provider.find_element(target)
        if info is None:
            return json.dumps({"error": f"Element not found: {target}",
                               "target": target})

        # Remove internal control reference from output
        result = {k: v for k, v in info.items() if not k.startswith("_")}

        # Optionally include child tree
        if include_tree and "_control" in info:
            try:
                from .ax_collector_windows import _safe_get_children, _normalize_role
                ctrl = info["_control"]
                children = _safe_get_children(ctrl)
                if children:
                    child_info = []
                    for child in children[:20]:
                        try:
                            ci = {
                                "name": child.Name or "",
                                "role": _normalize_role(child.ControlTypeName or ""),
                            }
                            try:
                                ci["automation_id"] = child.AutomationId
                            except Exception:
                                pass
                            try:
                                rect = child.BoundingRectangle
                                if rect:
                                    ci["pos"] = {"x": round((rect.left+rect.right)/2),
                                                   "y": round((rect.top+rect.bottom)/2)}
                            except Exception:
                                pass
                            child_info.append(ci)
                        except Exception:
                            pass
                    if child_info:
                        result["children"] = child_info
            except Exception:
                pass

        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": f"Error finding element: {e}"})


# ── MCP Tool: take_screenshot ──

@mcp.tool()
def take_region_screenshot(
    target: str = "",
    value: str = "",
) -> str:
    """Capture a screenshot of a specific screen region.

    Accepts a region specifier:
      - "x,y,width,height" — Direct pixel region (e.g., "100,200,400,300")
      - Element name — Captures the bounding rect of a named element
      - "id:xxx" — Captures the bounding rect of an automation ID target

    Falls back to full screen if no region is specified.

    Args:
        target: Region as 'x,y,width,height' or element name
        value: Optional fallback region 'x,y,width,height'

    Returns:
        JSON with base64-encoded PNG screenshot, region info, width, height.
    """
    provider = _get_provider()
    try:
        result = provider.act({"action": "take_region_screenshot", "target": target, "value": value})
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def trigger_shortcut(
    shortcut_name: str,
) -> str:
    """Trigger a named keyboard shortcut.

    Supports:
      - Text editing: copy, paste, cut, select_all, save, find, undo, redo
      - Navigation: new_tab, close_tab, switch_next_tab, refresh
      - System: lock_screen, task_switcher, show_desktop, screenshot, open_run
      - Dev: comment, uncomment, format_code, build, run, debug
      - Or any raw key combo string (passes through to key_press)

    Args:
        shortcut_name: Semantic shortcut name or raw key combo

    Returns:
        JSON with success status and the resolved key combo.
    """
    provider = _get_provider()
    try:
        result = provider.act({"action": "shortcut", "target": shortcut_name})
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def take_screenshot() -> str:
    """Capture the full screen as a base64-encoded PNG image.

    Uses Pillow (PIL) ImageGrab to capture the entire desktop.
    Returns a JSON object with the screenshot as a data URI,
    plus dimensions and format info.

    Use this to visually verify the desktop state when the
    accessibility tree is insufficient.

    Returns:
        JSON with:
          - screenshot: 'data:image/png;base64,...' (can be embedded in HTML)
          - width, height: Screen dimensions in pixels
          - format: Always 'png'
    """
    provider = _get_provider()
    try:
        result = provider.act({"action": "take_screenshot"})
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── MCP Tool: get_appstate ──

@mcp.tool()
def get_appstate(
    filter_interactive_only: bool = True,
    include_window_info: bool = True,
) -> str:
    """Get the current application state as a structured accessibility tree.

    Returns a JSON object describing every interactive element on screen:
    buttons, text fields, menus, checkboxes, etc. with their roles,
    labels, values, enabled/focused states, and screen positions.

    Args:
        filter_interactive_only: If True, returns only interactive elements.
        include_window_info: If True, includes window metadata (title, app name).
    """
    provider = _get_provider()
    try:
        obs = provider.observe()
        appstate = obs.appstate
        state.current_appstate = appstate

        # Apply filters if requested
        if filter_interactive_only:
            appstate = _filter_interactive_only(appstate)
        if not include_window_info:
            appstate = _strip_window_metadata(appstate)

        return json.dumps(appstate, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── MCP Tool: get_appshot ──

@mcp.tool()
def get_appshot() -> str:
    """Take a state snapshot showing what changed since the last observation.

    Returns a short summary plus a diff showing:
    - What elements appeared (new dialogs, menus, fields)
    - What elements disappeared (dismissed UI)
    - Focus changes (what's now selected/focused)
    - Value changes (text entered, sliders moved)

    Use this AFTER each action to verify what happened.
    """
    provider = _get_provider()
    try:
        obs = provider.observe()
        state.prev_appstate = state.current_appstate
        state.current_appstate = obs.appstate
        state.appshot_history.append(obs.appshot)
        return json.dumps(obs.appshot, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e), "appshot": {"id": 0, "summary": "Error: could not observe state", "type": "error"}})


# ── Internal: execute action with security check ──

def _exec_action_inner(provider: ActionProvider, action: dict) -> dict:
    """Execute a single action with optional optimization."""
    _ensure_warmup()
    # Observation cache: skip observe if nothing changed
    sec = _get_security()
    if sec is not None:
        # Check security before executing
        allowed, reason, level = sec.check_action(action)
        if not allowed:
            return {"success": False, "error": reason, "blocked": True}
        if level.value >= DangerLevel.DANGEROUS.value:
            return {"success": False, "error": f"Blocked (danger level {level.name}): {reason if reason else 'dangerous action'}",
                    "blocked": True, "danger_level": level.name}
        # CAUTION level: still execute but log it
        if level.value >= DangerLevel.CAUTION.value:
            logger.warning(f"Caution: {reason}")

    result = provider.act(action)

    # Track execution in security context
    if sec is not None:            sec.record_executed(action)

    return result


# ── Global warmup state ──

_warmup_done: bool = False


def _ensure_warmup():
    """Warm the provider's observation cache on first call.

    Called lazily before the first tool invocation. Performs one
    initial observation so that subsequent get_appstate / get_appshot
    calls hit the fingerprint cache instead of doing a cold walk.

    Only runs once per process lifetime.
    """
    global _warmup_done
    if _warmup_done:
        return
    _warmup_done = True
    try:
        provider = _get_provider()
        if hasattr(provider, "warm_cache"):
            provider.warm_cache()
            logger.info("Cache warmup complete")
    except Exception as e:
        logger.debug(f"Cache warmup skipped: {e}")


@mcp.tool()
def execute_action(
    action_type: str,
    target: str = "",
    value: str = "",
    modifiers: list[str] | None = None,
) -> str:
    """Execute a UI action on the desktop.

    Supported action types:
      - click:           Click an element by name or position ("200,300")
      - double_click:    Double-click an element
      - right_click:     Right-click (context menu) an element
      - hover:           Hover over an element to reveal tooltips/previews
      - drag:            Drag a source element to a target position or element (drag-and-drop)
      - select:          Select an option from a dropdown/combobox by value
      - resize_window:   Resize the foreground window to dimensions (e.g., '1024,768')
      - minimize_window: Minimize the foreground window
      - maximize_window: Maximize the foreground window
      - close_window:    Close the foreground window gracefully
      - take_screenshot:        Capture the full screen as a base64-encoded PNG image
      - take_region_screenshot:  Capture a specific screen region as base64 PNG (by coords or element)
      - shortcut:                Trigger a named keyboard shortcut (copy, paste, save, new_tab, etc.)
      - type:            Type text into the focused element
      - type_into:       Click an element first, then type text into it
      - find_element:    Find a UI element by name, position, or automation ID
      - key_press:       Press a key ('enter', 'tab', 'escape') or combo ('ctrl+c', 'alt+f4')
      - scroll:          Scroll in a direction ('up', 'down', 'left', 'right')
      - open_app:        Open an application by name ('notepad', 'chrome')
      - open_url:        Open a URL in the default browser ('https://example.com')
      - wait:            Wait N milliseconds ('500', '1000')

    Args:
        action_type: One of the supported action types above.
        target: Element name or position ("200,300"). For key_press, the key/combo.
                For scroll, the direction.
        value: Text to type, URL to open, or ms to wait.
        modifiers: Key modifiers (unused in current Windows backend).
    """
    provider = _get_provider()
    action = {"action": action_type, "target": target, "value": value}

    # Timing
    timer = _optimization_timer
    if timer:
        timer.start("execute_action")

    result = _exec_action_inner(provider, action)
    state.action_history.append({**action, **result})

    if timer:
        timer.stop("execute_action")

    return json.dumps(result, indent=2, default=str)


# ── MCP Tool: list_actions ──

@mcp.tool()
def list_actions() -> str:
    """List all supported action types with descriptions and parameter schemas.

    Returns a JSON array of action descriptors.
    """
    actions = [
        {
            "name": "click",
            "description": "Click an interactive element (button, link, checkbox, etc.)",
            "params": {"target": "Element name or position like '200,300'"},
        },
        {
            "name": "double_click",
            "description": "Double-click an element",
            "params": {"target": "Element name or position"},
        },
        {
            "name": "right_click",
            "description": "Right-click to show context menu",
            "params": {"target": "Element name or position"},
        },
        {
            "name": "hover",
            "description": "Hover over an element to reveal tooltips, previews, or hover effects",
            "params": {"target": "Element name, 'id:xxx', or 'x,y' position"},
        },
        {
            "name": "drag",
            "description": "Drag a source element to a target position or element (drag-and-drop)",
            "params": {"target": "Source element name or position", "value": "Target position '400,500' or element name to drop on"},
        },
        {
            "name": "select",
            "description": "Select an option from a dropdown/combobox by value",
            "params": {"target": "Combobox/dropdown element name or position", "value": "Option text/value to select"},
        },
        {
            "name": "resize_window",
            "description": "Resize the foreground window to specified dimensions",
            "params": {"value": "Dimensions in 'width,height' format (e.g. '1024,768')"},
        },
        {
            "name": "minimize_window",
            "description": "Minimize the foreground window to taskbar",
            "params": {},
        },
        {
            "name": "maximize_window",
            "description": "Maximize the foreground window to fill screen",
            "params": {},
        },
        {
            "name": "close_window",
            "description": "Close the foreground window gracefully",
            "params": {},
        },
        {
            "name": "take_screenshot",
            "description": "Capture the full screen as a base64-encoded PNG image using Pillow",
            "params": {},
        },
        {
            "name": "take_region_screenshot",
            "description": "Capture a screenshot of a specific screen region by coordinates or element",
            "params": {"target": "'x,y,width,height' region or element name", "value": "Optional fallback region 'x,y,w,h'"},
        },
        {
            "name": "shortcut",
            "description": "Trigger a named keyboard shortcut (copy, paste, save, new_tab, etc.)",
            "params": {"target": "Shortcut name: copy/paste/cut/select_all/save/undo/redo/new_tab/close_tab/refresh/lock_screen/task_switcher/show_desktop/open_run, or any key combo"},
        },
        {
            "name": "type",
            "description": "Type text into the currently focused element",
            "params": {"value": "Text to type"},
        },
        {
            "name": "type_into",
            "description": "Click an element first, then type text into it (target-first typing)",
            "params": {"target": "Element name or position", "value": "Text to type"},
        },
        {
            "name": "find_element",
            "description": "Find a UI element by name, automation ID, or position and return its info (name, role, position, size, state)",
            "params": {"target": "Element name, 'id:xxx', or 'x,y' position"},
        },
        {
            "name": "key_press",
            "description": "Press a key or keyboard shortcut",
            "params": {"target": "Key: enter/tab/escape. Combo: ctrl+c, alt+f4, ctrl+shift+s"},
        },
        {
            "name": "scroll",
            "description": "Scroll in a direction",
            "params": {"target": "up/down/left/right", "value": "Number of scroll steps (1-10)"},
        },
        {
            "name": "open_app",
            "description": "Open an application by name or path",
            "params": {"target": "App name: notepad, chrome, explorer, etc."},
        },
        {
            "name": "open_url",
            "description": "Open a URL in the default browser",
            "params": {"value": "Full URL including https://"},
        },
        {
            "name": "wait",
            "description": "Wait before the next action (state settling)",
            "params": {"value": "Milliseconds to wait (500-2000)"},
        },
    ]
    return json.dumps(actions, indent=2)


# ── Internal: merge and compress helpers ──

def _merge_actions(actions: list[dict]) -> list[dict]:
    """Merge compatible consecutive actions using the action merger."""
    if _optimization_merger is not None and _optimization_config is not None:
        return _optimization_merger.merge(actions)
    return actions


def _compress_history(actions: list[dict], appshots: list[dict]) -> tuple[list[dict], list[dict]]:
    """Compress action and appshot history using token optimizer."""
    if _optimization_tokenizer is not None:
        actions = _optimization_tokenizer.compress_action_history(actions)
        appshots = _optimization_tokenizer.compress_appshot_history(appshots)
    return actions, appshots


@mcp.tool()
def run_task(
    task: str,
    max_steps: int = 50,
    model: str = "deepseek-chat",
    temperature: float = 0.1,
) -> str:
    """Run the full autonomous agent loop for a given task.

    The agent will:
    1. Observe the current desktop state
    2. Decide the next action using DeepSeek v4
    3. Execute the action
    4. Verify the result
    5. Repeat until done or max_steps reached

    Args:
        task: The full task description (e.g., "Open Notepad and type 'Hello World'")
        max_steps: Maximum number of steps before stopping (default 50)
        model: LLM model to use (default: deepseek-chat)
        temperature: LLM temperature for action selection (default: 0.1, lower = more deterministic)

    Returns:
        JSON with: success, steps_completed, total_duration, summary, error, action_history
    """
    provider = _get_provider()
    prompt_config = PromptConfig(max_total_tokens=64000)
    prompt_builder = PromptBuilder(prompt_config)
    loop_config = LoopConfig(max_steps=max_steps, model=model, temperature=temperature)

    loop = AgentLoop(
        action_provider=provider,
        prompt_builder=prompt_builder,
        config=loop_config,
    )
    state.agent_loop = loop

    # Timeline tracking
    timer = _optimization_timer
    if timer:
        timer.start("run_task_total")

    try:
        result = loop.run(task)

        # Merge actions in result if optimization enabled
        if "action_history" in result and isinstance(result["action_history"], list):
            result["action_history"] = _merge_actions(result["action_history"])
            # Also compress
            if _optimization_tokenizer:
                result["action_history"] = _optimization_tokenizer.compress_action_history(
                    result["action_history"]
                )

        if timer:
            timer.stop("run_task_total")
            logger.info(f"\n{timer.report()}")

        return json.dumps({
            "success": result.get("success", False),
            "steps_completed": result.get("total_steps", 0),
            "summary": result.get("summary", result.get("error", "")),
            "error": result.get("error"),
            "total_duration": result.get("duration", 0),
            "action_count": result.get("total_action_history", 0),
        }, indent=2)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ── MCP Tool: observe_and_act ──

@mcp.tool()
def observe_and_act(
    task: str = "",
    previous_actions: str = "[]",
) -> str:
    """Perform a single Observe→Think→Act step.

    This is useful for step-by-step control where you want to:
    1. See what the agent observes
    2. Review or override the action choice before it executes
    3. Proceed step by step

    Args:
        task: The current task description
        previous_actions: JSON array of previous action dicts (for context)

    Returns:
        JSON with: observation, decided_action, execution_result
    """
    provider = _get_provider()

    # Optimize observation (cache if enabled)
    obs_provider = provider
    cache = _optimization_cache
    if cache is not None:
        def _observe():
            return provider.observe()
        # Use the current appstate as context for cache fingerprinting
        context = state.current_appstate or {}
        obs = cache.get_or_observe(_observe, context)
    else:
        obs = provider.observe()

    state.current_appstate = obs.appstate
    state.appshot_history.append(obs.appshot)

    # Parse previous actions
    try:
        past = json.loads(previous_actions) if isinstance(previous_actions, str) else previous_actions
    except (json.JSONDecodeError, TypeError):
        past = []

    # Compress history
    all_actions = past + state.action_history[-10:]
    all_appshots = state.appshot_history[-8:]
    if _optimization_tokenizer:
        all_actions = _optimization_tokenizer.compress_action_history(all_actions)
        all_appshots = _optimization_tokenizer.compress_appshot_history(all_appshots)

    # Build prompt
    prompt_builder = PromptBuilder()
    messages = prompt_builder.build_messages(
        appstate=obs.appstate,
        appshots=all_appshots,
        action_history=all_actions,
        task=task or "Explore the current desktop state",
    )

    # Call LLM (if available) or return action prompt for the client to decide
    if os.environ.get("DEEPSEEK_API_KEY"):
        import openai
        client = openai.OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        )
        try:
            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                temperature=0.1,
                max_tokens=512,
            )
            llm_text = response.choices[0].message.content or ""
            # Parse action from response
            action = loop_parse_action(llm_text)
            if action:
                result = _exec_action_inner(provider, action)
                state.action_history.append({**action, **result})
                return json.dumps({
                    "observation": obs.appshot,
                    "llm_response": llm_text,
                    "action": action,
                    "result": result,
                }, indent=2, default=str)
        except Exception as e:
            logger.warning(f"LLM call failed: {e}")

    # No LLM or LLM failed — return the prompt for manual action
    return json.dumps({
        "observation": obs.appshot,
        "llm_response": None,
        "action": None,
        "action_prompt": messages[-1]["content"][:1000],
        "hint": "Set DEEPSEEK_API_KEY env var for autonomous action selection",
    }, indent=2, default=str)


# ── New Desktop Tools ─────────────────────────────────────────────


@mcp.tool()
def list_windows() -> str:
    """List all open top-level windows with their titles, roles, and PIDs.

    Returns a JSON array of window info objects. Each has title, role,
    pid, visible, and focused state. Useful for discovering what's open
    on the desktop before targeting a specific window.
    """
    provider = _get_provider()
    try:
        if hasattr(provider, "_collector") and provider._collector is not None:
            collector = provider._collector
            windows = collector.list_all_windows()
            return json.dumps({"success": True, "windows": windows}, indent=2, default=str)
        # Fallback via uiautomation directly
        from .ax_collector_windows import _ensure_com, WindowsCollector
        _ensure_com()
        c = WindowsCollector()
        windows = c.list_all_windows()
        return json.dumps({"success": True, "windows": windows}, indent=2, default=str)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def get_cursor_position() -> str:
    """Get the current mouse cursor position on screen.

    Returns JSON with x, y coordinates of the cursor. Use this before
    mouse_move or mouse_click to know where the mouse is.
    """
    import ctypes
    try:
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
        pt = POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return json.dumps({"success": True, "x": pt.x, "y": pt.y})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def get_screen_size() -> str:
    """Get the screen resolution dimensions.

    Returns JSON with width and height of the primary monitor.
    """
    import ctypes
    try:
        user32 = ctypes.windll.user32
        w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        return json.dumps({"success": True, "width": w, "height": h})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def press_key(keys: str) -> str:
    """Press a keyboard key or key combination.

    Args:
        keys: Key to press. Single keys: 'enter', 'tab', 'escape',
              'space', 'backspace', 'delete', 'up', 'down', 'left', 'right',
              'home', 'end', 'pageup', 'pagedown'.
              Combos: 'ctrl+c', 'ctrl+v', 'alt+f4', 'ctrl+shift+esc'.
              Modifier order: ctrl/alt/shift/meta + key.

    Returns JSON with success status and the keys pressed.
    """
    provider = _get_provider()
    try:
        result = provider.act({"action": "key_press", "target": keys})
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def type_text(text: str) -> str:
    """Type text at the currently focused element.

    Args:
        text: The text to type. Supports regular characters and common
              special sequences.

    Returns JSON with success status and what was typed.
    """
    provider = _get_provider()
    try:
        result = provider.act({"action": "type", "value": text})
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def mouse_move(x: int, y: int) -> str:
    """Move the mouse cursor to an absolute screen position.

    Args:
        x: Target x-coordinate in screen pixels
        y: Target y-coordinate in screen pixels

    Returns JSON with success status and the target position.
    """
    import ctypes
    try:
        ctypes.windll.user32.SetCursorPos(x, y)
        return json.dumps({"success": True, "x": x, "y": y, "action": "mouse_move"})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def mouse_click(
    button: str = "left",
    x: int | None = None,
    y: int | None = None,
    double: bool = False,
) -> str:
    """Perform a mouse click, optionally at a specific position.

    Args:
        button: Which button — 'left', 'right', 'middle' (default: 'left')
        x: Optional x-coordinate. If provided moves cursor first.
        y: Optional y-coordinate. If provided moves cursor first.
        double: If True, performs a double-click instead of single click.

    Returns JSON with success status and click details.
    """
    import ctypes
    try:
        # Move to position if provided
        if x is not None and y is not None:
            ctypes.windll.user32.SetCursorPos(x, y)

        # Map button to Win32 flags
        if button == "left":
            down, up = 0x0002, 0x0004
        elif button == "right":
            down, up = 0x0008, 0x0010
        elif button == "middle":
            down, up = 0x0020, 0x0040
        else:
            return json.dumps({"success": False, "error": f"Unknown button: {button}"})

        if double:
            ctypes.windll.user32.mouse_event(down, 0, 0, 0, 0)
            ctypes.windll.user32.mouse_event(up, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(down, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(up, 0, 0, 0, 0)

        return json.dumps({
            "success": True, "button": button,
            "x": x, "y": y, "double": double,
            "action": "mouse_click"
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ── Helpers ──

def _get_security() -> SecurityContext | None:
    """Get the global security context (None if --security not enabled)."""
    return _security_context


def _get_optimization_config() -> OptimizationConfig | None:
    """Get the global optimization config (None if --optimize not enabled)."""
    return _optimization_config


def _get_provider() -> ActionProvider:
    """Get or create the action provider based on the current platform."""
    if state.action_provider is None:
        if sys.platform == "win32":
            state.action_provider = WindowsActionProvider()
        else:
            state.action_provider = StubActionProvider()
    return state.action_provider


def loop_parse_action(text: str) -> dict | None:
    """Minimal JSON action parser (duplicates agent_loop._parse_action logic
    for standalone use in observe_and_act)."""
    text = text.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    if "```json" in text:
        try:
            return json.loads(text.split("```json")[1].split("```")[0].strip())
        except (IndexError, json.JSONDecodeError):
            pass
    if "```" in text:
        try:
            jt = text.split("```")[1]
            if jt.startswith("json"):
                jt = jt[4:]
            return json.loads(jt.strip())
        except (IndexError, json.JSONDecodeError):
            pass
    # Brace counting fallback
    brace_depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if start == -1:
                start = i
            brace_depth += 1
        elif ch == "}":
            brace_depth -= 1
            if brace_depth == 0 and start != -1:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start = -1
    return None


# ── AppState filtering ──

def _filter_interactive_only(node: dict) -> dict:
    """Remove non-interactive elements from the accessibility tree.

    Keeps only elements with roles that the LLM can interact with.
    Uses the shared INTERACTIVE_ROLES set from prompt_builder.py
    to ensure consistency.
    """
    def _walk(n: dict) -> dict | None:
        role = n.get("role", "")
        if not role:
            return None

        children = n.get("children", [])
        kept_children = []
        for child in children:
            result = _walk(child)
            if result is not None:
                kept_children.append(result)

        if role in INTERACTIVE_ROLES or kept_children:
            result = {}
            for key in ("role", "title", "value", "label", "desc"):
                if key in n and n[key]:
                    result[key] = n[key]
            for key in ("focused", "selected", "enabled"):
                if key in n:
                    result[key] = n[key]
            for key in ("pos", "size"):
                if key in n:
                    result[key] = n[key]
            for key in ("actions", "automation_id"):
                if key in n:
                    result[key] = n[key]
            if "_app" in n:
                result["_app"] = n["_app"]
            if "_pid" in n:
                result["_pid"] = n["_pid"]
            if "_platform" in n:
                result["_platform"] = n["_platform"]
            if kept_children:
                result["children"] = kept_children
            return result
        return None

    return _walk(node) or {"role": "Desktop", "title": "No interactive elements"}


def _strip_window_metadata(node: dict) -> dict:
    """Remove window metadata keys from the tree."""
    meta_keys = {"_app", "_pid", "_platform"}
    result = {k: v for k, v in node.items() if k not in meta_keys}
    if "children" in result:
        result["children"] = [_strip_window_metadata(c) for c in result["children"]]
    return result


# ── Win32 HWND helpers (fallback for custom/non-AX controls) ──


def _hwnd_from_point(x: int, y: int) -> int | None:
    """Get window handle (HWND) at screen coordinates using Win32 API.

    Useful for custom controls, game engines, Electron apps, legacy
    MFC/Win32, and web views that don't expose UIA properties.
    These elements may still have an HWND even if they're invisible to UIA.

    Returns the HWND (int) or None if no window found at that point.
    """
    import ctypes
    try:
        return ctypes.windll.user32.WindowFromPoint(x, y)
    except Exception:
        return None


def _hwnd_element_info(hwnd: int) -> dict:
    """Get basic UI information from an HWND via Win32 API.

    This is a fallback for elements that don't expose UIA properties.
    Uses only Win32 API calls (no COM/UIA dependency).

    Returns a dict with:
        hwnd: int — The window handle
        title: str — Window text (via GetWindowTextW)
        class_name: str — Window class (via GetClassNameW)
        rect: tuple — (left, top, right, bottom) bounding rectangle
        pid: int — Process ID
        parent_hwnd: int — Parent window handle (0 if top-level)
        via: str — Always "win32_hwnd" for identification
    """
    import ctypes
    from ctypes import wintypes

    info: dict = {"hwnd": hwnd, "via": "win32_hwnd"}

    try:
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
        info["title"] = buf.value or ""
    except Exception:
        info["title"] = ""

    try:
        class_buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, class_buf, 256)
        info["class_name"] = class_buf.value or ""
    except Exception:
        info["class_name"] = ""

    try:
        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        info["rect"] = (rect.left, rect.top, rect.right, rect.bottom)
        info["pos"] = {
            "x": round((rect.left + rect.right) / 2),
            "y": round((rect.top + rect.bottom) / 2),
        }
        info["size"] = {
            "w": round(rect.right - rect.left),
            "h": round(rect.bottom - rect.top),
        }
    except Exception:
        pass

    try:
        info["parent_hwnd"] = ctypes.windll.user32.GetParent(hwnd)
    except Exception:
        info["parent_hwnd"] = 0

    try:
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        info["pid"] = pid.value
    except Exception:
        pass

    return info


def _hwnd_get_child_windows(hwnd: int) -> list[dict]:
    """Enumerate all child windows of an HWND via Win32 EnumChildWindows.

    Returns a flat list of child window info dicts (title, class_name, rect, etc.).
    Useful for finding sub-elements inside custom controls.
    """
    import ctypes
    from ctypes import wintypes

    results: list[dict] = []

    def _enum_child(hwnd_child: int, lparam: int) -> bool:
        try:
            info = _hwnd_element_info(hwnd_child)
            if info["title"] or info.get("class_name"):
                results.append(info)
        except Exception:
            pass
        return True

    cb_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    try:
        callback = cb_type(_enum_child)
        ctypes.windll.user32.EnumChildWindows(hwnd, callback, 0)
    except Exception:
        pass

    return results


def _hwnd_click(hwnd: int, x: int | None = None, y: int | None = None, button: str = 'left') -> bool:
    """Click on an HWND using Win32 mouse_event for custom/non-AX controls.

    Uses mouse_event (global cursor simulation) which works with all Windows apps
    including custom controls, game engine UIs, Electron/web views, and legacy Win32.
    Avoids SendMessage/PostMessage to prevent double-issue with the real mouse event.

    Args:
        hwnd: Window handle to click on
        x, y: Optional screen coordinates. If omitted, clicks at center.
        button: 'left' (default), 'right', or 'middle'

    Returns:
        True if the click was attempted, False on failure
    """
    import ctypes
    from ctypes import wintypes
    import time

    try:
        ctypes.windll.user32.BringWindowToTop(hwnd)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        time.sleep(0.1)

        if x is None or y is None:
            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            x = (rect.left + rect.right) // 2
            y = (rect.top + rect.bottom) // 2

        ctypes.windll.user32.SetCursorPos(x, y)
        time.sleep(0.05)

        if button == 'right':
            ctypes.windll.user32.mouse_event(0x0008, 0, 0, 0, 0)  # MOUSEEVENTF_RIGHTDOWN
            time.sleep(0.05)
            ctypes.windll.user32.mouse_event(0x0010, 0, 0, 0, 0)  # MOUSEEVENTF_RIGHTUP
        elif button == 'middle':
            ctypes.windll.user32.mouse_event(0x0020, 0, 0, 0, 0)  # MOUSEEVENTF_MIDDLEDOWN
            time.sleep(0.05)
            ctypes.windll.user32.mouse_event(0x0040, 0, 0, 0, 0)  # MOUSEEVENTF_MIDDLEUP
        else:
            ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
            time.sleep(0.05)
            ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP

        return True
    except Exception:
        return False


# ── CLI entry point ──

def main():
    """Run the MCP server."""
    import argparse

    parser = argparse.ArgumentParser(description="CUA Driver MCP Server")
    parser.add_argument(
        "--collector", choices=["windows", "stub"], default=None,
        help="Force a specific action provider (default: auto-detect platform)",
    )
    parser.add_argument(
        "--log-level", default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )
    parser.add_argument(
        "--security", action="store_true", default=False,
        help="Enable security layer (rate limiting, dangerous action detection)",
    )
    parser.add_argument(
        "--no-block-dangerous", action="store_true",
        help="When --security is on, allow dangerous actions with a warning (default: block)",
    )
    parser.add_argument(
        "--security-allow-apps", type=str, default=None,
        help="Comma-separated list of allowed apps (when --security is on)",
    )
    parser.add_argument(
        "--security-block-apps", type=str, default=None,
        help="Comma-separated list of blocked apps (when --security is on)",
    )
    parser.add_argument(
        "--security-read-only", action="store_true", default=False,
        help="When --security is on, enable read-only mode (no actions executed)",
    )
    parser.add_argument(
        "--optimize", action="store_true", default=False,
        help="Enable performance optimizations (action merging, caching, timing)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if args.collector == "windows":
        state.action_provider = WindowsActionProvider()
        logger.info("Using Windows action provider")
    elif args.collector == "stub":
        state.action_provider = StubActionProvider()
        logger.info("Using STUB action provider (no actual UI actions)")

    # ── Initialize security context if enabled ──
    global _security_context
    if args.security:
        allowed_apps = None
        blocked_apps = set()
        if args.security_allow_apps:
            allowed_apps = {a.strip().lower() for a in args.security_allow_apps.split(",")}
        if args.security_block_apps:
            blocked_apps = {a.strip().lower() for a in args.security_block_apps.split(",")}

        from .security import ActionValidator, SandboxConfig
        _security_context = SecurityContext(
            validator=ActionValidator(
                allowed_apps=allowed_apps if allowed_apps else None,
                blocked_apps=blocked_apps,
                block_dangerous=not args.no_block_dangerous,
            ),
            sandbox=SandboxConfig(
                read_only=args.security_read_only,
            ),
        )
        logger.info(f"Security enabled (block_dangerous={not args.no_block_dangerous}, "
                    f"read_only={args.security_read_only})")
        if allowed_apps:
            logger.info(f"  Allowed apps: {allowed_apps}")
        if blocked_apps:
            logger.info(f"  Blocked apps: {blocked_apps}")
    else:
        _security_context = None
        logger.info("Security disabled")

    # ── Initialize optimization if enabled ──
    global _optimization_config, _optimization_merger, _optimization_cache
    global _optimization_timer, _optimization_tokenizer
    if args.optimize:
        _optimization_config = OptimizationConfig()
        _optimization_merger = _optimization_config.create_merger()
        _optimization_cache = _optimization_config.create_cache()
        _optimization_timer = _optimization_config.create_timer()
        _optimization_tokenizer = _optimization_config.create_token_optimizer()
        logger.info(f"Optimization enabled: merger={_optimization_merger.enabled}, "
                    f"cache={_optimization_cache.ttl_seconds}s, "
                    f"timer={_optimization_timer.enabled}")
    else:
        _optimization_config = None
        _optimization_merger = None
        _optimization_cache = None
        _optimization_timer = None
        _optimization_tokenizer = None
        logger.info("Optimization disabled")

    # ── Warm observation cache on startup ──
    _ensure_warmup()

    logger.info("Starting CUA Driver MCP server on stdio...")
    mcp.run()


if __name__ == "__main__":
    main()
