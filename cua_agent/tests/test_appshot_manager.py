"""
Tests for the appshot manager module.

Run with: python -m cua_agent.tests.test_appshot_manager
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cua_agent.appshot_manager import AppshotManager


def test_initial_snapshot():
    """First snapshot should be type 'initial'."""
    mgr = AppshotManager()
    shot = mgr.take_snapshot({"role": "AXWindow", "title": "Test", "children": []})
    assert shot["id"] == 1
    assert shot["type"] == "initial"
    assert shot["important"] is True
    assert "summary" in shot
    print("  PASS: test_initial_snapshot")


def test_second_snapshot_no_change():
    """Second identical snapshot should be 'no_change'."""
    mgr = AppshotManager()
    state = {"role": "AXWindow", "title": "Test", "children": []}
    mgr.take_snapshot(state)
    shot = mgr.take_snapshot(state)
    assert shot["type"] == "no_change"
    assert shot["important"] is False
    print("  PASS: test_second_snapshot_no_change")


def test_snapshot_detects_added():
    """Appshot should detect added elements."""
    mgr = AppshotManager()
    mgr.take_snapshot({
        "role": "AXWindow", "title": "Test",
        "children": [{"role": "AXButton", "title": "OK"}],
    })
    shot = mgr.take_snapshot({
        "role": "AXWindow", "title": "Test",
        "children": [
            {"role": "AXButton", "title": "OK"},
            {"role": "AXTextField", "title": "Name"},
        ],
    })
    assert shot["type"] == "state_change"
    assert shot["diff"]["added_count"] > 0
    print("  PASS: test_snapshot_detects_added")


def test_snapshot_detects_removed():
    """Appshot should detect removed elements."""
    mgr = AppshotManager()
    mgr.take_snapshot({
        "role": "AXWindow", "title": "Test",
        "children": [
            {"role": "AXButton", "title": "OK"},
            {"role": "AXTextField", "title": "Name"},
        ],
    })
    shot = mgr.take_snapshot({
        "role": "AXWindow", "title": "Test",
        "children": [{"role": "AXButton", "title": "OK"}],
    })
    assert shot["type"] == "state_change"
    assert shot["diff"]["removed_count"] > 0
    print("  PASS: test_snapshot_detects_removed")


def test_snapshot_detects_value_change():
    """Appshot should detect value changes on the same element."""
    mgr = AppshotManager()
    mgr.take_snapshot({
        "role": "AXWindow", "title": "Form",
        "children": [{"role": "AXTextField", "title": "Name", "value": ""}],
    })
    shot = mgr.take_snapshot({
        "role": "AXWindow", "title": "Form",
        "children": [{"role": "AXTextField", "title": "Name", "value": "John"}],
    })
    assert shot["type"] == "state_change"
    assert len(shot["diff"]["changed_elements"]) > 0
    print("  PASS: test_snapshot_detects_value_change")


def test_increments_id():
    """Appshot IDs should increment."""
    mgr = AppshotManager()
    s1 = mgr.take_snapshot({"role": "AXWindow", "title": "T"})
    s2 = mgr.take_snapshot({"role": "AXWindow", "title": "T"})
    assert s2["id"] == s1["id"] + 1
    print("  PASS: test_increments_id")


def test_reset():
    """reset() should clear the state and restart IDs."""
    mgr = AppshotManager()
    mgr.take_snapshot({"role": "AXWindow", "title": "T"})
    mgr.reset()
    s = mgr.take_snapshot({"role": "AXWindow", "title": "T"})
    assert s["id"] == 1
    assert s["type"] == "initial"
    print("  PASS: test_reset")


def test_summary_initial():
    """Summary should include app name for initial state."""
    mgr = AppshotManager()
    shot = mgr.take_snapshot({
        "role": "AXWindow", "title": "Notepad",
        "children": [{"role": "AXEdit"}],
    })
    assert "Notepad" in shot["summary"]
    assert "initial" in shot["summary"]
    print("  PASS: test_summary_initial")


def test_summary_state_change():
    """Summary should include +N for added elements."""
    mgr = AppshotManager()
    mgr.take_snapshot({"role": "AXWindow", "title": "Test", "children": []})
    shot = mgr.take_snapshot({
        "role": "AXWindow", "title": "Test",
        "children": [{"role": "AXButton", "title": "OK"}],
    })
    assert "+1" in shot["summary"]
    print("  PASS: test_summary_state_change")


def test_large_tree():
    """Should handle large trees without crash."""
    mgr = AppshotManager()
    large_state = {
        "role": "AXWindow", "title": "Large",
        "children": [
            {"role": "AXGroup", "title": f"Group {i}",
             "children": [
                 {"role": "AXButton", "title": f"Btn {j}"}
                 for j in range(5)
             ]}
            for i in range(20)
        ],
    }
    shot = mgr.take_snapshot(large_state)
    assert shot["id"] == 1
    assert shot["type"] == "initial"
    print("  PASS: test_large_tree")


if __name__ == "__main__":
    print("Running appshot manager tests...\n")

    test_initial_snapshot()
    test_second_snapshot_no_change()
    test_snapshot_detects_added()
    test_snapshot_detects_removed()
    test_snapshot_detects_value_change()
    test_increments_id()
    test_reset()
    test_summary_initial()
    test_summary_state_change()
    test_large_tree()

    print(f"\n{'=' * 50}")
    print("✅ All 10 appshot manager tests passed!")
