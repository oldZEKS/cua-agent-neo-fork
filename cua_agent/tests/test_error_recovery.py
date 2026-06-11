"""
Tests for the error recovery module.

Run with: python -m cua_agent.tests.test_error_recovery
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cua_agent.error_recovery import (
    ErrorRecovery,
    RetryPolicy,
    ErrorCategory,
    ErrorRecord,
    generate_alternative_action,
)


def test_classify_element_not_found():
    """Should classify element_not_found errors."""
    er = ErrorRecovery()
    cat = er.classify_error(
        {"action": "click", "target": "[5]"},
        {"success": False, "error": "Element not found: [5]"},
    )
    assert cat == ErrorCategory.ELEMENT_NOT_FOUND
    print("  PASS: test_classify_element_not_found")


def test_classify_permission():
    """Should classify permission errors."""
    er = ErrorRecovery()
    cat = er.classify_error(
        {"action": "click"},
        {"success": False, "error": "Access denied: permission not granted"},
    )
    assert cat == ErrorCategory.PERMISSION_DENIED
    print("  PASS: test_classify_permission")


def test_classify_timeout():
    """Should classify timeout errors."""
    er = ErrorRecovery()
    cat = er.classify_error(
        {"action": "wait"},
        {"success": False, "error": "Timed out waiting for element to appear"},
    )
    assert cat == ErrorCategory.TIMEOUT
    print("  PASS: test_classify_timeout")


def test_classify_app_not_responding():
    """Should classify not responding errors."""
    er = ErrorRecovery()
    cat = er.classify_error(
        {"action": "click"},
        {"success": False, "error": "Application is not responding"},
    )
    assert cat == ErrorCategory.APP_NOT_RESPONDING
    print("  PASS: test_classify_app_not_responding")


def test_classify_rate_limited():
    """Should classify rate limit errors."""
    er = ErrorRecovery()
    cat = er.classify_error(
        {"action": "click"},
        {"success": False, "error": "Rate limit exceeded. Please slow down."},
    )
    assert cat == ErrorCategory.RATE_LIMITED
    print("  PASS: test_classify_rate_limited")


def test_classify_invalid_action():
    """Should classify invalid action errors."""
    er = ErrorRecovery()
    cat = er.classify_error(
        {"action": "unknown_action"},
        {"success": False, "error": "Unknown action: unknown_action"},
    )
    assert cat == ErrorCategory.INVALID_ACTION
    print("  PASS: test_classify_invalid_action")


def test_classify_unknown():
    """Should classify unrecognized errors as UNKNOWN."""
    er = ErrorRecovery()
    cat = er.classify_error(
        {"action": "click"},
        {"success": False, "error": "Something weird happened"},
    )
    assert cat == ErrorCategory.UNKNOWN
    print("  PASS: test_classify_unknown")


def test_classify_state_mismatch():
    """Should classify state_mismatch."""
    er = ErrorRecovery()
    cat = er.classify_error(
        {"action": "click"},
        {"success": False, "error": "Unknown error", "state_mismatch": True},
    )
    assert cat == ErrorCategory.STATE_MISMATCH
    print("  PASS: test_classify_state_mismatch")


def test_retry_policy_for_category():
    """RetryPolicy.for_category should return category-specific policies."""
    policies = {
        ErrorCategory.ELEMENT_NOT_FOUND: RetryPolicy.for_category(ErrorCategory.ELEMENT_NOT_FOUND),
        ErrorCategory.PERMISSION_DENIED: RetryPolicy.for_category(ErrorCategory.PERMISSION_DENIED),
        ErrorCategory.INVALID_ACTION: RetryPolicy.for_category(ErrorCategory.INVALID_ACTION),
    }
    assert policies[ErrorCategory.ELEMENT_NOT_FOUND].max_retries == 2
    assert policies[ErrorCategory.PERMISSION_DENIED].max_retries == 1
    assert policies[ErrorCategory.PERMISSION_DENIED].use_alternative is False
    assert policies[ErrorCategory.INVALID_ACTION].max_retries == 0
    print("  PASS: test_retry_policy_for_category")


def test_generate_alternative_for_click():
    """generate_alternative_action should produce alternatives for click."""
    alts = generate_alternative_action({"action": "click", "target": "[0]"})
    assert len(alts) >= 1
    assert alts[0]["action"] in ("wait", "click")
    print("  PASS: test_generate_alternative_for_click")


def test_generate_alternative_for_type():
    """generate_alternative_action should produce alternatives for type."""
    alts = generate_alternative_action({"action": "type", "target": "[2]", "value": "hello"})
    assert len(alts) >= 1
    assert alts[0]["action"] == "click"
    print("  PASS: test_generate_alternative_for_type")


def test_generate_alternative_for_scroll():
    """generate_alternative_action should produce opposite direction scroll."""
    alts = generate_alternative_action({"action": "scroll", "target": "down", "value": "3"})
    assert len(alts) >= 1
    assert alts[0]["action"] == "scroll"
    assert alts[0]["target"] == "up"
    print("  PASS: test_generate_alternative_for_scroll")


def test_handle_error_simple_retry():
    """handle_error should return a copy of the action for simple retry."""
    er = ErrorRecovery()
    recovery = er.handle_error(
        step=1,
        action={"action": "click", "target": "[0]"},
        result={"success": False, "error": "Element not found"},
    )
    assert recovery is not None
    assert recovery["action"] == "click"
    assert recovery["target"] == "[0]"
    print("  PASS: test_handle_error_simple_retry")


def test_handle_error_fatal_too_many():
    """handle_error should return None when max consecutive errors exceeded."""
    er = ErrorRecovery(max_consecutive_errors=2)
    er.consecutive_errors = 2
    recovery = er.handle_error(
        step=1,
        action={"action": "click"},
        result={"success": False, "error": "Element not found"},
    )
    assert recovery is None
    print("  PASS: test_handle_error_fatal_too_many")


def test_record_success():
    """record_success should reset consecutive error counter."""
    er = ErrorRecovery()
    er.consecutive_errors = 5
    er.record_success({"action": "click"}, {"success": True})
    assert er.consecutive_errors == 0
    print("  PASS: test_record_success")


def test_handle_error_exhausts_retries():
    """handle_error should return None when category retries exceeded."""
    er = ErrorRecovery()
    # Simulate many ELEMENT_NOT_FOUND errors
    for i in range(3):
        recovery = er.handle_error(
            step=i + 1,
            action={"action": "click", "target": f"[{i}]"},
            result={"success": False, "error": "Element not found"},
        )
        if i < 2:
            assert recovery is not None, f"Step {i+1} should still allow retry"
        else:
            # After 3 errors with max_retries=2, should be fatal
            assert recovery is None, f"Step {i+1} should be exhausted"
    print("  PASS: test_handle_error_exhausts_retries")


def test_error_record():
    """ErrorRecord should work as a dataclass."""
    record = ErrorRecord(
        step=1,
        action={"action": "click"},
        error_message="Test error",
        category=ErrorCategory.UNKNOWN,
    )
    assert record.step == 1
    assert record.category == ErrorCategory.UNKNOWN
    assert record.timestamp > 0
    print("  PASS: test_error_record")


def test_error_summary():
    """get_error_summary should return formatted text."""
    er = ErrorRecovery()
    assert "No errors" in er.get_error_summary()
    er.handle_error(1, {"action": "click"}, {"success": False, "error": "Not found"})
    summary = er.get_error_summary()
    assert "Recent errors" in summary
    assert "element_not_found" in summary
    print("  PASS: test_error_summary")


def test_check_state_integrity_empty():
    """check_state_integrity should pass with empty state."""
    er = ErrorRecovery()
    valid, reason = er.check_state_integrity({"role": "A"}, {"role": "B", "children": []})
    assert valid
    assert "passed" in reason
    print("  PASS: test_check_state_integrity_empty")


if __name__ == "__main__":
    print("Running error recovery tests...\n")

    test_classify_element_not_found()
    test_classify_permission()
    test_classify_timeout()
    test_classify_app_not_responding()
    test_classify_rate_limited()
    test_classify_invalid_action()
    test_classify_unknown()
    test_classify_state_mismatch()
    test_retry_policy_for_category()
    test_generate_alternative_for_click()
    test_generate_alternative_for_type()
    test_generate_alternative_for_scroll()
    test_handle_error_simple_retry()
    test_handle_error_fatal_too_many()
    test_record_success()
    test_handle_error_exhausts_retries()
    test_error_record()
    test_error_summary()
    test_check_state_integrity_empty()

    print(f"\n{'=' * 50}")
    print("✅ All 19 error recovery tests passed!")
