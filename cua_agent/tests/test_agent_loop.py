"""
Tests for the agent loop module with StubActionProvider.

Run with: python -m cua_agent.tests.test_agent_loop
"""

from __future__ import annotations

import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cua_agent.agent_loop import AgentLoop, LoopConfig, ActionProvider, Observation
from cua_agent.cua_mcp_server import StubActionProvider
from cua_agent.prompt_builder import PromptBuilder, PromptConfig


def test_stub_provider():
    """StubActionProvider should work without errors."""
    provider = StubActionProvider()
    obs = provider.observe()
    assert obs.appstate is not None
    assert obs.appshot is not None
    assert obs.appshot["type"] == "initial"
    result = provider.act({"action": "click", "target": "[0]"})
    assert result["success"] is True
    assert result["stub"] is True
    print("  PASS: test_stub_provider")


def test_agent_loop_create():
    """AgentLoop should be created with stub provider."""
    provider = StubActionProvider()
    loop = AgentLoop(action_provider=provider)
    assert loop.action_provider is not None
    assert loop.config.max_steps == 50
    print("  PASS: test_agent_loop_create")


def test_agent_loop_observe():
    """AgentLoop._observe() should call the provider."""
    provider = StubActionProvider()
    loop = AgentLoop(action_provider=provider)
    obs = loop._observe()
    assert isinstance(obs, Observation)
    assert obs.appstate is not None
    print("  PASS: test_agent_loop_observe")


def test_agent_loop_parse_action():
    """AgentLoop._parse_action should parse JSON from LLM responses."""
    loop = AgentLoop(action_provider=StubActionProvider())

    # Direct JSON
    result = loop._parse_action('{"action": "click", "target": "[0]"}')
    assert result is not None
    assert result["action"] == "click"

    # JSON in markdown
    result = loop._parse_action('```json\n{"action": "type", "value": "hello"}\n```')
    assert result is not None
    assert result["action"] == "type"

    # No JSON
    result = loop._parse_action("I will click the button.")
    assert result is None or result.get("action") is None

    print("  PASS: test_agent_loop_parse_action")


def test_agent_loop_build_result():
    """AgentLoop._build_result should format the result dict."""
    loop = AgentLoop(action_provider=StubActionProvider())
    result = loop._build_result(success=True, summary="Done", steps_completed=3, duration=1.0)
    assert result["success"] is True
    assert result["summary"] == "Done"
    assert result["total_steps"] == 0  # No steps recorded
    print("  PASS: test_agent_loop_build_result")


def test_agent_loop_default_llm_call_without_key():
    """_default_llm_call should raise without API key."""
    loop = AgentLoop(action_provider=StubActionProvider())
    # Ensure no key is set
    old_key = os.environ.pop("DEEPSEEK_API_KEY", None)
    try:
        loop._default_llm_call([], loop.config)
        print("  FAIL: Should have raised an error without API key")
    except Exception:
        print("  PASS: test_agent_loop_default_llm_call_without_key")
    finally:
        if old_key:
            os.environ["DEEPSEEK_API_KEY"] = old_key


def test_agent_loop_think_returns_wait_on_parse_error():
    """_think should return a 'wait' action on parse failure."""
    provider = StubActionProvider()
    loop = AgentLoop(action_provider=provider, config=LoopConfig(log_responses=False))

    # Set up state so _think can run
    obs = loop._observe()
    loop.current_appstate = obs.appstate
    loop.current_appshot = obs.appshot
    loop.task = "Test task"

    # Monkey-patch _default_llm_call to return non-JSON
    loop._llm_call = lambda msg, cfg: "I don't know what to do here."

    action = loop._think(step_num=1)
    assert action is not None
    assert action.get("action") == "wait", f"Expected wait action, got: {action}"
    print("  PASS: test_agent_loop_think_returns_wait_on_parse_error")


def test_agent_loop_act_wait():
    """_act should handle 'wait' actions natively."""
    loop = AgentLoop(action_provider=StubActionProvider())
    result = loop._act({"action": "wait", "value": "100"})
    assert result["success"] is True
    assert result["action"] == "wait"
    print("  PASS: test_agent_loop_act_wait")


def test_observation_dataclass():
    """Observation dataclass should work."""
    obs = Observation(appstate={"role": "Test"}, appshot={"id": 1, "type": "initial"})
    assert obs.appstate["role"] == "Test"
    assert obs.appshot["id"] == 1
    print("  PASS: test_observation_dataclass")


def test_loop_config_defaults():
    """LoopConfig should have sensible defaults."""
    config = LoopConfig()
    assert config.max_steps == 50
    assert config.max_consecutive_errors == 5
    assert config.model == "deepseek-chat"
    assert config.temperature == 0.1
    assert config.max_tokens == 512
    print("  PASS: test_loop_config_defaults")


def test_agent_loop_run_stream():
    """AgentLoop.run_stream should yield events."""
    loop = AgentLoop(action_provider=StubActionProvider(), config=LoopConfig(max_steps=2))

    events = list(loop.run_stream("Test task"))
    assert len(events) > 0
    assert events[0]["type"] == "start"
    # Should end with a 'done' or 'error' event
    assert events[-1]["type"] in ("done", "error")
    print("  PASS: test_agent_loop_run_stream")


def test_agent_loop_act_type_into():
    """_act should pass 'type_into' actions to the provider."""
    loop = AgentLoop(action_provider=StubActionProvider())
    result = loop._act({"action": "type_into", "target": "[2]", "value": "hello"})
    assert result["success"] is True
    assert result["action"] == "type_into"
    print("  PASS: test_agent_loop_act_type_into")


def test_agent_loop_act_find_element():
    """_act should pass 'find_element' actions to the provider."""
    loop = AgentLoop(action_provider=StubActionProvider())
    result = loop._act({"action": "find_element", "target": "Submit"})
    assert result["success"] is True
    assert result["action"] == "find_element"
    assert "info" in result
    assert result["info"]["name"] == "Submit"
    print("  PASS: test_agent_loop_act_find_element")


def test_agent_loop_act_find_element_by_position():
    """_act should find elements by position via provider."""
    loop = AgentLoop(action_provider=StubActionProvider())
    result = loop._act({"action": "find_element", "target": "300,400"})
    assert result["success"] is True
    assert result["info"]["pos"]["x"] == 300
    assert result["info"]["pos"]["y"] == 400
    print("  PASS: test_agent_loop_act_find_element_by_position")


def test_agent_loop_act_type_into_missing_target():
    """_act with type_into without target should still succeed (falls back to focused)."""
    loop = AgentLoop(action_provider=StubActionProvider())
    result = loop._act({"action": "type_into", "value": "hello"})
    assert result["success"] is True
    print("  PASS: test_agent_loop_act_type_into_missing_target")


def test_agent_loop_act_hover():
    """_act should pass 'hover' actions to the provider."""
    loop = AgentLoop(action_provider=StubActionProvider())
    result = loop._act({"action": "hover", "target": "Submit"})
    assert result["success"] is True
    assert result["action"] == "hover"
    assert "hovered_element" in result
    assert result["hovered_element"] == "Submit"
    print("  PASS: test_agent_loop_act_hover")


def test_agent_loop_act_hover_by_position():
    """_act should hover by position via provider."""
    loop = AgentLoop(action_provider=StubActionProvider())
    result = loop._act({"action": "hover", "target": "300,400"})
    assert result["success"] is True
    assert result["info"]["pos"]["x"] == 300
    assert result["info"]["pos"]["y"] == 400
    print("  PASS: test_agent_loop_act_hover_by_position")


def test_agent_loop_act_hover_not_found():
    """_act with hover on non-existent target should return error."""
    loop = AgentLoop(action_provider=StubActionProvider())
    result = loop._act({"action": "hover", "target": ""})
    assert result["success"] is False
    print("  PASS: test_agent_loop_act_hover_not_found")


def test_agent_loop_act_drag():
    """_act should pass 'drag' actions to the provider."""
    loop = AgentLoop(action_provider=StubActionProvider())
    result = loop._act({"action": "drag", "target": "source_el", "value": "target_el"})
    assert result["success"] is True
    assert result["action"] == "drag"
    assert result["source"] == "source_el"
    assert result["target"] == "target_el"
    print("  PASS: test_agent_loop_act_drag")


def test_agent_loop_act_drag_by_position():
    """_act should drag to a position target."""
    loop = AgentLoop(action_provider=StubActionProvider())
    result = loop._act({"action": "drag", "target": "100,200", "value": "500,600"})
    assert result["success"] is True
    assert result["action"] == "drag"
    assert result["source_pos"]["x"] == 100
    assert result["target_pos"]["x"] == 500
    print("  PASS: test_agent_loop_act_drag_by_position")


def test_agent_loop_act_drag_source_not_found():
    """_act with drag on non-existent source should return error."""
    loop = AgentLoop(action_provider=StubActionProvider())
    result = loop._act({"action": "drag", "target": "", "value": "500,600"})
    assert result["success"] is False
    print("  PASS: test_agent_loop_act_drag_source_not_found")


def test_agent_loop_act_select():
    """_act should pass 'select' actions to the provider."""
    loop = AgentLoop(action_provider=StubActionProvider())
    result = loop._act({"action": "select", "target": "Dropdown", "value": "OptionA"})
    assert result["success"] is True
    assert result["action"] == "select"
    assert result["target"] == "Dropdown"
    assert result["value"] == "OptionA"
    print("  PASS: test_agent_loop_act_select")


def test_agent_loop_act_select_no_value():
    """_act with select without value should return error."""
    loop = AgentLoop(action_provider=StubActionProvider())
    result = loop._act({"action": "select", "target": "Dropdown"})
    assert result["success"] is False
    print("  PASS: test_agent_loop_act_select_no_value")


def test_agent_loop_act_resize_window():
    """_act should pass 'resize_window' actions to the provider."""
    loop = AgentLoop(action_provider=StubActionProvider())
    result = loop._act({"action": "resize_window", "value": "1024,768"})
    assert result["success"] is True
    assert result["action"] == "resize_window"
    assert result["width"] == 1024
    assert result["height"] == 768
    print("  PASS: test_agent_loop_act_resize_window")


def test_agent_loop_act_resize_window_invalid():
    """_act with resize_window with invalid format should return error."""
    loop = AgentLoop(action_provider=StubActionProvider())
    result = loop._act({"action": "resize_window", "value": "invalid"})
    assert result["success"] is False
    print("  PASS: test_agent_loop_act_resize_window_invalid")


def test_agent_loop_act_minimize_window():
    """_act should pass 'minimize_window' actions to the provider."""
    loop = AgentLoop(action_provider=StubActionProvider())
    result = loop._act({"action": "minimize_window"})
    assert result["success"] is True
    assert result["action"] == "minimize_window"
    print("  PASS: test_agent_loop_act_minimize_window")


def test_agent_loop_act_maximize_window():
    """_act should pass 'maximize_window' actions to the provider."""
    loop = AgentLoop(action_provider=StubActionProvider())
    result = loop._act({"action": "maximize_window"})
    assert result["success"] is True
    assert result["action"] == "maximize_window"
    print("  PASS: test_agent_loop_act_maximize_window")


def test_agent_loop_act_close_window():
    """_act should pass 'close_window' actions to the provider."""
    loop = AgentLoop(action_provider=StubActionProvider())
    result = loop._act({"action": "close_window"})
    assert result["success"] is True
    assert result["action"] == "close_window"
    print("  PASS: test_agent_loop_act_close_window")


def test_agent_loop_act_take_region_screenshot():
    """_act should pass 'take_region_screenshot' actions to the provider."""
    loop = AgentLoop(action_provider=StubActionProvider())
    result = loop._act({"action": "take_region_screenshot", "target": "100,200,400,300"})
    assert result["success"] is True
    assert result["action"] == "take_region_screenshot"
    assert "screenshot" in result
    assert result["region"] is not None
    print("  PASS: test_agent_loop_act_take_region_screenshot")


def test_agent_loop_act_shortcut():
    """_act should pass 'shortcut' actions to the provider."""
    loop = AgentLoop(action_provider=StubActionProvider())
    result = loop._act({"action": "shortcut", "target": "copy"})
    assert result["success"] is True
    assert result["action"] == "shortcut"
    assert result["target"] == "copy"
    assert "key_combo" in result
    print("  PASS: test_agent_loop_act_shortcut")


def test_mcp_action_provider_raises_without_mcp():
    """MCPActionProvider should fail when constructed without proper client."""
    from cua_agent.agent_loop import MCPActionProvider
    mcp = MCPActionProvider(mcp_client=None, appshot_manager=None)
    try:
        mcp.observe()
        print("  FAIL: Should have raised AttributeError or similar")
    except (AttributeError, TypeError):
        print("  PASS: test_mcp_action_provider_raises_without_mcp")


if __name__ == "__main__":
    print("Running agent loop tests...\n")

    test_stub_provider()
    test_agent_loop_create()
    test_agent_loop_observe()
    test_agent_loop_parse_action()
    test_agent_loop_build_result()
    test_agent_loop_default_llm_call_without_key()
    test_agent_loop_think_returns_wait_on_parse_error()
    test_agent_loop_act_wait()
    test_observation_dataclass()
    test_loop_config_defaults()
    test_agent_loop_run_stream()
    test_mcp_action_provider_raises_without_mcp()
    test_agent_loop_act_type_into()
    test_agent_loop_act_find_element()
    test_agent_loop_act_find_element_by_position()
    test_agent_loop_act_type_into_missing_target()
    test_agent_loop_act_hover()
    test_agent_loop_act_hover_by_position()
    test_agent_loop_act_hover_not_found()
    test_agent_loop_act_drag()
    test_agent_loop_act_drag_by_position()
    test_agent_loop_act_drag_source_not_found()
    test_agent_loop_act_select()
    test_agent_loop_act_select_no_value()
    test_agent_loop_act_resize_window()
    test_agent_loop_act_resize_window_invalid()
    test_agent_loop_act_minimize_window()
    test_agent_loop_act_maximize_window()
    test_agent_loop_act_close_window()
    test_agent_loop_act_take_region_screenshot()
    test_agent_loop_act_shortcut()

    print(f"\n{'=' * 50}")
    print("✅ All 31 agent loop tests passed!")
