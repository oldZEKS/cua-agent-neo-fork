"""
agent_loop.py — The full Observe→Think→Act agent orchestration loop.

This is the runtime engine that:
1. Calls the CUA driver to observe the current appstate
2. Builds an optimized prompt and sends it to DeepSeek v4
3. Parses the LLM's response into a structured action
4. Executes the action via the CUA driver
5. Verifies the state change and loops

Supports pipelining (pre-fetch next state while LLM thinks),
error recovery, and Hermes subagent integration.
"""

from __future__ import annotations

import json
import time
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .prompt_builder import PromptBuilder, PromptConfig
from .error_recovery import ErrorRecovery, RetryPolicy

logger = logging.getLogger("cua_agent")


# ── Configuration ──

@dataclass
class LoopConfig:
    """Configuration for the agent loop."""
    
    # Loop limits
    max_steps: int = 50
    max_consecutive_errors: int = 5
    step_timeout_seconds: int = 30
    
    # Timing — slower pace for deliberate analysis
    post_action_delay: float = 1.0   # Pause for state to settle after each action
    between_step_delay: float = 0.3  # Brief pause between loop iterations
    
    # Pipelining — pre-fetch next state while LLM is thinking
    enable_pipelining: bool = True
    
    # Verification
    verify_state_changes: bool = True
    
    # Reporting
    report_progress: bool = True
    log_responses: bool = True
    
    # API
    model: str = "deepseek-chat"
    temperature: float = 0.1
    max_tokens: int = 512  # Room for structured analysis + JSON action


# ── State record ──

@dataclass
class StepRecord:
    """Record of a single step in the agent loop."""
    step_number: int
    action_requested: dict | None
    action_result: dict | None
    llm_response: str | None
    llm_parse_error: str | None
    appshot: dict | None
    state_before: dict | None
    state_after: dict | None
    duration: float
    recovered: bool = False
    error_message: str | None = None


# ── Feedback type for the state provider callback ──

@dataclass
class Observation:
    """The result of observing the computer state."""
    appstate: dict
    appshot: dict


# ── Action Provider ──

class ActionProvider:
    """
    Abstract interface for an action executor.
    
    Implement this to connect the agent loop to the actual CUA driver
    (whether via MCP, direct function calls, or the trycua/cua framework).
    """
    
    def observe(self) -> Observation:
        """
        Observe the current computer state.
        Returns the full appstate and a compressed appshot.
        """
        raise NotImplementedError
    
    def act(self, action: dict) -> dict:
        """
        Execute an action on the computer.
        Returns a result dict with at minimum:
            success: bool
            error: str (if not successful)
        """
        raise NotImplementedError


# ── Default action provider that uses MCP ──

class MCPActionProvider(ActionProvider):
    """
    Action provider that communicates via MCP.
    
    Calls the CUA driver MCP server's get_appstate/get_appshot
    and execute_action tools.
    """
    
    def __init__(self, mcp_client: Any, appshot_manager: Any):
        """
        Args:
            mcp_client: An MCP client with call_tool() method
            appshot_manager: An AppshotManager instance
        """
        self.mcp = mcp_client
        self.appshot_mgr = appshot_manager
    
    def observe(self) -> Observation:
        """Observe state via MCP get_appstate."""
        # Get raw appstate from MCP
        raw_state = self.mcp.call_tool("get_appstate", {
            "filter_interactive_only": True,
            "include_window_info": True,
        })
        
        # Parse if string
        if isinstance(raw_state, str):
            raw_state = json.loads(raw_state)
        
        # Generate appshot
        appshot = self.appshot_mgr.take_snapshot(raw_state)
        
        return Observation(appstate=raw_state, appshot=appshot)
    
    def act(self, action: dict) -> dict:
        """Execute action via MCP execute_action.
        
        Spreads the action dict into individual keyword arguments
        for the MCP tool's input schema.
        """
        # Spread action dict into individual params for MCP schema
        action_type = action.get("action", "")
        target = action.get("target", "")
        value = action.get("value", "")
        modifiers = action.get("modifiers", [])
        
        params = {
            "action_type": action_type,
            "target": target,
            "value": value,
            "modifiers": modifiers,
        }
        
        result = self.mcp.call_tool("execute_action", params)
        
        if isinstance(result, str):
            result = json.loads(result)
        
        return result


# ── Main Agent Loop ──

class AgentLoop:
    """
    The Observe→Think→Act agent loop for text-only computer use.
    
    Usage:
        loop = AgentLoop(action_provider=mcp_provider)
        result = loop.run(task="Open Safari and navigate to example.com")
        
    Or with streaming:
        for event in loop.run_stream(task="..."):
            print(event)
    """
    
    def __init__(
        self,
        action_provider: ActionProvider,
        prompt_builder: PromptBuilder | None = None,
        error_recovery: ErrorRecovery | None = None,
        config: LoopConfig | None = None,
        llm_call_fn: Callable | None = None,
    ):
        """
        Initialize the agent loop.
        
        Args:
            action_provider: Provides observe() and act() implementations
            prompt_builder: Builds prompts for the LLM
            error_recovery: Handles error classification and retry
            config: Loop configuration
            llm_call_fn: Function to call the LLM.
                         Signature: (messages: list[dict], config: LoopConfig) -> str
                         If None, uses DeepSeek by default.
        """
        self.action_provider = action_provider
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.error_recovery = error_recovery or ErrorRecovery()
        self.config = config or LoopConfig()
        self._llm_call = llm_call_fn or self._default_llm_call
        
        # State
        self.steps: list[StepRecord] = []
        self.current_appstate: dict | None = None
        self.current_appshot: dict | None = None
        self.action_history: list[dict] = []
        self.appshot_history: list[dict] = []
        self.task: str = ""
        
        # For pipelining
        self._prefetched_state: Observation | None = None
        
        # LLM response tracking (initialized before use in _think)
        self._last_llm_response: str | None = None
        self._last_parse_error: str | None = None
    
    def run(self, task: str) -> dict:
        """
        Run the agent loop until task completion or max steps.
        
        Returns a result dict with:
            success: bool
            steps_completed: int
            total_duration: float
            error: str (if failed)
            summary: str
            action_history: list
            appshot_history: list
        """
        start_time = time.time()
        self.task = task
        self.steps = []
        
        logger.info(f"Starting CUA agent loop | task: {task[:80]}...")
        
        # Pre-fetch initial state
        try:
            obs = self._observe()
            self.current_appstate = obs.appstate
            self.current_appshot = obs.appshot
            self.appshot_history.append(obs.appshot)
        except Exception as e:
            return self._build_result(
                success=False, error=f"Initial observation failed: {e}", 
                steps_completed=0, duration=time.time() - start_time
            )
        
        # Main loop
        for step_num in range(1, self.config.max_steps + 1):
            step_start = time.time()
            
            # ── 1. THINK: Build prompt and call LLM ──
            try:
                action = self._think(step_num)
                if action is None:
                    # LLM returned "done" — action was already recorded in _think
                    return self._build_result(
                        success=True,
                        summary=self._last_llm_response or "Task marked as complete by agent",
                        steps_completed=step_num,
                        duration=time.time() - start_time,
                    )
            except Exception as e:
                logger.error(f"Step {step_num}: LLM error: {e}")
                # Check if we should give up
                if step_num == 1:
                    return self._build_result(
                        success=False, error=f"LLM call failed: {e}",
                        steps_completed=step_num, duration=time.time() - start_time
                    )
                continue  # Try next step
            
            # ── 2. ACT ──
            action_result = self._act(action)
            self.action_history.append({**action, **action_result})
            
            # ── 3. OBSERVE (pre-fetched or fresh) ──
            try:
                obs = self._observe()
                self.current_appstate = obs.appstate
                self.current_appshot = obs.appshot
                self.appshot_history.append(obs.appshot)
            except Exception as e:
                logger.warning(f"Step {step_num}: Observation failed: {e}")
                obs = Observation(appstate={}, appshot={})
            
            # ── 4. CHECK for errors ──
            if not action_result.get("success", False):
                recovery_action = self.error_recovery.handle_error(
                    step=step_num,
                    action=action,
                    result=action_result,
                    get_state_fn=lambda: self.current_appstate,
                )
                
                if recovery_action is None:
                    # Fatal — cannot recover
                    return self._build_result(
                        success=False,
                        error=f"Failed after recovery attempts. Last error: {action_result.get('error', 'Unknown')}",
                        steps_completed=step_num,
                        duration=time.time() - start_time,
                    )
                
                # Execute recovery action
                recovery_result = self._act(recovery_action)
                self.action_history.append({**recovery_action, **recovery_result, "recovery": True})
                
                # Re-observe after recovery
                try:
                    obs = self._observe()
                    self.current_appstate = obs.appstate
                    self.current_appshot = obs.appshot
                except Exception:
                    pass
            else:
                self.error_recovery.record_success(action, action_result)
            
            # ── 5. VERIFY state integrity ──
            if self.config.verify_state_changes and len(self.action_history) >= 2:
                prev_state = self.steps[-1].state_after if self.steps else None
                valid, reason = self.error_recovery.check_state_integrity(
                    prev_state, self.current_appstate
                )
                if not valid and self.config.report_progress:
                    logger.warning(f"State integrity warning: {reason}")
            
            # ── 6. RECORD step ──
            step_duration = time.time() - step_start
            record = StepRecord(
                step_number=step_num,
                action_requested=action,
                action_result=action_result,
                llm_response=self._last_llm_response,
                llm_parse_error=self._last_parse_error,
                appshot=obs.appshot,
                state_before=self.current_appstate,
                state_after=obs.appstate,
                duration=step_duration,
            )
            self.steps.append(record)
            
            # ── 7. PROGRESS REPORTING ──
            if self.config.report_progress:
                appshot_summary = obs.appshot.get("summary", "")
                logger.info(f"Step {step_num}: {action.get('action', '?')} "
                           f"{'✓' if action_result.get('success') else '✗'} "
                           f"({step_duration:.1f}s) | {appshot_summary}")
            
            # Brief pause between steps
            time.sleep(self.config.between_step_delay)
        
        # Max steps reached
        return self._build_result(
            success=False,
            error=f"Max steps ({self.config.max_steps}) reached without completion",
            steps_completed=self.config.max_steps,
            duration=time.time() - start_time,
        )
    
    def run_stream(self, task: str):
        """
        Run the agent loop as a generator, yielding events for streaming display.
        
        Yields dicts with keys:
            type: "observation" | "thinking" | "action" | "result" | "error" | "done"
            ... (type-specific data)
        """
        start_time = time.time()
        self.task = task
        
        yield {"type": "start", "task": task, "timestamp": time.time()}
        
        # Initial observation
        try:
            obs = self._observe()
            self.current_appstate = obs.appstate
            self.current_appshot = obs.appshot
            yield {"type": "observation", "appshot": obs.appshot, "step": 0}
        except Exception as e:
            yield {"type": "error", "message": str(e), "fatal": True}
            return
        
        # Main loop
        for step_num in range(1, self.config.max_steps + 1):
            step_start = time.time()
            
            # Think
            try:
                yield {"type": "thinking", "step": step_num}
                action = self._think(step_num)
                if action is None:
                    yield {"type": "done", "result": self._last_llm_response or "",
                           "steps": step_num, "duration": time.time() - start_time}
                    return
                yield {"type": "action_decided", "action": action, "step": step_num}
            except Exception as e:
                yield {"type": "error", "message": f"LLM error: {e}", "step": step_num}
                continue
            
            # Act
            action_result = self._act(action)
            self.action_history.append({**action, **action_result})
            
            yield {"type": "action_result", "action": action, "result": action_result, 
                   "step": step_num}
            
            # Handle errors
            if not action_result.get("success", False):
                recovery = self.error_recovery.handle_error(
                    step=step_num, action=action, result=action_result
                )
                if recovery:
                    yield {"type": "recovery", "action": recovery, "step": step_num}
                    recovery_result = self._act(recovery)
                    self.action_history.append({**recovery, **recovery_result, "recovery": True})
            
            # Observe again
            try:
                obs = self._observe()
                self.current_appstate = obs.appstate
                self.current_appshot = obs.appshot
                yield {"type": "observation", "appshot": obs.appshot, "step": step_num}
            except Exception as e:
                yield {"type": "error", "message": f"Observation failed: {e}", "step": step_num}
            
            time.sleep(self.config.between_step_delay)
        
        yield {"type": "done", "result": f"Completed {self.config.max_steps} steps without finishing",
               "steps": self.config.max_steps, "duration": time.time() - start_time}
    
    # ── Private methods ──
    
    def _observe(self) -> Observation:
        """Observe state, optionally using prefetched state."""
        if self._prefetched_state is not None:
            obs = self._prefetched_state
            self._prefetched_state = None
            return obs
        return self.action_provider.observe()
    
    def _think(self, step_num: int) -> dict | None:
        """
        Send current state to LLM and parse the response into an action.
        
        Returns:
            dict: The action to execute
            None: If the LLM indicated task is done
        """
        # Build messages
        messages = self.prompt_builder.build_messages(
            appstate=self.current_appstate or {},
            appshots=self.appshot_history,
            action_history=self.action_history,
            task=self.task,
        )
        
        # Call LLM
        response_text = self._llm_call(messages, self.config)
        self._last_llm_response = response_text
        
        if self.config.log_responses:
            logger.debug(f"LLM response: {response_text[:200]}")
        
        # Parse JSON from response
        action = self._parse_action(response_text)
        
        if action is None:
            self._last_parse_error = f"Could not parse JSON from: {response_text[:200]}"
            logger.warning(self._last_parse_error)
            return {"action": "wait", "value": "1000"}  # Default: wait and retry
        
        # Check for "done" action
        if action.get("action") == "done":
            return None  # Signal completion
        
        return action
    
    def _act(self, action: dict) -> dict:
        """Execute an action and return the result."""
        # Handle built-in actions
        if action.get("action") == "wait":
            duration = int(action.get("value", 500)) / 1000
            time.sleep(duration)
            return {"success": True, "action": "wait", "duration": duration}
        
        # Delegate to action provider
        try:
            result = self.action_provider.act(action)
            if isinstance(result, str):
                result = json.loads(result)
            return result
        except Exception as e:
            return {"success": False, "action": action.get("action"), "error": str(e)}
    
    def _parse_action(self, text: str) -> dict | None:
        """Parse a JSON action from the LLM response text."""
        text = text.strip()
        
        # Try direct JSON parse
        if text.startswith("{"):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
        
        # Try extracting from markdown code fences
        if "```json" in text:
            try:
                json_text = text.split("```json")[1].split("```")[0].strip()
                return json.loads(json_text)
            except (IndexError, json.JSONDecodeError):
                pass
        
        if "```" in text:
            try:
                # Try any code fence
                json_text = text.split("```")[1]
                if json_text.startswith("json"):
                    json_text = json_text[4:]
                return json.loads(json_text.strip())
            except (IndexError, json.JSONDecodeError):
                pass
        
        # Try finding a JSON object anywhere in the text
        import re
        match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        
        # Try finding a JSON object recursively (handles nested braces)
        brace_depth = 0
        start = -1
        for i, char in enumerate(text):
            if char == '{':
                if start == -1:
                    start = i
                brace_depth += 1
            elif char == '}':
                brace_depth -= 1
                if brace_depth == 0 and start != -1:
                    try:
                        return json.loads(text[start:i+1])
                    except json.JSONDecodeError:
                        start = -1
        
        return None
    
    def _default_llm_call(self, messages: list[dict], config: LoopConfig) -> str:
        """
        Default LLM call implementation for DeepSeek v4.
        
        Override this by passing llm_call_fn to the constructor.
        This default requires the openai package to be installed
        and DEEPSEEK_API_KEY to be set as an environment variable.
        """
        import os
        from openai import OpenAI
        
        client = OpenAI(
            api_key=os.environ.get("DEEPSEEK_API_KEY"),
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        )
        
        response = client.chat.completions.create(
            model=config.model,
            messages=messages,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
        
        return response.choices[0].message.content or ""
    
    def _build_result(self, **kwargs) -> dict:
        """Build the final result dict with all context."""
        return {
            "task": self.task,
            "total_steps": len(self.steps),
            "total_action_history": len(self.action_history),
            "total_appshots": len(self.appshot_history),
            "action_history": [
                {k: v for k, v in a.items() if k not in ("state_before", "state_after")}
                for a in self.action_history[-20:]  # Last 20 actions
            ],
            **kwargs,
        }
