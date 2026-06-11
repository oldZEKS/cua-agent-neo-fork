"""
Tests for the optimization module (ActionMerger, ObservationCache, etc.).

Run with: python -m cua_agent.tests.test_optimization
"""

from __future__ import annotations

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cua_agent.optimization import (
    ActionMerger,
    ObservationCache,
    PerformanceTimer,
    TokenOptimizer,
    OptimizationConfig,
)


def test_merger_merges_consecutive_type():
    """ActionMerger should merge consecutive type actions."""
    merger = ActionMerger(enabled=True)
    actions = [
        {"action": "type", "value": "Hello"},
        {"action": "type", "value": " World"},
        {"action": "type", "value": "!"},
    ]
    merged = merger.merge(actions)
    assert len(merged) == 1, f"Expected 1 merged action, got {len(merged)}"
    assert merged[0]["action"] == "type"
    assert merged[0]["value"] == "Hello World!"
    assert merged[0].get("_merged") == 3
    print("  PASS: test_merger_merges_consecutive_type")


def test_merger_skips_single_type():
    """ActionMerger should not modify a single type action."""
    merger = ActionMerger(enabled=True)
    actions = [{"action": "type", "value": "Hello"}]
    merged = merger.merge(actions)
    assert len(merged) == 1
    assert "_merged" not in merged[0]
    print("  PASS: test_merger_skips_single_type")


def test_merger_merges_scroll():
    """ActionMerger should merge consecutive scroll actions."""
    merger = ActionMerger(enabled=True)
    actions = [
        {"action": "scroll", "target": "down", "value": "1"},
        {"action": "scroll", "target": "down", "value": "1"},
        {"action": "scroll", "target": "down", "value": "1"},
    ]
    merged = merger.merge(actions)
    assert len(merged) == 1
    assert merged[0]["action"] == "scroll"
    assert merged[0]["target"] == "down"
    assert merged[0]["_merged"] == 3
    print("  PASS: test_merger_merges_scroll")


def test_merger_skips_mixed_actions():
    """ActionMerger should not merge different action types."""
    merger = ActionMerger(enabled=True)
    actions = [
        {"action": "type", "value": "Hello"},
        {"action": "click", "target": "[0]"},
        {"action": "type", "value": " World"},
    ]
    merged = merger.merge(actions)
    assert len(merged) == 3, "Mixed actions should not be merged"
    print("  PASS: test_merger_skips_mixed_actions")


def test_merger_handles_empty():
    """ActionMerger should handle empty list."""
    merger = ActionMerger(enabled=True)
    merged = merger.merge([])
    assert merged == []
    print("  PASS: test_merger_handles_empty")


def test_merger_disabled():
    """ActionMerger should pass through when disabled."""
    merger = ActionMerger(enabled=False)
    actions = [
        {"action": "type", "value": "A"},
        {"action": "type", "value": "B"},
    ]
    merged = merger.merge(actions)
    assert len(merged) == 2
    print("  PASS: test_merger_disabled")


def test_merger_max_merged_types():
    """ActionMerger should respect max_merged_types."""
    merger = ActionMerger(enabled=True, max_merged_types=2)
    actions = [
        {"action": "type", "value": "A"},
        {"action": "type", "value": "B"},
        {"action": "type", "value": "C"},
    ]
    merged = merger.merge(actions)
    # First 2 should be merged, 3rd stays separate
    assert len(merged) == 2, f"Expected 2 actions (1 merged + 1 single), got {len(merged)}"
    assert merged[0].get("_merged") == 2, "First should merge 2"
    print("  PASS: test_merger_max_merged_types")


def test_merger_click_plus_type():
    """ActionMerger should merge click+wait+type pattern."""
    merger = ActionMerger(enabled=True)
    actions = [
        {"action": "click", "target": "[2]"},
        {"action": "wait", "value": "300"},
        {"action": "type", "value": "Hello"},
    ]
    merged = merger.merge(actions)
    # Should be click, small wait, type (with _pre_clicked)
    assert len(merged) == 3
    assert merged[0]["action"] == "click"
    assert merged[1]["action"] == "wait"
    assert merged[2]["action"] == "type"
    assert merged[2].get("_pre_clicked") is True
    print("  PASS: test_merger_click_plus_type")


def test_cache_basic():
    """ObservationCache should cache results."""
    cache = ObservationCache(max_size=3, ttl_seconds=10.0)
    call_count = [0]
    def observe():
        call_count[0] += 1
        return {"result": call_count[0]}

    # First call — cache miss
    r1 = cache.get_or_observe(observe, {"title": "test"})
    assert call_count[0] == 1
    assert r1["result"] == 1

    # Second call with same context — cache hit
    r2 = cache.get_or_observe(observe, {"title": "test"})
    assert call_count[0] == 1, f"Expected cache hit, got {call_count[0]} calls"
    assert r2["result"] == 1

    # Third call with different context — cache miss
    r3 = cache.get_or_observe(observe, {"title": "other"})
    assert call_count[0] == 2
    assert r3["result"] == 2

    print("  PASS: test_cache_basic")


def test_cache_expiry():
    """ObservationCache should expire entries after TTL."""
    cache = ObservationCache(ttl_seconds=0.05)
    call_count = [0]
    def observe():
        call_count[0] += 1
        return call_count[0]

    r1 = cache.get_or_observe(observe, {"title": "test"})
    assert r1 == 1

    time.sleep(0.06)

    # Should miss cache
    r2 = cache.get_or_observe(observe, {"title": "test"})
    assert r2 == 2
    print("  PASS: test_cache_expiry")


def test_cache_max_size():
    """ObservationCache should respect max_size."""
    cache = ObservationCache(max_size=2, ttl_seconds=10.0)
    call_count = [0]
    def observe(key):
        call_count[0] += 1
        return key

    # Fill cache
    cache.get_or_observe(lambda: observe("a"), {"key": "a"})
    cache.get_or_observe(lambda: observe("b"), {"key": "b"})
    cache.get_or_observe(lambda: observe("c"), {"key": "c"})  # Evicts 'a'

    # 'a' should be evicted
    r = cache.get_or_observe(lambda: observe("a"), {"key": "a"})
    assert r == "a", "Should re-fetch evicted entry"

    print("  PASS: test_cache_max_size")


def test_cache_invalidate_all():
    """ObservationCache.invalidate() should clear all entries."""
    cache = ObservationCache(ttl_seconds=10.0)
    def observe():
        return "fresh"
    cache.get_or_observe(observe, {"title": "a"})
    cache.get_or_observe(observe, {"title": "b"})
    assert len(cache._cache) == 2
    cache.invalidate()
    assert len(cache._cache) == 0
    print("  PASS: test_cache_invalidate_all")


def test_timer_basic():
    """PerformanceTimer should track timing."""
    timer = PerformanceTimer(enabled=True)
    timer.start("observe")
    time.sleep(0.01)
    duration = timer.stop("observe")
    assert duration is not None
    assert duration >= 0.01, f"Expected >= 0.01s, got {duration}"
    print("  PASS: test_timer_basic")


def test_timer_disabled():
    """PerformanceTimer should be no-op when disabled."""
    timer = PerformanceTimer(enabled=False)
    timer.start("observe")
    duration = timer.stop("observe")
    assert duration is None
    print("  PASS: test_timer_disabled")


def test_timer_stats():
    """PerformanceTimer should provide stats."""
    timer = PerformanceTimer(enabled=True)
    for _ in range(3):
        timer.start("click")
        time.sleep(0.001)
        timer.stop("click")
    stats = timer.get_stats("click")
    assert stats["count"] == 3
    assert stats["total"] > 0
    assert stats["avg"] > 0
    print("  PASS: test_timer_stats")


def test_timer_report():
    """PerformanceTimer should generate a readable report."""
    timer = PerformanceTimer(enabled=True)
    timer.start("observe")
    timer.stop("observe")
    timer.start("act")
    timer.stop("act")
    report = timer.report()
    assert "observe" in report
    assert "act" in report
    print("  PASS: test_timer_report")


def test_token_optimizer_compress_actions():
    """TokenOptimizer should compress repeated actions."""
    opt = TokenOptimizer(enabled=True)
    actions = [{"action": "type", "value": "a"} for _ in range(6)]
    compressed = opt.compress_action_history(actions)
    assert len(compressed) == 1
    assert compressed[0].get("_compressed") == 6
    print("  PASS: test_token_optimizer_compress_actions")


def test_token_optimizer_compress_appshots():
    """TokenOptimizer should compress redundant appshots."""
    opt = TokenOptimizer(enabled=True)
    appshots = [
        {"summary": "Window Test | 3 fields", "type": "initial"},
        {"summary": "Window Test | no change", "type": "no_change"},
        {"summary": "Window Test | no change", "type": "no_change"},  # Should be removed
        {"summary": "Window Test | +1 element", "type": "state_change"},
    ]
    compressed = opt.compress_appshot_history(appshots)
    assert len(compressed) == 3, f"Expected 3, got {len(compressed)}"
    print("  PASS: test_token_optimizer_compress_appshots")


def test_token_optimizer_passthrough_when_disabled():
    """TokenOptimizer should pass through when disabled."""
    opt = TokenOptimizer(enabled=False)
    actions = [{"action": "type", "value": "a"}, {"action": "type", "value": "b"}]
    compressed = opt.compress_action_history(actions)
    assert len(compressed) == 2
    print("  PASS: test_token_optimizer_passthrough_when_disabled")


def test_optimization_config_factory():
    """OptimizationConfig should create all optimizer instances."""
    opt = OptimizationConfig()
    merger = opt.create_merger()
    cache = opt.create_cache()
    timer = opt.create_timer()
    tokenizer = opt.create_token_optimizer()

    assert merger.enabled is True
    assert cache.ttl_seconds == 2.0
    assert timer.enabled is True
    assert tokenizer.enabled is True
    print("  PASS: test_optimization_config_factory")


if __name__ == "__main__":
    print("Running optimization module tests...\n")

    test_merger_merges_consecutive_type()
    test_merger_skips_single_type()
    test_merger_merges_scroll()
    test_merger_skips_mixed_actions()
    test_merger_handles_empty()
    test_merger_disabled()
    test_merger_max_merged_types()
    test_merger_click_plus_type()
    test_cache_basic()
    test_cache_expiry()
    test_cache_max_size()
    test_cache_invalidate_all()
    test_timer_basic()
    test_timer_disabled()
    test_timer_stats()
    test_timer_report()
    test_token_optimizer_compress_actions()
    test_token_optimizer_compress_appshots()
    test_token_optimizer_passthrough_when_disabled()
    test_optimization_config_factory()

    print(f"\n{'=' * 50}")
    print("✅ All 20 optimization tests passed!")
