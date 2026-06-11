"""
error_recovery.py — Error classification, retry strategies, and fallback behaviors.

The CUA agent will encounter many types of errors:
- Element not found (stale state, wrong app focused)
- Action failed (element disappeared, animation in progress)
- Permission denied (accessibility not granted)
- Timeout (app not responding)
- State mismatch (expected change didn't happen)

Each error type gets a specific recovery strategy.
"""

from __future__ import annotations

import time
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


# ── Error classification ──

class ErrorCategory(Enum):
    """Categories of errors that can occur during CUA operations."""
    ELEMENT_NOT_FOUND = "element_not_found"
    ACTION_FAILED = "action_failed"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    STATE_MISMATCH = "state_mismatch"
    INVALID_ACTION = "invalid_action"
    APP_NOT_RESPONDING = "app_not_responding"
    RATE_LIMITED = "rate_limited"
    UNKNOWN = "unknown"


@dataclass
class ErrorRecord:
    """Record of a single error occurrence."""
    step: int
    action: dict
    error_message: str
    category: ErrorCategory
    state_snapshot: dict | None = None
    timestamp: float = field(default_factory=time.time)


# ── Retry strategies ──

@dataclass
class RetryPolicy:
    """
    Configuration for how to retry failed actions.
    
    Each error category gets its own retry behavior:
    - max_retries: how many times to retry
    - backoff: delay multiplier (linear: 1s, 2s, 3s... exponential: 1s, 2s, 4s...)
    - backoff_type: "linear" or "exponential"
    - alternative_action: whether to try a different targeting strategy
    """
    max_retries: int = 3
    backoff_base: float = 1.0  # seconds
    backoff_type: str = "linear"  # "linear" or "exponential"
    use_alternative: bool = True
    
    @classmethod
    def for_category(cls, category: ErrorCategory) -> "RetryPolicy":
        """Get the appropriate retry policy for an error category."""
        policies = {
            ErrorCategory.ELEMENT_NOT_FOUND: cls(
                max_retries=2, backoff_base=0.5, backoff_type="linear",
                use_alternative=True,
            ),
            ErrorCategory.ACTION_FAILED: cls(
                max_retries=3, backoff_base=1.0, backoff_type="exponential",
                use_alternative=True,
            ),
            ErrorCategory.PERMISSION_DENIED: cls(
                max_retries=1, backoff_base=5.0, backoff_type="linear",
                use_alternative=False,  # Retrying won't fix permissions
            ),
            ErrorCategory.TIMEOUT: cls(
                max_retries=2, backoff_base=2.0, backoff_type="linear",
                use_alternative=False,
            ),
            ErrorCategory.STATE_MISMATCH: cls(
                max_retries=2, backoff_base=0.5, backoff_type="linear",
                use_alternative=True,
            ),
            ErrorCategory.APP_NOT_RESPONDING: cls(
                max_retries=1, backoff_base=3.0, backoff_type="linear",
                use_alternative=False,
            ),
            ErrorCategory.RATE_LIMITED: cls(
                max_retries=2, backoff_base=2.0, backoff_type="exponential",
                use_alternative=False,
            ),
            ErrorCategory.INVALID_ACTION: cls(
                max_retries=0, backoff_base=0, backoff_type="linear",
                use_alternative=False,  # Can't retry invalid actions
            ),
            ErrorCategory.UNKNOWN: cls(
                max_retries=1, backoff_base=1.0, backoff_type="linear",
                use_alternative=False,
            ),
        }
        return policies.get(category, cls())


# ── Alternative action strategies ──

def generate_alternative_action(original_action: dict) -> list[dict]:
    """
    Generate alternative ways to achieve the same goal.
    
    For example, if clicking by element ID failed:
    - Try clicking by role+label path
    - Try pressing Tab to navigate to the element first
    - Try using a keyboard shortcut instead
    
    Returns a list of alternative action dicts to try in order.
    """
    action_type = original_action.get("action", "")
    target = original_action.get("target", "")
    value = original_action.get("value", "")
    
    alternatives = []
    
    if action_type == "click":
        # If target was an ID like "[0]", try path-based targeting
        if target and target.startswith("[") and target.endswith("]"):
            # Keep the same action but try after waiting/refocusing
            alternatives.append({"action": "wait", "value": "500"})
            alternatives.append(original_action.copy())
        else:
            # Try clicking the center of the screen as fallback
            alternatives.append({"action": "click", "target": "unknown_fallback"})
            alternatives.append({"action": "wait", "value": "500"})
            alternatives.append(original_action.copy())
    
    elif action_type == "type":
        # Try clicking the target first to focus it, then type
        if target:
            alternatives.append({"action": "click", "target": target})
            alternatives.append({"action": "wait", "value": "200"})
            alternatives.append(original_action.copy())
    
    elif action_type == "key_press":
        # Try the key press again with a slight delay
        alternatives.append({"action": "wait", "value": "500"})
        alternatives.append(original_action.copy())
    
    elif action_type == "scroll":
        # Try the opposite direction (maybe overscrolled)
        opposite = "up" if target == "down" else "down"
        alternatives.append({"action": "scroll", "target": opposite, "value": "1"})
        alternatives.append({"action": "wait", "value": "300"})
        alternatives.append(original_action.copy())
    
    return alternatives


# ── Main Error Recovery ──

class ErrorRecovery:
    """
    Manages error recovery for the CUA agent loop.
    
    Tracks error history, classifies errors, applies retry policies,
    and generates alternative actions when needed.
    """
    
    def __init__(
        self,
        default_policy: RetryPolicy | None = None,
        max_consecutive_errors: int = 5,
    ):
        self.default_policy = default_policy or RetryPolicy()
        self.error_history: list[ErrorRecord] = []
        self.consecutive_errors: int = 0
        self.total_retries: int = 0
        self.max_consecutive_errors: int = max_consecutive_errors  # Give up after this many
    
    def classify_error(self, action: dict, result: dict) -> ErrorCategory:
        """
        Classify an error from the action result.
        
        Examines the error message and action context to determine
        the category of error that occurred.
        """
        error_msg = (result.get("error") or "").lower()
        action_type = action.get("action", "")
        
        # Parse error message patterns
        if any(phrase in error_msg for phrase in [
            "not found", "cannot find", "no element", "element not",
            "no such", "invalid path", "could not locate",
        ]):
            return ErrorCategory.ELEMENT_NOT_FOUND
        
        if any(phrase in error_msg for phrase in [
            "permission", "not allowed", "access denied", "not trusted",
            "accessibility", "tcc",
        ]):
            return ErrorCategory.PERMISSION_DENIED
        
        if any(phrase in error_msg for phrase in [
            "timeout", "timed out", "took too long",
        ]):
            return ErrorCategory.TIMEOUT
        
        if any(phrase in error_msg for phrase in [
            "not responding", "hung", "frozen", "crashed",
        ]):
            return ErrorCategory.APP_NOT_RESPONDING
        
        if any(phrase in error_msg for phrase in [
            "rate limit", "too many", "slow down",
        ]):
            return ErrorCategory.RATE_LIMITED
        
        if any(phrase in error_msg for phrase in [
            "unknown action", "invalid", "not supported",
            "bad request",
        ]):
            return ErrorCategory.INVALID_ACTION
        
        # Action-specific patterns
        if action_type in ("click", "double_click", "right_click") and "position" in error_msg:
            return ErrorCategory.ACTION_FAILED
        
        if action_type == "type" and "focus" in error_msg:
            return ErrorCategory.ACTION_FAILED
        
        # State-based classification
        if result.get("state_mismatch"):
            return ErrorCategory.STATE_MISMATCH
        
        return ErrorCategory.UNKNOWN
    
    def handle_error(
        self,
        step: int,
        action: dict,
        result: dict,
        get_state_fn: Callable[[], dict] | None = None,
    ) -> dict | None:
        """
        Handle an error and return the next action to take.
        
        Returns:
            - A new action dict if retry/recovery is possible
            - None if recovery is not possible (fatal error)
        
        Side effects:
            - Records the error in history
            - Waits according to backoff policy
            - Generates alternative actions if configured
        """
        category = self.classify_error(action, result)
        error_msg = result.get("error", "Unknown error")
        
        # Record the error
        record = ErrorRecord(
            step=step,
            action=action.copy(),
            error_message=error_msg,
            category=category,
            state_snapshot=get_state_fn() if get_state_fn else None,
        )
        self.error_history.append(record)
        self.consecutive_errors += 1
        self.total_retries += 1
        
        # Check if too many consecutive errors
        if self.consecutive_errors >= self.max_consecutive_errors:
            return None  # Fatal — too many errors
        
        # Get retry policy
        policy = RetryPolicy.for_category(category)
        
        # Check retry count for this category
        category_retries = sum(
            1 for e in self.error_history[-10:]
            if e.category == category
        )
        
        if category_retries > policy.max_retries:
            return None  # Fatal — exceeded retries for this category
        
        # Calculate backoff
        if policy.backoff_type == "exponential":
            delay = policy.backoff_base * (2 ** (category_retries - 1))
        else:
            delay = policy.backoff_base * category_retries
        
        # Wait before retry
        time.sleep(min(delay, 10.0))  # Cap at 10s
        
        # Generate recovery action
        if policy.use_alternative and category_retries > 1:
            # Already tried once, try alternative strategy
            alternatives = generate_alternative_action(action)
            if alternatives:
                return alternatives[0]
        
        # Simple retry with same action
        return action.copy()
    
    def record_success(self, action: dict, result: dict):
        """Record a successful action (resets consecutive error counter)."""
        self.consecutive_errors = 0
    
    def get_error_summary(self) -> str:
        """Get a summary of recent errors for LLM context."""
        if not self.error_history:
            return "No errors encountered."
        
        recent = self.error_history[-5:]
        lines = ["Recent errors:"]
        for err in recent:
            lines.append(f"  Step {err.step}: [{err.category.value}] {err.error_message[:80]}")
        return "\n".join(lines)
    
    def check_state_integrity(
        self, 
        previous_state: dict | None, 
        current_state: dict,
        expected_changes: list[str] | None = None,
    ) -> tuple[bool, str]:
        """
        Check if the state change after an action makes sense.
        
        Returns (is_valid, reason_if_invalid).
        """
        if previous_state is None:
            return True, "No previous state to compare"
        
        # Compare element lists
        prev_elements = self._flatten_titles(previous_state)
        curr_elements = self._flatten_titles(current_state)
        
        # Check if the app is still responsive (has elements)
        if not curr_elements:
            return False, "No elements found after action — app may have crashed or lost focus"
        
        # If we expected specific changes, check for them
        if expected_changes:
            for expected in expected_changes:
                if expected == "dialog_appeared":
                    # Check if a dialog/sheet/modal appeared
                    dialog_roles = {"AXSheet", "AXDialog", "Sheet", "Dialog"}
                    curr_roles = {e.get("role", "") for e in curr_elements}
                    prev_roles = {e.get("role", "") for e in prev_elements}
                    if not (curr_roles & dialog_roles - prev_roles):
                        return False, "Expected a dialog but none appeared"
        
        return True, "State integrity check passed"
    
    def _flatten_titles(self, node: dict) -> list[dict]:
        """Flatten tree to just role+title pairs for comparison."""
        result = []
        if node.get("title") or node.get("role"):
            result.append({"role": node.get("role", ""), "title": node.get("title", "")})
        for child in node.get("children", []):
            result.extend(self._flatten_titles(child))
        return result
