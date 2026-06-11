"""
Tests for the prompt builder module (PromptBuilder, compress_appstate_tree, etc.).

Run with: python -m cua_agent.tests.test_prompt_builder
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cua_agent.prompt_builder import (
    PromptBuilder,
    PromptConfig,
    INTERACTIVE_ROLES,
    estimate_tokens,
    estimate_dict_tokens,
    compress_appstate_tree,
    tree_element_count,
    flatten_to_summary,
    create_prompt_builder,
)


def test_estimate_tokens():
    """estimate_tokens should return positive ints."""
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello world") > 0
    assert estimate_tokens("a" * 100) > estimate_tokens("a" * 10)
    print("  PASS: test_estimate_tokens")


def test_estimate_dict_tokens():
    """estimate_dict_tokens should work with dicts."""
    d = {"role": "AXButton", "title": "Submit"}
    tokens = estimate_dict_tokens(d)
    assert tokens > 0
    assert isinstance(tokens, int)
    print("  PASS: test_estimate_dict_tokens")


def test_interactive_roles():
    """INTERACTIVE_ROLES should contain common UI roles."""
    assert "AXButton" in INTERACTIVE_ROLES
    assert "AXTextField" in INTERACTIVE_ROLES
    assert "AXCheckBox" in INTERACTIVE_ROLES
    assert "AXWindow" in INTERACTIVE_ROLES
    assert "Button" in INTERACTIVE_ROLES  # Without AX prefix
    print("  PASS: test_interactive_roles")


def test_tree_element_count():
    """tree_element_count should count all nodes."""
    tree = {
        "role": "AXWindow",
        "children": [
            {"role": "AXButton"},
            {"role": "AXGroup", "children": [
                {"role": "AXButton"},
                {"role": "AXTextField"},
            ]},
        ],
    }
    assert tree_element_count(tree) == 5
    print("  PASS: test_tree_element_count")


def test_compress_tree_basic():
    """compress_appstate_tree should preserve structure at level 0."""
    tree = {
        "role": "AXWindow",
        "title": "Test",
        "pos": {"x": 0, "y": 0},
        "children": [
            {"role": "AXButton", "title": "OK"},
        ],
    }
    compressed = compress_appstate_tree(tree, truncation_level=0)
    assert compressed is not None
    assert compressed["role"] == "AXWindow"
    assert "children" in compressed
    print("  PASS: test_compress_tree_basic")


def test_compress_tree_strips_positions():
    """compress_appstate_tree should strip positions at level 1+."""
    tree = {
        "role": "AXWindow",
        "title": "Test",
        "pos": {"x": 100, "y": 200},
    }
    compressed = compress_appstate_tree(tree, truncation_level=1, strip_positions=True)
    assert "pos" not in compressed
    print("  PASS: test_compress_tree_strips_positions")


def test_compress_tree_truncates_depth():
    """compress_appstate_tree should truncate depth."""
    tree = {
        "role": "AXWindow",
        "children": [
            {"role": "AXGroup", "children": [
                {"role": "AXButton"},
                {"role": "AXGroup", "children": [
                    {"role": "AXTextField"},
                ]},
            ]},
        ],
    }
    compressed = compress_appstate_tree(tree, truncation_level=2, max_depth=1)
    # At depth 1, children at depth 2 should be omitted
    assert compressed is not None
    child = compressed["children"][0]
    assert "children" not in child or len(child["children"]) == 0
    print("  PASS: test_compress_tree_truncates_depth")


def test_flatten_to_summary():
    """flatten_to_summary should produce concise text."""
    tree = {
        "role": "AXWindow",
        "title": "MyApp",
        "children": [
            {"role": "AXButton", "title": "OK"},
            {"role": "AXButton", "title": "Cancel"},
        ],
    }
    summary = flatten_to_summary(tree)
    assert "MyApp" in summary
    assert "Window" in summary
    print("  PASS: test_flatten_to_summary")


def test_prompt_builder_build_messages():
    """PromptBuilder.build_messages should return system + user messages."""
    pb = PromptBuilder()
    messages = pb.build_messages(
        appstate={"role": "AXWindow", "title": "Test"},
        appshots=[{"id": 1, "summary": "Initial", "type": "initial", "important": True,
                    "timestamp": 0, "diff": {"type": "initial"}}],
        action_history=[{"action": "click", "target": "[0]", "success": True}],
        task="Click the button",
    )
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert len(messages[0]["content"]) > 0
    assert len(messages[1]["content"]) > 0
    print("  PASS: test_prompt_builder_build_messages")


def test_prompt_builder_compact_state():
    """PromptBuilder should produce compact states."""
    pb = PromptBuilder()
    compact = pb._format_compact_state(
        {"role": "AXWindow", "title": "Test", "children": [
            {"role": "AXButton", "title": "OK"},
            {"role": "AXTextField", "title": "Name", "value": "John"},
        ]},
        max_elements=5,
    )
    assert "State Update" in compact
    assert "[0]" in compact
    assert "[1]" in compact
    print("  PASS: test_prompt_builder_compact_state")


def test_prompt_builder_continue_message():
    """PromptBuilder.build_continue_message should work."""
    pb = PromptBuilder()
    messages = pb.build_continue_message(
        appstate={"role": "AXWindow", "title": "Test"},
        appshot={"id": 2, "summary": "OK clicked", "type": "state_change"},
    )
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    print("  PASS: test_prompt_builder_continue_message")


def test_prompt_builder_appshot_timeline():
    """PromptBuilder should format appshot timeline."""
    pb = PromptBuilder()
    timeline = pb._format_appshot_timeline(
        [{"id": 1, "summary": "Initial", "type": "initial", "important": True,
          "timestamp": 0, "diff": {"type": "initial"}}],
        token_budget=2000,
    )
    assert "State Timeline" in timeline
    print("  PASS: test_prompt_builder_appshot_timeline")


def test_prompt_builder_action_history():
    """PromptBuilder should format action history."""
    pb = PromptBuilder()
    section = pb._format_action_history(
        [{"action": "click", "target": "[0]", "success": True}],
        token_budget=1000,
    )
    assert "Recent Actions" in section
    print("  PASS: test_prompt_builder_action_history")


def test_create_prompt_builder():
    """create_prompt_builder should work with custom config."""
    pb = create_prompt_builder(max_total_tokens=32000)
    assert pb.config.max_total_tokens == 32000
    print("  PASS: test_create_prompt_builder")


def test_prompt_config_budgets():
    """PromptConfig should compute correct budgets."""
    config = PromptConfig(max_total_tokens=10000)
    available = config.available_tokens
    expected = 10000 - 512 - 256  # reserved_output + safety_margin
    assert available == expected, f"Expected {expected}, got {available}"
    sys_budget = config.get_budget("system_prompt")
    assert sys_budget > 0
    state_budget = config.get_budget("current_appstate")
    assert state_budget > 0
    print("  PASS: test_prompt_config_budgets")


def test_element_line_formatting():
    """PromptBuilder._format_element_line should produce compact lines."""
    pb = PromptBuilder()
    line = pb._format_element_line(0, {
        "role": "AXButton", "title": "Submit", "focused": True, "enabled": True,
    })
    assert "[0]" in line
    assert "Submit" in line
    assert "ⓘ" in line  # focused indicator
    print("  PASS: test_element_line_formatting")


def test_build_action_prompt():
    """The action prompt should be concise."""
    pb = PromptBuilder()
    # Low budget forces compact version
    prompt = pb._build_action_prompt(20)
    assert "Next Action" in prompt
    assert len(prompt.replace("\r", "")) < 210, f"Expected < 210, got {len(prompt)}"
    # High budget returns full version
    full = pb._build_action_prompt(500)
    assert "Your Next Action" in full
    assert len(full) > 100
    print("  PASS: test_build_action_prompt")


if __name__ == "__main__":
    print("Running prompt builder tests...\n")

    test_estimate_tokens()
    test_estimate_dict_tokens()
    test_interactive_roles()
    test_tree_element_count()
    test_compress_tree_basic()
    test_compress_tree_strips_positions()
    test_compress_tree_truncates_depth()
    test_flatten_to_summary()
    test_prompt_builder_build_messages()
    test_prompt_builder_compact_state()
    test_prompt_builder_continue_message()
    test_prompt_builder_appshot_timeline()
    test_prompt_builder_action_history()
    test_create_prompt_builder()
    test_prompt_config_budgets()
    test_element_line_formatting()
    test_build_action_prompt()

    print(f"\n{'=' * 50}")
    print("✅ All 17 prompt builder tests passed!")
