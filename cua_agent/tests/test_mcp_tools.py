"""
Tests for MCP tool functions — exercises execute_action, get_element_info,
and list_actions through the public function interface.

Uses StubActionProvider set on the global state so no real UI is needed.
All state is reset before and after each test.

Run with: python -m cua_agent.tests.test_mcp_tools
"""

from __future__ import annotations

import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cua_agent.cua_mcp_server import (
    state,
    execute_action,
    get_element_info,
    list_actions,
    StubActionProvider,
)


def setup():
    """Set up the stub provider in global state before each test."""
    state.action_provider = StubActionProvider()
    state.action_history.clear()
    state.appshot_history.clear()
    state.current_appstate = None
    state.prev_appstate = None


def teardown():
    """Clear global state after each test."""
    state.action_provider = None
    state.action_history.clear()
    state.appshot_history.clear()
    state.current_appstate = None
    state.prev_appstate = None


def test_execute_hover_by_element_name():
    """execute_action with 'hover' should return info for a named element."""
    setup()
    try:
        raw = execute_action(action_type="hover", target="Submit")
        result = json.loads(raw)
        assert result["success"] is True
        assert result["action"] == "hover"
        assert result["target"] == "Submit"
        assert result["hovered_element"] == "Submit"
        assert "info" in result
        assert result["info"]["name"] == "Submit"
        assert result["info"]["role"] == "AXButton"
        assert result["info"]["pos"]["x"] == 500
        assert result["info"]["pos"]["y"] == 400
        print("  PASS: test_execute_hover_by_element_name")
    finally:
        teardown()


def test_execute_hover_by_position():
    """execute_action with 'hover' should work with an x,y position target."""
    setup()
    try:
        raw = execute_action(action_type="hover", target="300,400")
        result = json.loads(raw)
        assert result["success"] is True
        assert result["action"] == "hover"
        assert result["target"] == "300,400"
        assert result["hovered_element"] == "Element at 300,400"
        assert result["info"]["pos"]["x"] == 300
        assert result["info"]["pos"]["y"] == 400
        print("  PASS: test_execute_hover_by_position")
    finally:
        teardown()


def test_execute_hover_not_found():
    """execute_action with 'hover' on empty target should return error."""
    setup()
    try:
        raw = execute_action(action_type="hover", target="")
        result = json.loads(raw)
        assert result["success"] is False
        assert result["action"] == "hover"
        assert "not found" in result.get("error", "").lower()
        print("  PASS: test_execute_hover_not_found")
    finally:
        teardown()


def test_execute_hover_nonexistent_element():
    """execute_action with 'hover' on a non-existent element name."""
    setup()
    try:
        raw = execute_action(action_type="hover", target="NonExistentElementXYZ")
        # find_element returns a stub result for any non-empty name
        # (the stub always returns a match for non-empty, non-position strings)
        result = json.loads(raw)
        assert result["success"] is True
        assert result["hovered_element"] == "NonExistentElementXYZ"
        print("  PASS: test_execute_hover_nonexistent_element")
    finally:
        teardown()


def test_execute_hover_records_action_history():
    """execute_action with hover should append to action_history."""
    setup()
    try:
        initial_len = len(state.action_history)
        execute_action(action_type="hover", target="Submit")
        assert len(state.action_history) == initial_len + 1
        last = state.action_history[-1]
        assert last["action"] == "hover"
        assert last["target"] == "Submit"
        assert last["success"] is True
        print("  PASS: test_execute_hover_records_action_history")
    finally:
        teardown()


def test_execute_hover_with_value_param():
    """execute_action with hover should ignore extra value param gracefully."""
    setup()
    try:
        raw = execute_action(action_type="hover", target="Submit", value="unused")
        result = json.loads(raw)
        assert result["success"] is True
        assert result["target"] == "Submit"
        # value is not used by hover, but shouldn't cause errors
        print("  PASS: test_execute_hover_with_value_param")
    finally:
        teardown()


def test_get_element_info_by_name():
    """get_element_info MCP tool should return element details by name."""
    setup()
    try:
        raw = get_element_info(target="Submit")
        result = json.loads(raw)
        assert "error" not in result
        assert result["name"] == "Submit"
        assert result["role"] == "AXButton"
        assert result["pos"]["x"] == 500
        assert result["pos"]["y"] == 400
        print("  PASS: test_get_element_info_by_name")
    finally:
        teardown()


def test_get_element_info_by_position():
    """get_element_info MCP tool should work with position target."""
    setup()
    try:
        raw = get_element_info(target="300,400")
        result = json.loads(raw)
        assert "error" not in result
        assert result["name"] == "Element at 300,400"
        assert result["pos"]["x"] == 300
        assert result["pos"]["y"] == 400
        print("  PASS: test_get_element_info_by_position")
    finally:
        teardown()


def test_get_element_info_not_found():
    """get_element_info should return error for empty target."""
    setup()
    try:
        raw = get_element_info(target="")
        result = json.loads(raw)
        assert "error" in result
        assert "not found" in result["error"].lower()
        print("  PASS: test_get_element_info_not_found")
    finally:
        teardown()


def test_list_actions_includes_hover():
    """list_actions should include a 'hover' entry."""
    setup()
    try:
        raw = list_actions()
        actions = json.loads(raw)
        hover_entries = [a for a in actions if a["name"] == "hover"]
        assert len(hover_entries) == 1, f"Expected 1 hover entry, got {len(hover_entries)}"
        hover = hover_entries[0]
        assert "tooltip" in hover["description"].lower() or "hover" in hover["description"].lower()
        assert "target" in hover["params"]
        print("  PASS: test_list_actions_includes_hover")
    finally:
        teardown()


def test_execute_select_by_name():
    """execute_action with 'select' should select an option from a combobox."""
    setup()
    try:
        raw = execute_action(action_type="select", target="Dropdown", value="OptionA")
        result = json.loads(raw)
        assert result["success"] is True
        assert result["action"] == "select"
        assert result["target"] == "Dropdown"
        assert result["value"] == "OptionA"
        assert "option_name" in result
        assert "method" in result
        print("  PASS: test_execute_select_by_name")
    finally:
        teardown()


def test_execute_select_no_value():
    """execute_action with 'select' without value should return error."""
    setup()
    try:
        raw = execute_action(action_type="select", target="Dropdown")
        result = json.loads(raw)
        assert result["success"] is False
        assert "value" in result.get("error", "") or "value" in result.get("error", "") or "No value" in str(result)
        print("  PASS: test_execute_select_no_value")
    finally:
        teardown()


def test_execute_resize_window():
    """execute_action with 'resize_window' should resize the foreground window."""
    setup()
    try:
        raw = execute_action(action_type="resize_window", value="1024,768")
        result = json.loads(raw)
        assert result["success"] is True
        assert result["action"] == "resize_window"
        assert result["width"] == 1024
        assert result["height"] == 768
        print("  PASS: test_execute_resize_window")
    finally:
        teardown()


def test_execute_resize_window_invalid_format():
    """execute_action with 'resize_window' with invalid format should return error."""
    setup()
    try:
        raw = execute_action(action_type="resize_window", value="invalid")
        result = json.loads(raw)
        assert result["success"] is False
        assert "error" in result
        print("  PASS: test_execute_resize_window_invalid_format")
    finally:
        teardown()


def test_list_actions_includes_select_and_resize():
    """list_actions should include 'select' and 'resize_window' entries."""
    setup()
    try:
        raw = list_actions()
        actions = json.loads(raw)
        select_entries = [a for a in actions if a["name"] == "select"]
        assert len(select_entries) == 1, f"Expected 1 select entry, got {len(select_entries)}"
        resize_entries = [a for a in actions if a["name"] == "resize_window"]
        assert len(resize_entries) == 1, f"Expected 1 resize_window entry, got {len(resize_entries)}"
        print("  PASS: test_list_actions_includes_select_and_resize")
    finally:
        teardown()


def test_execute_minimize_window():
    """execute_action with 'minimize_window' should minimize the foreground window."""
    setup()
    try:
        raw = execute_action(action_type="minimize_window")
        result = json.loads(raw)
        assert result["success"] is True
        assert result["action"] == "minimize_window"
        print("  PASS: test_execute_minimize_window")
    finally:
        teardown()


def test_execute_maximize_window():
    """execute_action with 'maximize_window' should maximize the foreground window."""
    setup()
    try:
        raw = execute_action(action_type="maximize_window")
        result = json.loads(raw)
        assert result["success"] is True
        assert result["action"] == "maximize_window"
        print("  PASS: test_execute_maximize_window")
    finally:
        teardown()


def test_execute_close_window():
    """execute_action with 'close_window' should close the foreground window."""
    setup()
    try:
        raw = execute_action(action_type="close_window")
        result = json.loads(raw)
        assert result["success"] is True
        assert result["action"] == "close_window"
        print("  PASS: test_execute_close_window")
    finally:
        teardown()


def test_list_actions_includes_window_actions():
    """list_actions should include minimize_window, maximize_window, and close_window entries."""
    setup()
    try:
        raw = list_actions()
        actions = json.loads(raw)
        names = {a["name"] for a in actions}
        assert "minimize_window" in names, "minimize_window not in list_actions"
        assert "maximize_window" in names, "maximize_window not in list_actions"
        assert "close_window" in names, "close_window not in list_actions"
        # Verify no duplicates
        for name in ("minimize_window", "maximize_window", "close_window"):
            entries = [a for a in actions if a["name"] == name]
            assert len(entries) == 1, f"Expected 1 '{name}' entry, got {len(entries)}"
        print("  PASS: test_list_actions_includes_window_actions")
    finally:
        teardown()


def test_execute_drag_by_element_name():
    """execute_action with 'drag' should drag from source to target by name."""
    setup()
    try:
        raw = execute_action(action_type="drag", target="Source", value="Target")
        result = json.loads(raw)
        assert result["success"] is True
        assert result["action"] == "drag"
        assert result["source"] == "Source"
        assert result["target"] == "Target"
        assert "source_pos" in result
        assert "target_pos" in result
        assert "steps" in result
        print("  PASS: test_execute_drag_by_element_name")
    finally:
        teardown()


def test_execute_drag_by_position():
    """execute_action with 'drag' should work with position targets."""
    setup()
    try:
        raw = execute_action(action_type="drag", target="100,200", value="500,600")
        result = json.loads(raw)
        assert result["success"] is True
        assert result["action"] == "drag"
        assert result["source_pos"]["x"] == 100
        assert result["source_pos"]["y"] == 200
        assert result["target_pos"]["x"] == 500
        assert result["target_pos"]["y"] == 600
        assert result["steps"] == 10
        print("  PASS: test_execute_drag_by_position")
    finally:
        teardown()


def test_execute_drag_source_not_found():
    """execute_action with 'drag' on empty source should return error."""
    setup()
    try:
        raw = execute_action(action_type="drag", target="", value="500,600")
        result = json.loads(raw)
        assert result["success"] is False
        assert "not found" in result.get("error", "").lower()
        print("  PASS: test_execute_drag_source_not_found")
    finally:
        teardown()


def test_execute_take_screenshot():
    """execute_action with 'take_screenshot' should capture the screen."""
    setup()
    try:
        raw = execute_action(action_type="take_screenshot")
        result = json.loads(raw)
        assert result["success"] is True
        assert result["action"] == "take_screenshot"
        assert "screenshot" in result
        assert result["screenshot"].startswith("data:image/png;base64,")
        assert result["width"] == 1920
        assert result["height"] == 1080
        print("  PASS: test_execute_take_screenshot")
    finally:
        teardown()


def test_list_actions_includes_take_screenshot():
    """list_actions should include a 'take_screenshot' entry."""
    setup()
    try:
        raw = list_actions()
        actions = json.loads(raw)
        ss_entries = [a for a in actions if a["name"] == "take_screenshot"]
        assert len(ss_entries) == 1, f"Expected 1 take_screenshot entry, got {len(ss_entries)}"
        print("  PASS: test_list_actions_includes_take_screenshot")
    finally:
        teardown()


def test_execute_take_region_screenshot():
    """execute_action with 'take_region_screenshot' should capture a region."""
    setup()
    try:
        raw = execute_action(action_type="take_region_screenshot", target="100,200,400,300")
        result = json.loads(raw)
        assert result["success"] is True
        assert result["action"] == "take_region_screenshot"
        assert "screenshot" in result
        assert result["screenshot"].startswith("data:image/png;base64,")
        assert result["width"] == 800
        assert result["height"] == 600
        assert "region" in result
        print("  PASS: test_execute_take_region_screenshot")
    finally:
        teardown()


def test_execute_shortcut_by_name():
    """execute_action with 'shortcut' should trigger a named shortcut."""
    setup()
    try:
        raw = execute_action(action_type="shortcut", target="copy")
        result = json.loads(raw)
        assert result["success"] is True
        assert result["action"] == "shortcut"
        assert result["target"] == "copy"
        assert "key_combo" in result
        assert "resolved_to" in result
        assert "{Ctrl}c" in result["key_combo"] or "stub" in result["resolved_to"]
        print("  PASS: test_execute_shortcut_by_name")
    finally:
        teardown()


def test_execute_shortcut_raw_combo():
    """execute_action with 'shortcut' should pass through raw key combos."""
    setup()
    try:
        raw = execute_action(action_type="shortcut", target="ctrl+shift+s")
        result = json.loads(raw)
        assert result["success"] is True
        assert result["action"] == "shortcut"
        assert "key_combo" in result
        print("  PASS: test_execute_shortcut_raw_combo")
    finally:
        teardown()


def test_list_actions_includes_region_and_shortcut():
    """list_actions should include 'take_region_screenshot' and 'shortcut' entries."""
    setup()
    try:
        raw = list_actions()
        actions = json.loads(raw)
        names = {a["name"] for a in actions}
        assert "take_region_screenshot" in names, "take_region_screenshot not in list_actions"
        assert "shortcut" in names, "shortcut not in list_actions"
        for name in ("take_region_screenshot", "shortcut"):
            entries = [a for a in actions if a["name"] == name]
            assert len(entries) == 1, f"Expected 1 '{name}' entry, got {len(entries)}"
        print("  PASS: test_list_actions_includes_region_and_shortcut")
    finally:
        teardown()


def test_execute_hover_multiple_times():
    """execute_action with hover should work on consecutive calls."""
    setup()
    try:
        raw1 = execute_action(action_type="hover", target="Button1")
        r1 = json.loads(raw1)
        assert r1["success"] is True
        assert r1["hovered_element"] == "Button1"

        raw2 = execute_action(action_type="hover", target="200,300")
        r2 = json.loads(raw2)
        assert r2["success"] is True
        assert r2["hovered_element"] == "Element at 200,300"

        assert len(state.action_history) == 2
        print("  PASS: test_execute_hover_multiple_times")
    finally:
        teardown()


if __name__ == "__main__":
    print("Running MCP tool tests...\n")

    test_execute_hover_by_element_name()
    test_execute_hover_by_position()
    test_execute_hover_not_found()
    test_execute_hover_nonexistent_element()
    test_execute_hover_records_action_history()
    test_execute_hover_with_value_param()
    test_get_element_info_by_name()
    test_get_element_info_by_position()
    test_get_element_info_not_found()
    test_list_actions_includes_hover()
    test_execute_drag_by_element_name()
    test_execute_drag_by_position()
    test_execute_drag_source_not_found()
    test_execute_take_screenshot()
    test_list_actions_includes_take_screenshot()
    test_execute_take_region_screenshot()
    test_execute_shortcut_by_name()
    test_execute_shortcut_raw_combo()
    test_list_actions_includes_region_and_shortcut()
    test_execute_hover_multiple_times()
    test_execute_select_by_name()
    test_execute_select_no_value()
    test_execute_resize_window()
    test_execute_resize_window_invalid_format()
    test_list_actions_includes_select_and_resize()
    test_execute_minimize_window()
    test_execute_maximize_window()
    test_execute_close_window()
    test_list_actions_includes_window_actions()

    print(f"\n{'=' * 50}")
    print("All 29 MCP tool tests passed!")
