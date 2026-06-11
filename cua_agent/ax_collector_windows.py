"""ax_collector_windows.py — Windows accessibility tree collector using UI Automation.

Uses the `uiautomation` Python package (by yinkaisheng) to walk the
Windows UI Automation tree from the foreground window and produce a
structured JSON appstate dict compatible with the PromptBuilder.

Usage:
    collector = WindowsCollector()
    state = collector.get_foreground_app_state()
    print(json.dumps(state, indent=2))

Requirements:
    - uiautomation (pip install uiautomation)
    - Run as Administrator for full access to all processes
"""

from __future__ import annotations

import ctypes
import json
import logging
import sys
import threading
import time
from typing import Any

import uiautomation as auto

# ── COM initialization ──────────────────────────────────────────────────
# The uiautomation library requires COM initialized on the calling thread.
# FastMCP dispatches tool handlers on arbitrary threads, so we must
# initialise COM at the entry point of every public method.

_OLE32 = ctypes.windll.ole32
_OLE32.CoInitializeEx.restype = ctypes.c_long
_OLE32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
_OLE32.CoUninitialize.restype = None

_COINIT_APARTMENTTHREADED = 0x2  # COINIT_APARTMENTTHREADED


def _ensure_com() -> None:
    """Initialise COM on the calling thread if not already done.

    Safe to call repeatedly — CoInitializeEx with the same concurrency
    model increments an internal reference count on the same thread.
    We do NOT call CoUninitialize here because uiautomation may still
    need COM alive for lazily-evaluated COM properties on the same
    thread.  The OS reclaims COM state at thread exit automatically.
    """
    hr = _OLE32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    if hr == 0:                 # S_OK — first init on this thread
        logger = logging.getLogger("ax-collector-windows")
        logger.debug("COM initialized on thread %d", threading.get_ident())
    elif hr == 1:               # S_FALSE — already initialized on this thread
        pass
    else:
        logger = logging.getLogger("ax-collector-windows")
        logger.warning("CoInitializeEx returned 0x%08x", hr)

logger = logging.getLogger("ax-collector-windows")

# ── ControlType name resolution ──
# uiautomation provides ControlTypeName directly, but we also map from
# ControlType IDs for safety.
CONTROL_TYPE_NAMES: dict[int, str] = {
    50000: "Window", 50001: "Pane", 50002: "Button", 50003: "CheckBox",
    50004: "RadioButton", 50005: "ComboBox", 50006: "Edit", 50007: "Hyperlink",
    50008: "Image", 50009: "ListItem", 50010: "List", 50011: "Menu",
    50012: "MenuBar", 50013: "MenuItem", 50014: "ProgressBar", 50015: "ScrollBar",
    50016: "Slider", 50017: "Spinner", 50018: "StatusBar", 50019: "Tab",
    50020: "TabItem", 50021: "Table", 50022: "Text", 50023: "ToolBar",
    50024: "ToolTip", 50025: "Tree", 50026: "TreeItem", 50027: "Custom",
    50028: "Group", 50029: "Thumb", 50030: "DataGrid", 50031: "DataItem",
    50032: "Document", 50033: "SplitButton", 50034: "MenuButton",
    50035: "DropDownButton", 50036: "AppBar", 50037: "SemanticZoom",
    50038: "Separator", 50039: "TitleBar", 50040: "Header",
    50041: "HeaderItem", 50042: "Calendar", 50043: "DatePicker",
}


class WindowsCollector:
    """Collects the Windows UI Automation tree from the foreground window.

    Walks the UIA tree recursively, collecting role, name, value,
    position, focused/enabled state, and Automation ID for each element.

    Performance characteristics:
    - Full tree walk: ~15-30ms per node (each node makes ~5 COM cross-process calls)
    - A complex app (VS Code, Chrome) can have 500-2000+ nodes → 8-60s total
    - Use fast_mode=True to skip expensive pattern detection (3x faster)
    - Use timeout_seconds to abort long walks gracefully
    - Tree walk stops after _max_interactive_nodes interactive elements are found
    - Leaf containers (ScrollBar, StatusBar, etc.) skip child enumeration entirely
    """

    # Roles that are NOT interactive — skip expensive pattern detection for these
    _SKIP_PATTERNS_ROLES: frozenset = frozenset({
        "AXPane", "AXGroup", "AXWindow", "AXTitleBar", "AXMenuBar",
        "AXToolBar", "AXStatusBar", "AXScrollBar", "AXProgressBar",
        "AXSeparator", "AXHeader", "AXAppBar", "AXToolTip",
        "Pane", "Group", "Window", "TitleBar", "MenuBar",
        "ToolBar", "StatusBar", "ScrollBar", "ProgressBar",
        "Separator", "Header", "AppBar", "ToolTip",
    })

    # Roles that are leaf containers — skip child enumeration entirely to avoid COM calls
    _SKIP_CHILDREN_ROLES: frozenset = frozenset({
        "AXScrollBar", "AXProgressBar", "AXSeparator", "AXToolTip",
        "AXTitleBar", "AXMenuBar",
        "ScrollBar", "ProgressBar", "Separator", "ToolTip",
        "TitleBar", "MenuBar",
    })

    def __init__(
        self,
        max_depth: int = 15,
        max_children: int = 100,
        wait_seconds: float = 0.3,
        fast_mode: bool = False,
        timeout_seconds: float = 10.0,
        compute_patterns: bool = True,
        max_interactive_nodes: int = 200,
        adaptive_timeout: bool = True,
        initial_timeout: float = 2.0,
        max_timeout: float = 15.0,
        timeout_backoff: float = 2.0,
    ):
        self._max_depth = max_depth
        self._max_children = max_children
        self._wait_seconds = wait_seconds
        self._fast_mode = fast_mode
        self._timeout_seconds = timeout_seconds
        self._compute_patterns = compute_patterns and not fast_mode
        self._start_time: float = 0.0
        self._node_count: int = 0
        self._max_nodes: int = 3000
        self._max_interactive_nodes: int = max_interactive_nodes
        self._interactive_found: int = 0
        # Adaptive timeout: starts short, backs off on truncation
        self._adaptive_timeout = adaptive_timeout
        self._initial_timeout = initial_timeout
        self._max_timeout = max_timeout
        self._timeout_backoff = timeout_backoff
        self._current_timeout = initial_timeout

    def get_foreground_app_state(self) -> dict:
        """Get the accessibility tree of the foreground window.

        Uses adaptive timeout: starts at initial_timeout (2s) and backs off
        up to max_timeout (15s) when trees are truncated. On a successful
        full walk, the timeout resets to initial_timeout.

        Returns:
            The full appstate dict with role, title, children, etc.
            On error, returns a dict with an "error" key.
        """
        _ensure_com()
        self._start_time = time.time()
        self._node_count = 0
        self._interactive_found = 0

        # Apply adaptive timeout before each walk
        if self._adaptive_timeout:
            self._timeout_seconds = self._current_timeout

        try:
            # Get the foreground window
            window = auto.GetForegroundControl()
            if not window or not window.Exists(0, 0):
                return self._error_result("No foreground window found")

            # Build tree
            tree = self._build_node(window, depth=0)
            if not tree:
                return self._error_result("Failed to build accessibility tree")

            # Get process info from the root
            tree["_platform"] = "windows"
            tree["_node_count"] = self._node_count

            # Try to get app name from the window's process
            try:
                pid = window.ProcessId
                tree["_pid"] = pid
            except Exception:
                pass

            # ── Adaptive timeout: back off if truncated, reset if complete ──
            if self._adaptive_timeout:
                if tree.get("_truncated"):
                    old_timeout = self._current_timeout
                    self._current_timeout = min(
                        self._current_timeout * self._timeout_backoff,
                        self._max_timeout,
                    )
                    logger.debug(
                        f"Adaptive timeout: {old_timeout:.1f}s → {self._current_timeout:.1f}s "
                        f"(tree truncated)"
                    )
                else:
                    if self._current_timeout != self._initial_timeout:
                        logger.debug(
                            f"Adaptive timeout: {self._current_timeout:.1f}s → {self._initial_timeout:.1f}s "
                            f"(walk completed)"
                        )
                    self._current_timeout = self._initial_timeout

            return tree

        except Exception as e:
            return self._error_result(str(e))

    def get_focused_app_state(self) -> dict:
        """Get the accessibility tree starting from the focused element.

        Uses adaptive timeout: starts at initial_timeout (2s) and backs off
        up to max_timeout (15s) when trees are truncated.

        Returns:
            Appstate dict starting from the focused element (useful after
            interacting with a specific control).
        """
        _ensure_com()
        if self._adaptive_timeout:
            self._timeout_seconds = self._current_timeout
        self._start_time = time.time()
        self._node_count = 0
        self._interactive_found = 0

        try:
            focused = auto.GetFocusedControl()
            if not focused or not focused.Exists(0, 0):
                return self._error_result("No focused control found")

            # Walk up to the window root for context
            tree = self._build_node(focused, depth=0)
            if tree and self._adaptive_timeout:
                if tree.get("_truncated"):
                    self._current_timeout = min(
                        self._current_timeout * self._timeout_backoff,
                        self._max_timeout,
                    )
                else:
                    self._current_timeout = self._initial_timeout
            return tree or self._error_result("Failed to build from focused element")
        except Exception as e:
            return self._error_result(str(e))

    def get_app_state_for_window(self, window_title: str) -> dict:
        """Get accessibility tree for a specific window by title.

        Uses adaptive timeout: starts at initial_timeout (2s) and backs off
        up to max_timeout (15s) when trees are truncated.

        Args:
            window_title: Window title to search for

        Returns:
            Appstate dict for the matching window.
        """
        _ensure_com()
        if self._adaptive_timeout:
            self._timeout_seconds = self._current_timeout
        self._start_time = time.time()
        self._node_count = 0
        self._interactive_found = 0

        try:
            window = auto.WindowControl(searchDepth=1, Name=window_title)
            if not window.Exists(self._wait_seconds):
                return self._error_result(f"Window not found: {window_title}")

            tree = self._build_node(window, depth=0)
            if tree and self._adaptive_timeout:
                if tree.get("_truncated"):
                    self._current_timeout = min(
                        self._current_timeout * self._timeout_backoff,
                        self._max_timeout,
                    )
                else:
                    self._current_timeout = self._initial_timeout
            return tree or self._error_result(f"Failed to build tree for window: {window_title}")
        except Exception as e:
            return self._error_result(str(e))

    def is_administrator(self) -> bool:
        """Check if the process is running as Administrator.

        Administrator access is required for full UIA tree access to
        elevated processes (like Chrome, Visual Studio, etc.).
        """
        import ctypes
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False

    # ── Internal helpers ──

    def _check_limits(self) -> bool:
        """Check if we've exceeded time or node limits. Returns True if should abort.
        Increments the node counter (use for counting unique nodes).
        """
        self._node_count += 1
        return self._limits_exceeded()

    def _limits_exceeded(self) -> bool:
        """Check limits without incrementing node counter.
        Use this inside child iteration loops to avoid double-counting.
        """
        if self._node_count > self._max_nodes:
            return True
        if self._timeout_seconds > 0:
            elapsed = time.time() - self._start_time
            if elapsed > self._timeout_seconds:
                return True
        return False

    def _build_node(self, control: auto.Control, depth: int) -> dict | None:
        """Recursively build a node from a UIA control, with early exit for performance.

        Performance optimizations:
        1. Early exit when max_interactive_nodes is reached
        2. Skip child enumeration for known leaf container types (scrollbars, etc.)
        3. Lazy child iteration: materialize child COM objects only as we traverse
        4. In fast_mode, non-interactive roles skip property reads beyond basics
        """
        if depth > self._max_depth:
            return None

        # Check global limits (timeout + max nodes)
        if self._check_limits():
            return None

        try:
            role = self._get_role(control)
            if not role:
                return None

            node: dict[str, Any] = {"role": role}

            # ── Batch-read properties from the control (fewer COM trips) ──
            self._read_node_properties(control, node, role)

            # ── Track interactive nodes for early exit ──
            is_interactive = role not in self._SKIP_PATTERNS_ROLES
            if is_interactive:
                self._interactive_found += 1

            # ── Early exit: stop if we have enough interactive nodes ──
            if self._interactive_found > self._max_interactive_nodes:
                return node

            # ── Skip children for leaf containers (avoid unnecessary COM calls) ──
            if role in self._SKIP_CHILDREN_ROLES:
                return node

            # ── In fast mode, also skip large non-interactive containers ──
            if self._fast_mode and not is_interactive and depth > 2:
                return node

            # ── Build children lazily ──
            children = _safe_get_children(control)
            if children:
                kept = []
                total = len(children)
                for i, child in enumerate(children):
                    if len(kept) >= self._max_children:
                        node["_truncated"] = (
                            f"children limited to {self._max_children} "
                            f"of {total}"
                        )
                        break
                    # Early exit: stop processing remaining children if limits hit
                    # Note: use _limits_exceeded() instead of _check_limits() to avoid
                    # double-counting the parent node on each child iteration
                    if self._interactive_found > self._max_interactive_nodes or self._limits_exceeded():
                        if len(kept) < total:
                            remaining = total - i
                            node["_truncated"] = f"walk stopped ({remaining} remaining children)"
                        break
                    child_node = self._build_node(child, depth + 1)
                    if child_node is not None:
                        kept.append(child_node)
                if kept:
                    node["children"] = kept

            return node

        except Exception:
            return None

    def _read_node_properties(self, control: auto.Control, node: dict, role: str) -> None:
        """Read all node properties from a control, minimizing COM cross-process calls.

        Batches all property reads into a single try/except block so that
        a single COM failure doesn't cascade into 8 individual try/except calls.
        For non-interactive roles, skips expensive pattern detection.
        """
        try:
            # Name / title (single COM call)
            name = control.Name
            if name:
                node["title"] = name

            # Automation ID (skip if not useful, e.g. in fast mode)
            if not self._fast_mode:
                try:
                    aid = control.AutomationId
                    if aid:
                        node["automation_id"] = aid
                except Exception:
                    pass

            # Focused / enabled (cheap boolean COM calls)
            try:
                node["focused"] = bool(control.HasKeyboardFocus)
            except Exception:
                pass
            try:
                node["enabled"] = bool(control.IsEnabled)
            except Exception:
                pass

            # Position (bounding rectangle)
            try:
                rect = control.BoundingRectangle
                if rect and rect.right > rect.left and rect.bottom > rect.top:
                    node["pos"] = {
                        "x": round((rect.left + rect.right) / 2),
                        "y": round((rect.top + rect.bottom) / 2),
                    }
                    node["size"] = {
                        "w": round(rect.right - rect.left),
                        "h": round(rect.bottom - rect.top),
                    }
            except Exception:
                pass

            # Only compute patterns/value/selection for interactive roles
            is_interactive = role not in self._SKIP_PATTERNS_ROLES

            if is_interactive and self._compute_patterns:
                try:
                    value = _safe_get_value(control)
                    if value is not None:
                        node["value"] = value
                except Exception:
                    pass

                try:
                    selected = _safe_is_selected(control)
                    if selected:
                        node["selected"] = True
                except Exception:
                    pass

                # Supported patterns — use fast path first
                actions = _safe_get_supported_patterns_fast(control)
                if actions:
                    node["actions"] = actions

        except Exception:
            pass

    @staticmethod
    def _get_role(control: auto.Control) -> str:
        """Get a consistent role string from a UIA control."""
        # Try ControlTypeName first (e.g., "Button", "Edit", "Window")
        try:
            ct_name = control.ControlTypeName
            if ct_name:
                return _normalize_role(ct_name)
        except Exception:
            pass

        # Fall back to ControlType ID
        try:
            ct = control.ControlType
            if ct is not None:
                raw = int(ct)
                return CONTROL_TYPE_NAMES.get(raw, f"UIA_{raw}")
        except Exception:
            pass

        return ""

    def list_all_windows(self) -> list[dict]:
        """List all open top-level windows with their titles, roles, and PIDs.

        Returns a list of window info dicts. Each has:
            title: str — The window title
            role: str — e.g. "AXWindow"
            pid: int — Process ID
            visible: bool — Whether the window is visible
            focused: bool — Whether the window has focus
            pos: dict — Center position {x, y}
            size: dict — Size {w, h}

        On error, returns an empty list.
        """
        _ensure_com()
        try:
            root = auto.GetRootControl()
            if not root:
                return []

            results = []
            children = _safe_get_children(root)
            for child in children[:100]:  # Cap at 100 windows
                try:
                    role = self._get_role(child)
                    if "Window" not in role and "Pane" not in role:
                        continue

                    info = {
                        "title": child.Name or "",
                        "role": role,
                        "visible": False,
                        "focused": False,
                    }

                    try:
                        info["pid"] = child.ProcessId
                    except Exception:
                        pass

                    try:
                        info["visible"] = bool(child.IsOffscreen is False)
                    except Exception:
                        pass

                    try:
                        info["focused"] = bool(child.HasKeyboardFocus)
                    except Exception:
                        pass

                    try:
                        rect = child.BoundingRectangle
                        if rect:
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

                    results.append(info)
                except Exception:
                    continue

            return results
        except Exception as e:
            logger.warning(f"list_all_windows failed: {e}")
            return []

    def get_element_at(self, x: int, y: int) -> dict:
        """Get structured element info at a specific screen position.

        Args:
            x: Screen x coordinate
            y: Screen y coordinate

        Returns:
            Element info dict with role, title, automation_id, pos, size,
            focused, enabled, actions, and value (if applicable).
            Returns an error-result dict if nothing found.
        """
        _ensure_com()
        try:
            control = auto.ControlFromPoint(x, y)
            if not control:
                return self._error_result(f"No element at ({x}, {y})")

            node = self._build_node(control, depth=0)
            if node is None:
                return self._error_result(f"Could not build info for element at ({x}, {y})")

            node["_probed_at"] = {"x": x, "y": y}
            return node
        except Exception as e:
            return self._error_result(f"Error probing ({x}, {y}): {e}")

    @staticmethod
    def _error_result(msg: str) -> dict:
        return {"error": msg, "role": "", "title": "", "children": []}


# ── Module-level helpers (try/except wrappers for flaky COM calls) ──


def _safe_get_value(control: auto.Control) -> str | None:
    """Get the value of a control, if it supports the Value pattern."""
    try:
        if hasattr(control, "GetValuePattern"):
            pattern = control.GetValuePattern()
            if pattern:
                val = pattern.Value
                if val is not None:
                    s = str(val)
                    return s[:100] + "…" if len(s) > 100 else s
    except Exception:
        pass
    return None


def _safe_is_selected(control: auto.Control) -> bool:
    """Check if a control is selected (checkboxes, list items, etc.)."""
    try:
        if hasattr(control, "GetSelectionItemPattern"):
            pattern = control.GetSelectionItemPattern()
            if pattern:
                return bool(pattern.IsSelected)
    except Exception:
        pass
    try:
        if hasattr(control, "GetTogglePattern"):
            pattern = control.GetTogglePattern()
            if pattern:
                # ToggleState_On = 1, ToggleState_Off = 0
                return bool(pattern.CurrentToggleState)
    except Exception:
        pass
    return False


def _safe_get_children(control: auto.Control) -> list:
    """Get children of a control, returning empty list on failure."""
    try:
        kids = control.GetChildren()
        return kids if kids else []
    except Exception:
        return []


def _safe_get_supported_patterns(control: auto.Control) -> list[str]:
    """Get the list of UIA patterns this control supports (as action names).

    Slower fallback: tries each of 8 patterns individually via COM cross-process calls.
    Prefer _safe_get_supported_patterns_fast() which uses GetSupportedPatterns.
    """
    pattern_to_action = _PATTERN_ACTION_MAP
    actions: list[str] = []
    try:
        if hasattr(control, "GetSupportedPatterns"):
            patterns = control.GetSupportedPatterns()
            if patterns:
                for p in patterns:
                    p_name = p if isinstance(p, str) else p.__class__.__name__
                    mapped = pattern_to_action.get(p_name)
                    if mapped and mapped not in actions:
                        actions.append(mapped)
                return actions

        # Fallback: try each pattern individually (~8 COM calls)
        for pattern_name, action in pattern_to_action.items():
            try:
                method = getattr(control, f"Get{pattern_name}", None)
                if method:
                    result = method()
                    if result is not None:
                        actions.append(action)
            except Exception:
                pass
    except Exception:
        pass
    return actions


_PATTERN_ACTION_MAP = {
    "InvokePattern": "click",
    "ExpandCollapsePattern": "expand",
    "TogglePattern": "toggle",
    "SelectionItemPattern": "select",
    "ValuePattern": "type",
    "ScrollPattern": "scroll",
    "TextPattern": "read",
    "RangeValuePattern": "adjust",
}


def _safe_get_supported_patterns_fast(control: auto.Control) -> list[str]:
    """Fast single-call pattern detection using GetSupportedPatterns.

    Makes only 1 COM call instead of up to 8.
    Returns empty list if GetSupportedPatterns is not available.
    """
    actions: list[str] = []
    try:
        if hasattr(control, "GetSupportedPatterns"):
            patterns = control.GetSupportedPatterns()
            if patterns:
                for p in patterns:
                    p_name = p if isinstance(p, str) else p.__class__.__name__
                    mapped = _PATTERN_ACTION_MAP.get(p_name)
                    if mapped and mapped not in actions:
                        actions.append(mapped)
    except Exception:
        pass
    return actions


def _normalize_role(role: str) -> str:
    """Normalize role names for consistency with prompt_builder.

    - Adds 'AX' prefix for cross-platform compatibility
    - Strips 'Control' suffix (e.g., 'EditControl' -> 'Edit')
    - Strips 'ControlType' suffix (e.g., 'ButtonControlType' -> 'Button')
    """
    # First strip trailing 'Control' or 'ControlType' suffixes
    # uiautomation sometimes returns names like 'EditControl' or 'ButtonControlType'
    if role.endswith("ControlType"):
        role = role[:-len("ControlType")]
    elif role.endswith("Control"):
        role = role[:-len("Control")]

    if role.startswith("AX"):
        return role
    # uiautomation usually returns names like "Button", "Edit", "Window"
    # We prefix with AX for consistency with the prompt builder's INTERACTIVE_ROLES
    return f"AX{role}"


# ── Convenience ──

def is_available() -> bool:
    """Check if the Windows collector can be used on this platform."""
    return sys.platform == "win32"


# ── Quick test ──

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    collector = WindowsCollector()

    if not is_available():
        print("❌ Windows collector requires Windows.")
        sys.exit(1)

    is_admin = collector.is_administrator()
    if not is_admin:
        print("⚠️  Not running as Administrator. Some elements may not be accessible.")
        print("   Restart your terminal as Admin for full UIA tree access.\n")

    print("🔍 Collecting foreground window state...")
    state = collector.get_foreground_app_state()

    if "error" in state:
        print(f"❌ Error: {state['error']}")
        sys.exit(1)

    title = state.get("title", "")
    role = state.get("role", "")
    children_count = len(state.get("children", []))
    pid = state.get("_pid", "?")

    print(f"✅ Window: {title} (PID: {pid})")
    print(f"   Role: {role}")
    print(f"   Top-level children: {children_count}")

    # Print a compact element list
    print("\n📋 Interactive elements:")
    elements = _flatten_summary(state, max_items=20)
    for el in elements:
        print(f"   [{el['id']}] {el['role']} = \"{el['title']}\"")
    if elements:
        print(f"\n   ... {elements[0].get('total', 0)} total elements in tree")


# Static inline set of interactive roles for _flatten_summary
# (duplicated from prompt_builder.INTERACTIVE_ROLES to avoid relative import
# issues when running this file directly as __main__)
_FLATTEN_INTERACTIVE_ROLES = frozenset({
    "AXButton", "AXCheckBox", "AXComboBox", "AXRadioButton",
    "AXSlider", "AXDropDownButton", "AXMenuButton", "AXSplitButton",
    "AXHyperlink", "AXTab", "AXTabItem", "AXMenuItem",
    "AXTreeItem", "AXListItem", "AXDataItem",
    "AXEdit", "AXTextField", "AXText", "AXDocument",
    "AXWindow", "AXPane", "AXGroup",
    "AXList", "AXTree", "AXTable", "AXDataGrid",
    "AXTitleBar", "AXToolBar", "AXStatusBar",
    "AXProgressBar", "AXScrollBar", "AXThumb",
    "AXSeparator", "AXHeader", "AXHeaderItem",
    "AXCalendar", "AXDatePicker", "AXAppBar",
    "AXMenu", "AXMenuBar", "AXToolTip",
    "AXImage", "AXCustom",
})


def _flatten_summary(node: dict, max_items: int = 20) -> list[dict]:
    """Flatten tree to a simple list for quick preview."""
    results: list[dict] = []
    count: list[int] = [0]
    total: list[int] = [0]

    def _walk(n: dict):
        total[0] += 1
        role = n.get("role", "")
        title = n.get("title", "") or n.get("label", "") or ""
        if role in _FLATTEN_INTERACTIVE_ROLES and count[0] < max_items:
            results.append({"id": count[0], "role": role.replace("AX", ""), "title": title[:50]})
            count[0] += 1
        for child in n.get("children", []):
            _walk(child)

    _walk(node)
    if results:
        results[0]["total"] = total[0]
    return results
