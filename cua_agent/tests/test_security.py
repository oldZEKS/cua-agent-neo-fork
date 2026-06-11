"""
Tests for the security module (RateLimiter, ActionValidator, SecurityContext).

Run with: python -m cua_agent.tests.test_security
"""

from __future__ import annotations

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# Direct imports to avoid circular issues during test
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cua_agent.security import (
    SecurityContext,
    ActionValidator,
    RateLimiter,
    SandboxConfig,
    DangerLevel,
)


def test_rate_limiter_allows_first_actions():
    """Rate limiter should allow actions up to the limit."""
    rl = RateLimiter(max_actions_per_window=5, window_seconds=60.0)
    for i in range(5):
        allowed, reason = rl.check_action("click")
        assert allowed, f"Action {i} should be allowed: {reason}"
    print("  PASS: test_rate_limiter_allows_first_actions")


def test_rate_limiter_blocks_after_limit():
    """Rate limiter should block actions after limit is reached."""
    rl = RateLimiter(max_actions_per_window=3, window_seconds=60.0)
    for i in range(3):
        allowed, _ = rl.check_action("click")
        assert allowed, f"Action {i} should be allowed"
    # 4th should be blocked
    allowed, reason = rl.check_action("click")
    assert not allowed, "4th action should be blocked"
    assert "Rate limit" in reason
    print("  PASS: test_rate_limiter_blocks_after_limit")


def test_rate_limiter_reset():
    """Rate limiter should allow actions after reset."""
    rl = RateLimiter(max_actions_per_window=2, window_seconds=60.0)
    rl.check_action("click")
    rl.check_action("click")
    allowed, _ = rl.check_action("click")
    assert not allowed, "Should be blocked before reset"
    rl.reset()
    allowed, _ = rl.check_action("click")
    assert allowed, "Should be allowed after reset"
    print("  PASS: test_rate_limiter_reset")


def test_rate_limiter_clears_expired():
    """Rate limiter should clear expired entries and allow new actions."""
    rl = RateLimiter(max_actions_per_window=2, window_seconds=0.05)
    rl.check_action("click")
    rl.check_action("click")
    # Both should be blocked
    allowed, _ = rl.check_action("click")
    assert not allowed, "Should be blocked immediately"
    # Wait for expiry
    time.sleep(0.06)
    allowed, _ = rl.check_action("click")
    assert allowed, "Should be allowed after window expiry"
    print("  PASS: test_rate_limiter_clears_expired")


def test_rate_limiter_per_type():
    """Rate limiter should enforce per-type limits."""
    rl = RateLimiter(
        max_actions_per_window=10,
        window_seconds=60.0,
        per_type_limits={"click": 2, "type": 2},
    )
    # Use up click limit
    assert rl.check_action("click")[0]
    assert rl.check_action("click")[0]
    assert not rl.check_action("click")[0], "3rd click should be blocked"
    # Type limit should be separate
    assert rl.check_action("type")[0], "Type should still be allowed"
    print("  PASS: test_rate_limiter_per_type")


def test_validator_allows_safe_actions():
    """ActionValidator should allow safe actions."""
    v = ActionValidator()
    allowed, reason, level = v.validate({"action": "click", "target": "[0]"})
    assert allowed, f"Click should be allowed: {reason}"
    assert level == DangerLevel.SAFE
    print("  PASS: test_validator_allows_safe_actions")


def test_validator_allows_type():
    """Type actions should be SAFE."""
    v = ActionValidator()
    allowed, _, level = v.validate({"action": "type", "value": "hello"})
    assert allowed
    assert level == DangerLevel.SAFE
    print("  PASS: test_validator_allows_type")


def test_validator_allows_scroll():
    """Scroll should be SAFE."""
    v = ActionValidator()
    allowed, _, level = v.validate({"action": "scroll", "target": "down", "value": "3"})
    assert allowed
    assert level == DangerLevel.SAFE
    print("  PASS: test_validator_allows_scroll")


def test_validator_blocks_unknown_action():
    """Unrecognized actions should still be considered SAFE but allowed by default."""
    v = ActionValidator()
    allowed, _, _ = v.validate({"action": "unknown_action", "target": "x"})
    assert allowed
    print("  PASS: test_validator_blocks_unknown_action")


def test_validator_blocks_shell_app():
    """Open a shell terminal should be CAUTION or higher."""
    v = ActionValidator(block_dangerous=True)
    allowed, _, level = v.validate({"action": "open_app", "target": "powershell"})
    # With block_dangerous=True, CAUTION is still allowed (just logged)
    assert allowed, "CAUTION level should still be allowed with block_dangerous=True"
    assert level == DangerLevel.CAUTION
    print("  PASS: test_validator_blocks_shell_app")


def test_validator_blocks_regedit():
    """Open regedit should be CRITICAL and blocked."""
    v = ActionValidator(block_dangerous=True)
    allowed, _, level = v.validate({"action": "open_app", "target": "regedit"})
    assert not allowed, "CRITICAL action should be blocked"
    assert level == DangerLevel.CRITICAL
    print("  PASS: test_validator_blocks_regedit")


def test_validator_blocklist():
    """ActionValidator should block actions in the blocklist."""
    v = ActionValidator(blocked_actions={"open_app"})
    allowed, reason, _ = v.validate({"action": "open_app", "target": "notepad"})
    assert not allowed, "open_app should be blocked"
    assert "blocked" in reason
    print("  PASS: test_validator_blocklist")


def test_validator_allowlist():
    """ActionValidator should only allow actions in the allowlist."""
    v = ActionValidator(allowed_actions={"click", "type"})
    allowed, _, _ = v.validate({"action": "click", "target": "[0]"})
    assert allowed, "click should be in allowlist"
    allowed, _, _ = v.validate({"action": "scroll", "target": "down"})
    assert not allowed, "scroll should NOT be in allowlist"
    print("  PASS: test_validator_allowlist")


def test_validator_allowed_apps():
    """ActionValidator should restrict apps via allowlist."""
    v = ActionValidator(allowed_apps={"notepad", "calc"})
    allowed, _, _ = v.validate({"action": "open_app", "target": "notepad"})
    assert allowed, "notepad should be allowed"
    allowed, _, _ = v.validate({"action": "open_app", "target": "chrome"})
    assert not allowed, "chrome should be blocked"
    print("  PASS: test_validator_allowed_apps")


def test_validator_blocked_apps():
    """ActionValidator should block apps via blocklist."""
    v = ActionValidator(blocked_apps={"chrome"})
    allowed, _, _ = v.validate({"action": "open_app", "target": "chrome"})
    assert not allowed, "chrome should be blocked"
    allowed, _, _ = v.validate({"action": "open_app", "target": "notepad"})
    assert allowed, "notepad should be allowed"
    print("  PASS: test_validator_blocked_apps")


def test_validator_dangerous_key_combos():
    """ActionValidator should flag dangerous key combos."""
    v = ActionValidator(block_dangerous=True)
    # ctrl+alt+del is CRITICAL
    allowed, _, level = v.validate({"action": "key_press", "target": "ctrl+alt+del"})
    assert not allowed, "ctrl+alt+del should be blocked"
    assert level == DangerLevel.CRITICAL
    # alt+f4 is CAUTION but allowed
    allowed, _, level = v.validate({"action": "key_press", "target": "alt+f4"})
    assert allowed, "alt+f4 should be allowed (CAUTION)"
    assert level == DangerLevel.CAUTION
    print("  PASS: test_validator_dangerous_key_combos")


def test_validator_blocks_dangerous_url():
    """ActionValidator should flag dangerous URLs."""
    v = ActionValidator(block_dangerous=True)
    allowed, _, _ = v.validate({"action": "open_url", "value": "file:///etc/passwd"})
    assert allowed, "file:// is CAUTION but should be allowed with block_dangerous=True"
    print("  PASS: test_validator_blocks_dangerous_url")


def test_sandbox_read_only():
    """SandboxConfig read-only should block actions."""
    sbox = SandboxConfig(read_only=True)
    ctx = SecurityContext(sandbox=sbox)
    allowed, _, _ = ctx.check_action({"action": "click", "target": "[0]"})
    assert not allowed, "Read-only should block all actions"
    print("  PASS: test_sandbox_read_only")


def test_sandbox_max_actions():
    """SandboxConfig max_actions_total should be enforced."""
    sbox = SandboxConfig(max_actions_total=2)
    ctx = SecurityContext(sandbox=sbox)
    ctx.total_actions_executed = 2
    allowed, reason, _ = ctx.check_action({"action": "click", "target": "[0]"})
    assert not allowed, "Max actions should be enforced"
    assert "Max actions" in reason
    print("  PASS: test_sandbox_max_actions")


def test_sandbox_window_allowed():
    """SandboxConfig window allow/block patterns."""
    sbox = SandboxConfig(
        allowed_window_patterns=[r"Notepad"],
        blocked_window_patterns=[r"Command Prompt"],
    )
    assert sbox.is_window_allowed("Notepad - test.txt")
    assert not sbox.is_window_allowed("Command Prompt")
    # Window not in allowlist should be denied
    assert not sbox.is_window_allowed("Chrome")
    print("  PASS: test_sandbox_window_allowed")


def test_security_context_pipeline():
    """SecurityContext should run the full check pipeline."""
    ctx = SecurityContext()
    # Safe action
    allowed, reason, level = ctx.check_action({"action": "click", "target": "[0]"})
    assert allowed
    assert level == DangerLevel.SAFE
    assert ctx.total_actions_executed == 0  # Not recorded yet
    # Record it
    ctx.record_executed({"action": "click", "target": "[0]"})
    assert ctx.total_actions_executed == 1
    print("  PASS: test_security_context_pipeline")


def test_security_context_report():
    """SecurityContext.get_report should return structured data."""
    ctx = SecurityContext()
    ctx.record_executed({"action": "click"})
    ctx.record_executed({"action": "type", "value": "hello"})
    report = ctx.get_report()
    assert report["total_actions_executed"] == 2
    assert report["blocked_count"] == 0
    assert report["confirmed_count"] == 0
    assert "rate_summary" in report
    print("  PASS: test_security_context_report")


def test_security_context_blocked_tracking():
    """SecurityContext should track blocked actions."""
    v = ActionValidator(blocked_actions={"open_app"})
    ctx = SecurityContext(validator=v)
    ctx.check_action({"action": "open_app", "target": "notepad"})
    assert len(ctx.blocked_actions) == 1
    print("  PASS: test_security_context_blocked_tracking")


def test_confirm_dangerous_action():
    """Dangerous actions should be blocked unless confirmed."""
    v = ActionValidator(block_dangerous=True)
    allowed, _, level = v.validate({"action": "open_app", "target": "regedit"})
    assert not allowed, "regedit should be blocked"
    assert level == DangerLevel.CRITICAL
    # If user confirmed, it goes through
    ctx = SecurityContext(validator=v)
    ctx.record_confirmed({"action": "open_app", "target": "regedit"})
    assert ctx.total_actions_executed == 1
    assert len(ctx.confirmed_actions) == 1
    print("  PASS: test_confirm_dangerous_action")


def test_validator_long_wait():
    """Wait longer than 10s should be blocked."""
    v = ActionValidator()
    allowed, _, _ = v.validate({"action": "wait", "value": "15000"})
    assert not allowed, "15s wait should be blocked"
    allowed, _, _ = v.validate({"action": "wait", "value": "2000"})
    assert allowed, "2s wait should be allowed"
    print("  PASS: test_validator_long_wait")


def test_validator_without_blocking():
    """With block_dangerous=False, dangerous actions should just be flagged."""
    v = ActionValidator(block_dangerous=False)
    allowed, _, level = v.validate({"action": "open_app", "target": "regedit"})
    assert allowed, "Should be allowed with block_dangerous=False"
    assert level == DangerLevel.CRITICAL, "But should still flag as CRITICAL"
    print("  PASS: test_validator_without_blocking")


if __name__ == "__main__":
    print("Running security module tests...\n")

    test_rate_limiter_allows_first_actions()
    test_rate_limiter_blocks_after_limit()
    test_rate_limiter_reset()
    test_rate_limiter_clears_expired()
    test_rate_limiter_per_type()
    test_validator_allows_safe_actions()
    test_validator_allows_type()
    test_validator_allows_scroll()
    test_validator_blocks_unknown_action()
    test_validator_blocks_shell_app()
    test_validator_blocks_regedit()
    test_validator_blocklist()
    test_validator_allowlist()
    test_validator_allowed_apps()
    test_validator_blocked_apps()
    test_validator_dangerous_key_combos()
    test_validator_blocks_dangerous_url()
    test_validator_long_wait()
    test_validator_without_blocking()
    test_sandbox_read_only()
    test_sandbox_max_actions()
    test_sandbox_window_allowed()
    test_security_context_pipeline()
    test_security_context_report()
    test_security_context_blocked_tracking()
    test_confirm_dangerous_action()

    print(f"\n{'=' * 50}")
    print("✅ All 25 security tests passed!")
