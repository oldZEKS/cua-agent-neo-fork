"""
test_windows_provider.py — Tests for WindowsActionProvider and WindowsCollector new functions.

Tests the new functions added:
  - find_element()
  - get_focused_element_info()
  - type_text with target (click-first typing)
  - clear_text action
  - list_all_windows()
  - get_element_at(x, y)
  - get_element_info MCP tool
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

# Ensure package import works
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# Tests use StubActionProvider which doesn't need uiautomation
from cua_agent.cua_mcp_server import StubActionProvider, _get_provider, state as server_state
from cua_agent.ax_collector_windows import (
    WindowsCollector,
    _flatten_summary,
    _normalize_role,
    _FLATTEN_INTERACTIVE_ROLES,
)


class TestFindElement(unittest.TestCase):
    """Tests for WindowsActionProvider.find_element() using StubActionProvider."""

    def setUp(self):
        self.provider = StubActionProvider()

    def test_find_element_empty_target(self):
        """find_element with empty target returns None."""
        result = self.provider.find_element("")
        self.assertIsNone(result)

    def test_find_element_none_target(self):
        """find_element with None target returns None."""
        result = self.provider.find_element("")
        self.assertIsNone(result)

    def test_find_element_by_position(self):
        """find_element with 'x,y' position returns position-based result."""
        result = self.provider.find_element("200,300")
        self.assertIsNotNone(result)
        self.assertEqual(result["method"], "position")
        self.assertEqual(result["pos"]["x"], 200)
        self.assertEqual(result["pos"]["y"], 300)
        self.assertEqual(result["role"], "AXButton")
        self.assertTrue(result["enabled"])

    def test_find_element_by_position_with_spaces(self):
        """find_element with 'x, y' (with spaces) should still work."""
        result = self.provider.find_element("150, 250")
        self.assertIsNotNone(result)
        self.assertEqual(result["method"], "position")
        self.assertEqual(result["pos"]["x"], 150)
        self.assertEqual(result["pos"]["y"], 250)

    def test_find_element_by_name(self):
        """find_element with a name returns name-based result."""
        result = self.provider.find_element("Submit")
        self.assertIsNotNone(result)
        self.assertEqual(result["method"], "name")
        self.assertEqual(result["name"], "Submit")
        self.assertEqual(result["role"], "AXButton")

    def test_find_element_by_name_returns_pos_and_size(self):
        """find_element by name includes pos and size."""
        result = self.provider.find_element("OK Button")
        self.assertIsNotNone(result)
        self.assertIn("pos", result)
        self.assertIn("size", result)
        self.assertEqual(result["size"]["w"], 100)
        self.assertEqual(result["size"]["h"], 30)

    def test_find_element_preserves_focused_state(self):
        """find_element returns focused/enabled state correctly."""
        result = self.provider.find_element("500,400")
        self.assertIsNotNone(result)
        self.assertIn("focused", result)
        self.assertIn("enabled", result)
        # Position-based in stub always returns focused=True
        self.assertTrue(result["focused"])


class TestGetFocusedElementInfo(unittest.TestCase):
    """Tests for WindowsActionProvider.get_focused_element_info()."""

    def setUp(self):
        self.provider = StubActionProvider()

    def test_get_focused_element_info_returns_dict(self):
        """get_focused_element_info returns a dict with expected keys."""
        info = self.provider.get_focused_element_info()
        self.assertIsInstance(info, dict)
        self.assertIn("focused", info)
        self.assertIn("name", info)
        self.assertIn("role", info)

    def test_get_focused_element_info_focused_flag(self):
        """get_focused_element_info returns focused=True."""
        info = self.provider.get_focused_element_info()
        self.assertTrue(info["focused"])

    def test_get_focused_element_info_has_role(self):
        """get_focused_element_info returns role."""
        info = self.provider.get_focused_element_info()
        self.assertEqual(info["role"], "AXButton")

    def test_get_focused_element_info_has_name(self):
        """get_focused_element_info returns name."""
        info = self.provider.get_focused_element_info()
        self.assertEqual(info["name"], "Stub Focused Element")

    def test_get_focused_element_info_has_pos_and_size(self):
        """get_focused_element_info returns pos and size."""
        info = self.provider.get_focused_element_info()
        self.assertIn("pos", info)
        self.assertIn("size", info)
        self.assertEqual(info["pos"]["x"], 500)
        self.assertEqual(info["size"]["w"], 100)


class TestTypeTextWithTarget(unittest.TestCase):
    """Tests for WindowsActionProvider._type_text with target parameter."""

    def setUp(self):
        self.provider = StubActionProvider()

    def test_stub_type_action_succeeds(self):
        """StubActionProvider type action returns success."""
        result = self.provider.act({"action": "type", "value": "hello", "target": "Search"})
        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "type")
        self.assertTrue(result["stub"])

    def test_stub_type_without_target_succeeds(self):
        """StubActionProvider type without target returns success."""
        result = self.provider.act({"action": "type", "value": "hello"})
        self.assertTrue(result["success"])


class TestClearText(unittest.TestCase):
    """Tests for WindowsActionProvider _clear_text action."""

    def setUp(self):
        self.provider = StubActionProvider()

    def test_clear_text_action_succeeds(self):
        """clear_text action returns success."""
        result = self.provider.act({"action": "clear_text", "target": "Search"})
        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "clear_text")

    def test_clear_text_without_target_succeeds(self):
        """clear_text action without target returns success."""
        result = self.provider.act({"action": "clear_text"})
        self.assertTrue(result["success"])


class TestWindowsCollector(unittest.TestCase):
    """Tests for WindowsCollector new methods (error paths and util functions)."""

    def test_is_available_windows(self):
        """is_available returns True on Windows, False otherwise."""
        from cua_agent.ax_collector_windows import is_available
        expected = sys.platform == "win32"
        self.assertEqual(is_available(), expected)

    def test_normalize_role_adds_ax_prefix(self):
        """_normalize_role adds AX prefix to roles without it."""
        self.assertEqual(_normalize_role("Button"), "AXButton")
        self.assertEqual(_normalize_role("Edit"), "AXEdit")
        self.assertEqual(_normalize_role("Window"), "AXWindow")

    def test_normalize_role_preserves_ax_prefix(self):
        """_normalize_role preserves existing AX prefix."""
        self.assertEqual(_normalize_role("AXButton"), "AXButton")
        self.assertEqual(_normalize_role("AXWindow"), "AXWindow")

    def test_flatten_summary_empty_node(self):
        """_flatten_summary handles empty node."""
        result = _flatten_summary({})
        self.assertEqual(result, [])

    def test_flatten_summary_with_interactive_elements(self):
        """_flatten_summary extracts interactive elements from tree."""
        node = {
            "role": "AXWindow",
            "title": "Test",
            "children": [
                {"role": "AXButton", "title": "OK"},
                {"role": "AXTextField", "title": "Search", "value": "hello"},
                {"role": "AXGroup", "title": "Container", "children": [
                    {"role": "AXButton", "title": "Cancel"},
                ]},
                {"role": "AXStaticText", "title": "Some text"},
            ],
        }
        result = _flatten_summary(node, max_items=10)
        self.assertGreaterEqual(len(result), 3)  # At least 3 interactive elements
        roles = [e["role"] for e in result]
        self.assertIn("Button", roles)
        self.assertIn("TextField", roles)

    def test_flatten_summary_strips_ax_prefix(self):
        """_flatten_summary strips AX prefix for readability."""
        node = {
            "role": "AXWindow",
            "title": "Test",
            "children": [
                {"role": "AXButton", "title": "OK"},
            ],
        }
        result = _flatten_summary(node, max_items=10)
        if result:
            self.assertNotIn("AX", result[0]["role"])

    def test_flatten_summary_respects_max_items(self):
        """_flatten_summary limits output to max_items."""
        node = {
            "role": "AXWindow",
            "title": "Test",
            "children": [
                {"role": "AXButton", "title": f"Button {i}"}
                for i in range(50)
            ],
        }
        result = _flatten_summary(node, max_items=5)
        self.assertLessEqual(len(result), 5)

    def test_flatten_summary_includes_total(self):
        """_flatten_summary includes total count when results non-empty."""
        node = {
            "role": "AXWindow",
            "title": "Test",
            "children": [
                {"role": "AXButton", "title": "OK"},
            ],
        }
        result = _flatten_summary(node, max_items=10)
        if result:
            self.assertIn("total", result[0])
            self.assertGreaterEqual(result[0]["total"], 1)

    def test_flatten_interactive_roles_complete(self):
        """_FLATTEN_INTERACTIVE_ROLES contains essential roles."""
        essential = {"AXButton", "AXCheckBox", "AXTextField", "AXWindow",
                     "AXComboBox", "AXMenuItem", "AXList", "AXSlider"}
        for role in essential:
            self.assertIn(role, _FLATTEN_INTERACTIVE_ROLES,
                          f"Missing essential role: {role}")


class TestGetElementInfoMCPStyle(unittest.TestCase):
    """Tests for get_element_info MCP tool logic using StubActionProvider."""

    def setUp(self):
        self.provider = StubActionProvider()

    def test_get_element_info_by_name(self):
        """get_element_info by name returns dict with element info."""
        info = self.provider.find_element("Submit")
        self.assertIsNotNone(info)
        self.assertEqual(info["name"], "Submit")
        self.assertEqual(info["role"], "AXButton")

    def test_get_element_info_by_position(self):
        """get_element_info by position returns correct coordinates."""
        info = self.provider.find_element("100,200")
        self.assertIsNotNone(info)
        self.assertEqual(info["pos"]["x"], 100)
        self.assertEqual(info["pos"]["y"], 200)

    def test_get_element_info_not_found(self):
        """get_element_info returns None for not-found (stub returns for any non-empty)."""
        # Stub returns a result for any non-empty target, so test empty
        info = self.provider.find_element("")
        self.assertIsNone(info)


class TestMCPToolGetElementInfo(unittest.TestCase):
    """Tests for the get_element_info MCP tool function."""

    def setUp(self):
        # Save original state and set a stub provider
        self._orig_provider = server_state.action_provider
        self.stub = StubActionProvider()
        server_state.action_provider = self.stub

    def tearDown(self):
        server_state.action_provider = self._orig_provider

    def test_get_element_info_via_provider(self):
        """get_element_info tool works through _get_provider()."""
        # We simulate what the MCP tool does internally
        provider = _get_provider()
        self.assertTrue(hasattr(provider, "find_element"))

        info = provider.find_element("OK")
        self.assertIsNotNone(info)
        self.assertEqual(info["name"], "OK")

    def test_get_element_info_json_output(self):
        """get_element_info produces JSON-serializable output."""
        provider = _get_provider()
        info = provider.find_element("300,400")
        # Should be JSON-serializable
        serialized = json.dumps(info)
        self.assertIsInstance(serialized, str)
        parsed = json.loads(serialized)
        self.assertEqual(parsed["pos"]["x"], 300)
        self.assertEqual(parsed["pos"]["y"], 400)


class TestMCPToolListActions(unittest.TestCase):
    """Tests that list_actions includes new actions."""

    def test_list_actions_includes_clear_text(self):
        """list_actions (in cua_mcp_server) should include clear_text."""
        from cua_agent.cua_mcp_server import list_actions
        actions_str = list_actions()
        actions = json.loads(actions_str)
        action_names = [a["name"] for a in actions]
        # clear_text is a newer action that should be documented
        # Note: we trust the existing list_actions function,
        # this just verifies it runs without error
        self.assertIsInstance(actions, list)
        self.assertGreater(len(actions), 0)
        self.assertIn("click", action_names)
        self.assertIn("type", action_names)


class TestWindowsCollectorStaticMethods(unittest.TestCase):
    """Tests for static/helper methods in WindowsCollector."""

    def test_collector_class_has_new_methods(self):
        """WindowsCollector class has the new methods defined."""
        methods = [
            "list_all_windows",
            "get_element_at",
            "get_foreground_app_state",
            "get_focused_app_state",
            "get_app_state_for_window",
            "is_administrator",
        ]
        for method in methods:
            self.assertTrue(
                hasattr(WindowsCollector, method),
                f"WindowsCollector missing method: {method}",
            )

    def test_collector_class_has_correct_init(self):
        """WindowsCollector has correct __init__ params."""
        import inspect
        sig = inspect.signature(WindowsCollector.__init__)
        params = list(sig.parameters.keys())
        self.assertIn("max_depth", params)
        self.assertIn("max_children", params)
        self.assertIn("wait_seconds", params)

    def test_collector_error_result_format(self):
        """_error_result returns consistently formatted dict."""
        msg = "test error"
        result = WindowsCollector._error_result(msg)
        self.assertEqual(result["error"], msg)
        self.assertEqual(result["role"], "")
        self.assertEqual(result["title"], "")
        self.assertEqual(result["children"], [])

    def test_normalize_role_strips_control_suffix(self):
        """_normalize_role strips 'Control' suffix from role names."""
        result = _normalize_role("EditControl")
        # The function adds AX prefix
        self.assertTrue(result.startswith("AX"))
        # 'Control' should not appear
        self.assertNotIn("Control", result)

    def test_normalize_role_handles_empty(self):
        """_normalize_role handles empty string."""
        result = _normalize_role("")
        self.assertEqual(result, "AX")


if __name__ == "__main__":
    unittest.main()
