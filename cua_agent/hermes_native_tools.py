"""
hermes_native_tools.py — Native Hermes Agent toolset for Windows CUA.

Exposes Windows desktop control tools directly in the Hermes process
(no MCP layer). Gated on sys.platform == "win32" — tools only register
on Windows. On other platforms, tools return descriptive errors.

### Registration

In Hermes, add to your config:

```yaml
toolsets:
  - windows_cua
```

Or register programmatically from any Python process:

```python
from cua_agent.hermes_native_tools import register_tools
register_tools()
```

### Architecture

```
Hermes Agent Process
    │
    ▼
WindowsActionProvider (in-process singleton)
    │
    ├── WindowsCollector  → UI Automation API → Desktop
    ├── AppshotManager    → State diffs
    ├── SyntheticTree     → HWND + OCR fallback
    └── SecurityContext   → Rate limiting + validation
```

All tool calls are direct Python function calls — no serialization,
no subprocess, no stdio.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

logger = logging.getLogger("hermes-native-windows-cua")

# ── Lazy singleton provider ──

_provider: Any = None  # WindowsActionProvider | StubActionProvider
_provider_init_time: float = 0.0
_security_context: Any = None  # Optional SecurityContext


def _is_windows() -> bool:
    """Check if we're on a Windows platform."""
    return sys.platform == "win32"


def _get_provider():
    """Get or create the singleton action provider.

    On Windows, creates a WindowsActionProvider (full UIA).
    On other platforms, creates a StubActionProvider (logs only).
    """
    global _provider, _provider_init_time

    if _provider is not None:
        return _provider

    if _is_windows():
        from .cua_mcp_server import WindowsActionProvider

        provider = WindowsActionProvider()
        # Warm the observation cache on init
        try:
            if hasattr(provider, "warm_cache"):
                provider.warm_cache()
        except Exception:
            pass
        logger.info("Windows CUA provider initialized (native Hermes toolset)")
    else:
        from .cua_mcp_server import StubActionProvider

        provider = StubActionProvider()
        logger.info(
            "CUA provider initialized in STUB mode — no actual UI actions "
            "(platform: %s)", sys.platform
        )

    _provider = provider
    _provider_init_time = time.time()
    return provider


def _get_security():
    """Get or create the optional security context."""
    global _security_context
    if _security_context is None and _is_windows():
        from .security import SecurityContext

        _security_context = SecurityContext()
    return _security_context


def _json_result(data: dict) -> str:
    """Format a result dict as a JSON string (consistent with MCP tools)."""
    return json.dumps(data, indent=2, default=str)


# ── Tool: get_appstate ──


def get_appstate(
    filter_interactive_only: bool = True,
    include_window_info: bool = True,
) -> str:
    """Observe the current desktop state and return a structured accessibility tree.

    Returns a JSON object describing every interactive element on screen:
    buttons, text fields, menus, checkboxes, etc. with their roles,
    labels, values, enabled/focused states, and screen positions.

    Call this at the BEGINNING of a task and after significant navigation
    to understand what's on screen.

    Args:
        filter_interactive_only: If True, returns only interactive elements.
        include_window_info: If True, includes window metadata (title, app name).

    Returns:
        JSON with the full accessibility tree.
    """
    provider = _get_provider()
    try:
        obs = provider.observe()
        appstate = obs.appstate

        if filter_interactive_only:
            appstate = _filter_interactive_only(appstate)
        if not include_window_info:
            appstate = _strip_window_metadata(appstate)

        return _json_result(appstate)
    except Exception as e:
        return _json_result({"error": str(e), "role": "", "title": "", "children": []})


# ── Tool: get_appshot ──


def get_appshot() -> str:
    """Take a state snapshot showing what changed since the last observation.

    Returns a short summary plus a diff showing:
    - What elements appeared (new dialogs, menus, fields)
    - What elements disappeared (dismissed UI)
    - Focus changes (what's now selected/focused)
    - Value changes (text entered, sliders moved)

    Call this AFTER EACH ACTION to verify what happened.
    This is your primary feedback mechanism — the appshot tells you
    whether your action had the expected effect.
    """
    provider = _get_provider()
    try:
        obs = provider.observe()
        return _json_result(obs.appshot)
    except Exception as e:
        return _json_result({
            "error": str(e),
            "id": 0,
            "summary": "Error: could not observe state",
            "type": "error",
        })


# ── Tool: execute_action ──


def execute_action(
    action_type: str,
    target: str = "",
    value: str = "",
) -> str:
    """Execute a UI action on the desktop.

    Supported action types:
      - click:           Click an element by name or position ('200,300')
      - double_click:    Double-click an element
      - right_click:     Right-click (context menu) an element
      - hover:           Hover over an element to reveal tooltips/previews
      - drag:            Drag a source element to a target position or element
      - select:          Select an option from a dropdown/combobox by value
      - resize_window:   Resize foreground window to dimensions ('1024,768')
      - minimize_window: Minimize the foreground window to taskbar
      - maximize_window: Maximize foreground window to full screen
      - close_window:    Close the foreground window gracefully
      - take_screenshot: Capture full screen as base64 PNG
      - take_region_screenshot: Capture screen region as base64 PNG
      - shortcut:        Trigger a named keyboard shortcut (copy, paste, save)
      - type:            Type text into the focused element
      - type_into:       Click an element first, then type text
      - find_element:    Find a UI element by name, position, or automation ID
      - key_press:       Press a key ('enter', 'tab') or combo ('ctrl+c')
      - scroll:          Scroll in a direction ('up', 'down', 'left', 'right')
      - open_app:        Open an application by name ('notepad', 'chrome')
      - open_url:        Open a URL in the default browser
      - wait:            Wait N milliseconds ('500')

    Always call get_appshot() after this to verify the result.

    Args:
        action_type: One of the supported action types listed above.
        target: Element name, position ('200,300'), or key combo.
        value: Text to type, URL to open, or ms to wait.

    Returns:
        JSON with success status, action details, and any error.
    """
    provider = _get_provider()
    action = {"action": action_type, "target": target, "value": value}

    # Security check
    sec = _get_security()
    if sec is not None:
        allowed, reason, _ = sec.check_action(action)
        if not allowed:
            return _json_result({
                "success": False,
                "error": reason,
                "blocked": True,
                "action": action_type,
            })

    try:
        result = provider.act(action)

        # Track in security context
        if sec is not None and result.get("success"):
            sec.record_executed(action)

        return _json_result(result)
    except Exception as e:
        return _json_result({"success": False, "error": str(e), "action": action_type})


# ── Tool: list_actions ──


def list_actions() -> str:
    """List all supported action types with descriptions and parameter schemas.

    Returns a JSON array of action descriptors. Use this to discover
    what actions are available and what parameters each expects.
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
            "params": {
                "target": "Source element name or position",
                "value": "Target position '400,500' or element name",
            },
        },
        {
            "name": "select",
            "description": "Select an option from a dropdown/combobox by value",
            "params": {
                "target": "Combobox/dropdown element name or position",
                "value": "Option text/value to select",
            },
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
            "description": "Capture the full screen as base64-encoded PNG image",
            "params": {},
        },
        {
            "name": "take_region_screenshot",
            "description": "Capture a screenshot of a specific screen region by coordinates or element",
            "params": {
                "target": "'x,y,width,height' region or element name",
                "value": "Optional fallback region 'x,y,w,h'",
            },
        },
        {
            "name": "shortcut",
            "description": "Trigger a named keyboard shortcut (copy, paste, save, new_tab, etc.)",
            "params": {
                "target": "Shortcut name: copy/paste/cut/select_all/save/undo/redo/"
                         "new_tab/close_tab/refresh/lock_screen/open_run, or any key combo",
            },
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
            "description": "Find a UI element by name, automation ID, or position and return its info",
            "params": {"target": "Element name, 'id:xxx', or 'x,y' position"},
        },
        {
            "name": "key_press",
            "description": "Press a key or keyboard shortcut",
            "params": {
                "target": "Key: enter/tab/escape. Combo: ctrl+c, alt+f4, ctrl+shift+s",
            },
        },
        {
            "name": "scroll",
            "description": "Scroll in a direction",
            "params": {
                "target": "up/down/left/right",
                "value": "Number of scroll steps (1-10)",
            },
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
    return _json_result(actions)


# ── Tool: find_element ──


def find_element(
    target: str,
    include_tree: bool = False,
) -> str:
    """Find a UI element by name, automation ID, or position and return its info.

    Searches using multiple strategies (name match, automation ID, position,
    depth-first tree search, HWND fallback, synthetic/OCR tree) and returns
    the first match.

    Args:
        target: Element name ('Submit'), automation ID ('id:myButton'),
                or position ('200,300').
        include_tree: If True, includes children as a mini accessibility tree.

    Returns:
        JSON with element info: name, role, position, size, focused, enabled, value.
        Returns error JSON if element not found.
    """
    provider = _get_provider()
    if not hasattr(provider, "find_element"):
        return _json_result({
            "error": "find_element not available on this provider",
            "provider": type(provider).__name__,
        })

    try:
        info = provider.find_element(target)
        if info is None:
            return _json_result({
                "error": f"Element not found: {target}",
                "target": target,
            })

        # Remove internal control references
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
                                    ci["pos"] = {
                                        "x": round((rect.left + rect.right) / 2),
                                        "y": round((rect.top + rect.bottom) / 2),
                                    }
                            except Exception:
                                pass
                            child_info.append(ci)
                        except Exception:
                            pass
                    if child_info:
                        result["children"] = child_info
            except Exception:
                pass

        return _json_result(result)
    except Exception as e:
        return _json_result({"error": f"Error finding element: {e}"})


# ── Tool: take_screenshot ──


def take_screenshot() -> str:
    """Capture the full screen as a base64-encoded PNG image.

    Uses Pillow (PIL) ImageGrab to capture the entire desktop.
    Returns the screenshot as a data URI that can be embedded in HTML.

    Use this to visually verify the desktop state when the
    accessibility tree is insufficient.

    Returns:
        JSON with screenshot (base64 data URI), width, height, format.
    """
    provider = _get_provider()
    try:
        result = provider.act({"action": "take_screenshot"})
        return _json_result(result)
    except Exception as e:
        return _json_result({"error": str(e)})


# ── Tool: get_focused_element ──


def get_focused_element() -> str:
    """Get structured info about the currently focused UI element.

    Returns name, role, automation_id, position, size, focused/enabled state,
    current value, and supported action patterns.

    Returns an error response if nothing is focused.
    """
    provider = _get_provider()
    if not hasattr(provider, "get_focused_element_info"):
        return _json_result({
            "error": "get_focused_element_info not available",
            "provider": type(provider).__name__,
        })

    try:
        info = provider.get_focused_element_info()
        return _json_result(info)
    except Exception as e:
        return _json_result({"error": str(e)})


# ── Tool: list_windows ──


def list_windows() -> str:
    """List all open top-level windows with their titles, roles, and PIDs.

    Uses UI Automation to enumerate all windows on the desktop.
    Returns JSON array of window info dicts with:
      - title: Window title
      - role: Window role (e.g. 'Window', 'Pane')
      - pid: Process ID
      - visible: Whether the window is visible
      - focused: Whether the window has focus
      - pos: Center position {x, y}
      - size: Size {w, h}

    Windows-only. On other platforms returns an error.
    """
    if not _is_windows():
        return _json_result({
            "error": "list_windows is only available on Windows",
            "platform": sys.platform,
        })

    try:
        from .ax_collector_windows import WindowsCollector

        collector = WindowsCollector(max_interactive_nodes=50)
        windows = collector.list_all_windows()
        return _json_result({"windows": windows, "count": len(windows)})
    except Exception as e:
        return _json_result({"error": str(e), "windows": []})


# ── Tool: get_provider_info ──


def get_provider_info() -> str:
    """Get information about the current CUA provider and its capabilities.

    Returns platform info, provider type, uptime, and whether
    the provider is in stub (simulated) or live mode.

    Useful for diagnostics and to verify that the native tools
    are properly initialized.
    """
    provider = _get_provider()
    return _json_result({
        "platform": sys.platform,
        "is_windows": _is_windows(),
        "provider_type": type(provider).__name__,
        "is_stub": "Stub" in type(provider).__name__,
        "uptime_seconds": round(time.time() - _provider_init_time, 1),
        "available_tools": [
            "get_appstate",
            "get_appshot",
            "execute_action",
            "list_actions",
            "find_element",
            "take_screenshot",
            "get_focused_element",
            "list_windows",
            "get_provider_info",
        ],
        "registration": "native_hermes_toolset",
        "version": "0.2.0",
    })


# ── Tool: reset_provider ──


def reset_provider() -> str:
    """Reset the action provider state (clear caches, reset appshot counter).

    Call this when switching tasks or if the provider gets into
    a bad state. Does NOT restart the provider — just clears
    internal caches and observation history.
    """
    global _provider, _provider_init_time

    provider = _get_provider()
    try:
        # Clear in-provider caches
        if hasattr(provider, "_mark_mutation"):
            provider._mark_mutation()
        if hasattr(provider, "_observe_cache"):
            provider._observe_cache.clear()
        if hasattr(provider, "_find_cache"):
            provider._find_cache.clear()

        # Reset appshot manager
        if hasattr(provider, "_appshot_mgr"):
            provider._appshot_mgr.reset()

        _provider_init_time = time.time()
        return _json_result({
            "success": True,
            "message": "Provider state reset successfully",
        })
    except Exception as e:
        # If reset fails, recreate the provider
        _provider = None
        _provider_init_time = 0.0
        _get_provider()  # Re-initialize
        return _json_result({
            "success": True,
            "message": f"Provider recreated after reset error: {e}",
        })


# ── Registration ──


def register_tools() -> dict[str, Any]:
    """Register all CUA tools with Hermes Agent's tool system.

    Returns a dict mapping tool names to callables, suitable for
    Hermes' tool registration API.

    Usage:
        from cua_agent.hermes_native_tools import register_tools
        tools = register_tools()
        # Pass tools dict to Hermes' tool registry

    The returned dict includes all available tools and metadata
    for Hermes to generate tool schemas automatically.
    """
    tool_definitions = {
        "get_appstate": {
            "callable": get_appstate,
            "description": get_appstate.__doc__.partition("\n\n")[0].strip(),
            "windows_only": False,
        },
        "get_appshot": {
            "callable": get_appshot,
            "description": get_appshot.__doc__.partition("\n\n")[0].strip(),
            "windows_only": False,
        },
        "execute_action": {
            "callable": execute_action,
            "description": execute_action.__doc__.partition("\n\n")[0].strip(),
            "windows_only": False,
        },
        "list_actions": {
            "callable": list_actions,
            "description": list_actions.__doc__.partition("\n\n")[0].strip(),
            "windows_only": False,
        },
        "find_element": {
            "callable": find_element,
            "description": find_element.__doc__.partition("\n\n")[0].strip(),
            "windows_only": False,
        },
        "take_screenshot": {
            "callable": take_screenshot,
            "description": take_screenshot.__doc__.partition("\n\n")[0].strip(),
            "windows_only": False,
        },
        "get_focused_element": {
            "callable": get_focused_element,
            "description": get_focused_element.__doc__.partition("\n\n")[0].strip(),
            "windows_only": False,
        },
        "list_windows": {
            "callable": list_windows,
            "description": list_windows.__doc__.partition("\n\n")[0].strip(),
            "windows_only": True,
        },
        "get_provider_info": {
            "callable": get_provider_info,
            "description": get_provider_info.__doc__.partition("\n\n")[0].strip(),
            "windows_only": False,
        },
        "reset_provider": {
            "callable": reset_provider,
            "description": reset_provider.__doc__.partition("\n\n")[0].strip(),
            "windows_only": False,
        },
    }

    return {
        "toolset_name": "windows_cua",
        "toolset_description": "Windows desktop control via UI Automation accessibility tree. "
                               "Enables text-only LLMs to control Windows applications by "
                               "reading structured state (not screenshots).",
        "windows_only": True,
        "platform": sys.platform,
        "available": True,  # Stub fallback on non-Windows
        "tools": tool_definitions,
    }


# ── Filtering helpers (mirrored from cua_mcp_server) ──


def _filter_interactive_only(node: dict) -> dict:
    """Recursively filter to only interactive elements.

    Removes structural containers (Pane, Group, etc.) that don't
    represent actionable UI elements.
    """
    from .prompt_builder import INTERACTIVE_ROLES

    if not node:
        return node

    role = node.get("role", "")
    children = node.get("children", [])

    # Always keep windows, dialogs, and the root
    is_container = role in frozenset({
        "Window", "AXWindow", "Dialog", "AXDialog", "Sheet", "AXSheet",
        "Application", "AXApplication", "Pane", "AXPane",
        "Group", "AXGroup", "SplitGroup", "AXSplitGroup",
        "ScrollArea", "AXScrollArea", "Toolbar", "AXToolbar",
    })

    filtered_children = []
    for child in children:
        filtered = _filter_interactive_only(child)
        if filtered and (filtered.get("children") or filtered.get("role") in INTERACTIVE_ROLES):
            filtered_children.append(filtered)

    if is_container or role in INTERACTIVE_ROLES or filtered_children:
        result = {
            "role": role,
            "title": node.get("title", ""),
            "value": node.get("value"),
            "focused": node.get("focused", False),
            "enabled": node.get("enabled", True),
        }
        if node.get("pos") and node.get("size"):
            result["pos"] = node["pos"]
            result["size"] = node["size"]
        if node.get("automation_id"):
            result["automation_id"] = node["automation_id"]
        if filtered_children:
            result["children"] = filtered_children
        return result

    return None if not is_container else node


def _strip_window_metadata(node: dict) -> dict:
    """Remove internal metadata keys (prefixed with _) from the tree."""
    if not node:
        return node

    result = {}
    for key, value in node.items():
        if key.startswith("_"):
            continue
        if key == "children":
            result[key] = [_strip_window_metadata(c) for c in (value or [])]
        else:
            result[key] = value

    return result


# ── Diagnostics ──

if __name__ == "__main__":
    """Run diagnostics when executed directly."""
    logging.basicConfig(level=logging.INFO)

    print("=== Hermes Native CUA Tools — Diagnostics ===\n")
    print(f"Platform: {sys.platform}")
    print(f"Is Windows: {_is_windows()}")
    print()

    info = json.loads(get_provider_info())
    print(f"Provider: {info['provider_type']}")
    print(f"Stub mode: {info['is_stub']}")
    print(f"Uptime: {info['uptime_seconds']}s")
    print(f"Tools: {', '.join(info['available_tools'])}")
    print()

    if _is_windows():
        print("Testing get_appstate (filtered)...")
        state = json.loads(get_appstate(filter_interactive_only=True))
        title = state.get("title", "(no title)")
        children = len(state.get("children", []))
        print(f"  Window: {title}")
        print(f"  Children: {children}")
        if "error" in state:
            print(f"  Error: {state['error']}")
    else:
        print("⚠️  Not on Windows — provider is in STUB mode")
        print("   (all actions will be simulated)")
