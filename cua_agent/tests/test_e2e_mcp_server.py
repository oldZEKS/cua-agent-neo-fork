"""
test_e2e_mcp_server.py — End-to-end validation of MCP server tools via public function API.

Uses the public tool functions (execute_action, list_actions, get_element_info,
take_screenshot, get_appstate, get_appshot) through their function interface with a
StubActionProvider set on global state.

This validates the full pipeline end-to-end:
  1. State management (action_history, appshot_history)
  2. Tool dispatch for all action types
  3. JSON serialization/deserialization
  4. Error handling for invalid inputs
  5. No duplicate entries in action definitions
  6. Window state actions (minimize, maximize, close)
  7. All other action types (hover, drag, select, resize, screenshot, etc.)

Run with: python -m cua_agent.tests.test_e2e_mcp_server
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cua_agent.cua_mcp_server import (
    state,
    execute_action,
    get_element_info,
    list_actions,
    take_screenshot,
    get_appstate,
    get_appshot,
    StubActionProvider,
)


def setup():
    """Set up the stub provider in global state before each test section."""
    state.action_provider = StubActionProvider()
    state.action_history.clear()
    state.appshot_history.clear()
    state.current_appstate = None
    state.prev_appstate = None


def teardown():
    """Clear global state after each test section."""
    state.action_provider = None
    state.action_history.clear()
    state.appshot_history.clear()
    state.current_appstate = None
    state.prev_appstate = None


def check_result(raw: str, expected_action: str) -> dict:
    """Parse and validate a basic execute_action response."""
    result = json.loads(raw)
    assert result["success"] is True, f"Expected success, got: {result}"
    assert result["action"] == expected_action, f"Expected action={expected_action}, got: {result}"
    return result


def run_e2e_tests():
    """Run all E2E validation tests."""
    passed = 0
    failed = 0

    # ── Test 1: list_actions has all required types with no duplicates ──
    print("E2E 1: list_actions has all required actions...", end=" ")
    setup()
    try:
        raw = list_actions()
        actions = json.loads(raw)
        names = [a["name"] for a in actions]
        
        required = [
            "click", "double_click", "right_click", "hover", "drag",
            "select", "resize_window", "minimize_window", "maximize_window",
            "close_window", "take_screenshot", "type", "type_into",
            "find_element", "key_press", "scroll", "open_app", "open_url", "wait",
        ]
        for name in required:
            assert name in names, f"Missing required action: {name}"
            # Check no duplicates
            count = sum(1 for n in names if n == name)
            assert count == 1, f"Duplicate entries for '{name}': {count}"
        print(f"OK ({len(actions)} actions, {len(required)} required)")
        passed += 1
    finally:
        teardown()

    # ── Test 2: execute_action — minimize_window ──
    print("E2E 2: execute_action minimize_window...", end=" ")
    setup()
    try:
        raw = execute_action(action_type="minimize_window")
        r = check_result(raw, "minimize_window")
        assert r["success"] is True
        print("OK")
        passed += 1
    finally:
        teardown()

    # ── Test 3: execute_action — maximize_window ──
    print("E2E 3: execute_action maximize_window...", end=" ")
    setup()
    try:
        raw = execute_action(action_type="maximize_window")
        r = check_result(raw, "maximize_window")
        assert r["success"] is True
        print("OK")
        passed += 1
    finally:
        teardown()

    # ── Test 4: execute_action — close_window ──
    print("E2E 4: execute_action close_window...", end=" ")
    setup()
    try:
        raw = execute_action(action_type="close_window")
        r = check_result(raw, "close_window")
        assert r["success"] is True
        print("OK")
        passed += 1
    finally:
        teardown()

    # ── Test 5: execute_action — window actions record history ──
    print("E2E 5: window actions record action_history...", end=" ")
    setup()
    try:
        execute_action(action_type="minimize_window")
        execute_action(action_type="maximize_window")
        execute_action(action_type="close_window")
        assert len(state.action_history) == 3, f"Expected 3 history entries, got {len(state.action_history)}"
        assert state.action_history[0]["action"] == "minimize_window"
        assert state.action_history[1]["action"] == "maximize_window"
        assert state.action_history[2]["action"] == "close_window"
        print("OK (3 entries)")
        passed += 1
    finally:
        teardown()

    # ── Test 6: execute_action — resize_window ──
    print("E2E 6: execute_action resize_window...", end=" ")
    setup()
    try:
        raw = execute_action(action_type="resize_window", value="1024,768")
        r = check_result(raw, "resize_window")
        assert r["width"] == 1024
        assert r["height"] == 768
        print("OK")
        passed += 1
    finally:
        teardown()

    # ── Test 7: execute_action — resize_window invalid format ──
    print("E2E 7: execute_action resize_window invalid...", end=" ")
    setup()
    try:
        raw = execute_action(action_type="resize_window", value="not_a_size")
        result = json.loads(raw)
        assert result["success"] is False
        assert "error" in result
        print("OK (error returned)")
        passed += 1
    finally:
        teardown()

    # ── Test 8: execute_action — hover ──
    print("E2E 8: execute_action hover...", end=" ")
    setup()
    try:
        raw = execute_action(action_type="hover", target="Submit")
        r = check_result(raw, "hover")
        assert r["hovered_element"] == "Submit"
        print("OK")
        passed += 1
    finally:
        teardown()

    # ── Test 9: execute_action — hover by position ──
    print("E2E 9: execute_action hover by position...", end=" ")
    setup()
    try:
        raw = execute_action(action_type="hover", target="300,400")
        r = check_result(raw, "hover")
        assert r["hovered_element"] == "Element at 300,400"
        assert r["info"]["pos"]["x"] == 300
        assert r["info"]["pos"]["y"] == 400
        print("OK")
        passed += 1
    finally:
        teardown()

    # ── Test 10: execute_action — drag by name ──
    print("E2E 10: execute_action drag...", end=" ")
    setup()
    try:
        raw = execute_action(action_type="drag", target="Source", value="Target")
        r = check_result(raw, "drag")
        assert r["source"] == "Source"
        assert r["target"] == "Target"
        assert "source_pos" in r
        assert "target_pos" in r
        print("OK")
        passed += 1
    finally:
        teardown()

    # ── Test 11: execute_action — drag by position ──
    print("E2E 11: execute_action drag by position...", end=" ")
    setup()
    try:
        raw = execute_action(action_type="drag", target="100,200", value="500,600")
        r = check_result(raw, "drag")
        assert r["source_pos"]["x"] == 100
        assert r["source_pos"]["y"] == 200
        assert r["target_pos"]["x"] == 500
        assert r["target_pos"]["y"] == 600
        assert r["steps"] == 10
        print("OK")
        passed += 1
    finally:
        teardown()

    # ── Test 12: execute_action — select ──
    print("E2E 12: execute_action select...", end=" ")
    setup()
    try:
        raw = execute_action(action_type="select", target="Dropdown", value="OptionA")
        r = check_result(raw, "select")
        assert r["value"] == "OptionA"
        assert r["option_name"] == "OptionA"
        print("OK")
        passed += 1
    finally:
        teardown()

    # ── Test 13: get_element_info ──
    print("E2E 13: get_element_info tool...", end=" ")
    setup()
    try:
        raw = get_element_info(target="Submit")
        result = json.loads(raw)
        assert "error" not in result, f"Got error: {result.get('error')}"
        assert result["name"] == "Submit"
        assert result["role"] == "AXButton"
        assert result["pos"]["x"] == 500
        assert result["pos"]["y"] == 400
        assert result["focused"] is False
        assert result["enabled"] is True
        print("OK")
        passed += 1
    finally:
        teardown()

    # ── Test 14: take_screenshot ──
    print("E2E 14: take_screenshot tool...", end=" ")
    setup()
    try:
        raw = take_screenshot()
        result = json.loads(raw)
        assert result["success"] is True
        assert result["action"] == "take_screenshot"
        assert "screenshot" in result
        assert result["screenshot"].startswith("data:image/png;base64,")
        assert result["width"] == 1920
        assert result["height"] == 1080
        print("OK")
        passed += 1
    finally:
        teardown()

    # ── Test 15: get_appstate ──
    print("E2E 15: get_appstate tool...", end=" ")
    setup()
    try:
        raw = get_appstate()
        result = json.loads(raw)
        assert "role" in result or "error" in result
        print("OK")
        passed += 1
    finally:
        teardown()

    # ── Test 16: get_appshot ──
    print("E2E 16: get_appshot tool...", end=" ")
    setup()
    try:
        raw = get_appshot()
        result = json.loads(raw)
        assert "summary" in result or "error" in result
        print("OK")
        passed += 1
    finally:
        teardown()

    # ── Test 17: execute_action — find_element ──
    print("E2E 17: execute_action find_element...", end=" ")
    setup()
    try:
        raw = execute_action(action_type="find_element", target="Submit")
        r = check_result(raw, "find_element")
        assert r["info"]["name"] == "Submit"
        assert r["info"]["role"] == "AXButton"
        print("OK")
        passed += 1
    finally:
        teardown()

    # ── Test 18: execute_action — type_into ──
    print("E2E 18: execute_action type_into...", end=" ")
    setup()
    try:
        raw = execute_action(action_type="type_into", target="[2]", value="hello")
        r = check_result(raw, "type_into")
        assert r["value"] == "hello"
        print("OK")
        passed += 1
    finally:
        teardown()

    # ── Test 19: execute_action — click ──
    print("E2E 19: execute_action click...", end=" ")
    setup()
    try:
        raw = execute_action(action_type="click", target="Submit")
        r = check_result(raw, "click")
        assert r["stub"] is True
        print("OK")
        passed += 1
    finally:
        teardown()

    # ── Test 20: execute_action — unknown action (stub succeeds for all) ──
    print("E2E 20: execute_action unknown action...", end=" ")
    setup()
    try:
        raw = execute_action(action_type="nonexistent_action")
        r = check_result(raw, "nonexistent_action")
        assert r["stub"] is True
        print("OK (stub succeeds for all actions)")
        passed += 1
    finally:
        teardown()

    # ── Test 21: execute_action — take_region_screenshot ──
    print("E2E 21: execute_action take_region_screenshot...", end=" ")
    setup()
    try:
        raw = execute_action(action_type="take_region_screenshot", target="100,200,400,300")
        r = check_result(raw, "take_region_screenshot")
        assert "screenshot" in r
        assert r["screenshot"].startswith("data:image/png;base64,")
        print("OK")
        passed += 1
    finally:
        teardown()

    # ── Test 22: execute_action — shortcut ──
    print("E2E 22: execute_action shortcut...", end=" ")
    setup()
    try:
        raw = execute_action(action_type="shortcut", target="copy")
        r = check_result(raw, "shortcut")
        assert "key_combo" in r
        print("OK")
        passed += 1
    finally:
        teardown()

    # ── Test 23: list_actions includes take_region_screenshot and shortcut ──
    print("E2E 23: list_actions includes region screenshot and shortcut...", end=" ")
    setup()
    try:
        raw = list_actions()
        actions = json.loads(raw)
        names = {a["name"] for a in actions}
        assert "take_region_screenshot" in names
        assert "shortcut" in names
        # No duplicates
        for name in ("take_region_screenshot", "shortcut"):
            count = sum(1 for a in actions if a["name"] == name)
            assert count == 1, f"Duplicate '{name}': {count}"
        print("OK")
        passed += 1
    finally:
        teardown()

    # ── Summary ──
    print(f"\n{'=' * 50}")
    print(f"E2E Validation: {passed} passed, {failed} failed ({passed + failed} total)")
    return failed == 0


if __name__ == "__main__":
    success = run_e2e_tests()
    sys.exit(0 if success else 1)
