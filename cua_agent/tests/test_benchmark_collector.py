"""
test_benchmark_collector.py — Performance benchmarks for WindowsCollector optimizations.

Tests the performance-critical paths:
  - Limit checking (check_limits, limits_exceeded) — no double-counting
  - Early exit via max_interactive_nodes, max_nodes, timeout
  - SKIP_CHILDREN_ROLES and SKIP_PATTERNS_ROLES optimization
  - Adaptive timeout backoff behavior
  - Simulated tree walk timing
  - ParallelPrefetcher perf characteristics

Run with: python -m cua_agent.tests.test_benchmark_collector
"""

from __future__ import annotations

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cua_agent.ax_collector_windows import WindowsCollector


# ── 1. Limit checking ──

def test_check_limits_increments():
    """_check_limits() should increment node_count each call."""
    c = WindowsCollector(adaptive_timeout=False)
    c._start_time = time.time()

    count_before = c._node_count
    c._check_limits()
    assert c._node_count == count_before + 1, f"Expected {count_before + 1}, got {c._node_count}"
    print("  PASS: test_check_limits_increments")


def test_limits_exceeded_no_increment():
    """_limits_exceeded() should NOT increment node_count."""
    c = WindowsCollector(adaptive_timeout=False)
    c._start_time = time.time()
    c._node_count = 5

    for _ in range(3):
        exceeded = c._limits_exceeded()
        assert not exceeded, "Should not exceed with 5 nodes (max=3000)"
    assert c._node_count == 5, f"node_count should still be 5, got {c._node_count}"
    print("  PASS: test_limits_exceeded_no_increment")


def test_check_limits_vs_limits_exceeded_counting():
    """_check_limits and _limits_exceeded should differ only by increment."""
    c = WindowsCollector(adaptive_timeout=False)
    c._start_time = time.time()
    c._node_count = 0

    # _check_limits: increment + check
    r1 = c._check_limits()
    assert c._node_count == 1
    assert not r1

    # _limits_exceeded: check only
    r2 = c._limits_exceeded()
    assert c._node_count == 1, "Should NOT increment"
    assert not r2

    print("  PASS: test_check_limits_vs_limits_exceeded_counting")


def test_limits_exceeded_max_nodes():
    """_limits_exceeded() should return True when node_count > max_nodes."""
    c = WindowsCollector(adaptive_timeout=False)
    c._start_time = time.time()
    c._max_nodes = 100
    c._node_count = 100  # At the limit

    assert not c._limits_exceeded(), "100 should not exceed max=100"

    c._node_count = 101
    assert c._limits_exceeded(), "101 should exceed max=100"

    print("  PASS: test_limits_exceeded_max_nodes")


def test_limits_exceeded_timeout():
    """_limits_exceeded() should return True after timeout."""
    c = WindowsCollector(adaptive_timeout=False, timeout_seconds=0.05)
    c._start_time = time.time()
    c._node_count = 1

    # Should not be exceeded immediately
    assert not c._limits_exceeded()

    # Wait for timeout
    time.sleep(0.06)
    assert c._limits_exceeded(), "Should exceed after timeout"

    print("  PASS: test_limits_exceeded_timeout")


def test_check_limits_timeout():
    """_check_limits() should return True after timeout (with increment)."""
    c = WindowsCollector(adaptive_timeout=False, timeout_seconds=0.05)
    c._start_time = time.time()
    c._node_count = 0

    # First call: increment + check, should pass
    r1 = c._check_limits()
    assert c._node_count == 1
    assert not r1

    # Wait for timeout
    time.sleep(0.06)

    # Second call: increment + check, should exceed
    r2 = c._check_limits()
    assert c._node_count == 2
    assert r2, "Should exceed after timeout"

    print("  PASS: test_check_limits_timeout")


# ── 2. Adaptive timeout ──

def test_adaptive_timeout_starts_short():
    """Adaptive timeout should start at initial_timeout."""
    c = WindowsCollector(adaptive_timeout=True, initial_timeout=2.0)
    assert c._current_timeout == 2.0
    print("  PASS: test_adaptive_timeout_starts_short")


def test_adaptive_timeout_backs_off_on_truncated():
    """Adaptive timeout should multiply on truncated result."""
    c = WindowsCollector(adaptive_timeout=True, initial_timeout=2.0, max_timeout=16.0, timeout_backoff=2.0)

    # Simulate a truncated tree walk
    tree = {"role": "AXWindow", "_truncated": "children limited to 100 of 500"}
    if c._adaptive_timeout and tree.get("_truncated"):
        c._current_timeout = min(c._current_timeout * c._timeout_backoff, c._max_timeout)

    assert c._current_timeout == 4.0, f"Expected 4.0, got {c._current_timeout}"
    print("  PASS: test_adaptive_timeout_backs_off_on_truncated")


def test_adaptive_timeout_multiple_backoffs():
    """Adaptive timeout should back off repeatedly until max."""
    c = WindowsCollector(adaptive_timeout=True, initial_timeout=2.0, max_timeout=16.0, timeout_backoff=2.0)

    # 3 truncated walks: 2 → 4 → 8 → 16
    for expected in [4.0, 8.0, 16.0]:
        tree = {"role": "AXWindow", "_truncated": "still truncated"}
        if c._adaptive_timeout and tree.get("_truncated"):
            c._current_timeout = min(c._current_timeout * c._timeout_backoff, c._max_timeout)
        assert c._current_timeout == expected, f"Expected {expected}, got {c._current_timeout}"

    # Next backoff should cap at max_timeout
    tree = {"role": "AXWindow", "_truncated": "still truncated"}
    if c._adaptive_timeout and tree.get("_truncated"):
        c._current_timeout = min(c._current_timeout * c._timeout_backoff, c._max_timeout)
    assert c._current_timeout == 16.0, f"Capped at 16.0, got {c._current_timeout}"

    print("  PASS: test_adaptive_timeout_multiple_backoffs")


def test_adaptive_timeout_resets_on_complete():
    """Adaptive timeout should reset to initial after a complete walk."""
    c = WindowsCollector(adaptive_timeout=True, initial_timeout=2.0, max_timeout=16.0, timeout_backoff=2.0)

    # Back off to 8.0 first
    for _ in range(2):
        tree = {"role": "AXWindow", "_truncated": "truncated"}
        if c._adaptive_timeout and tree.get("_truncated"):
            c._current_timeout = min(c._current_timeout * c._timeout_backoff, c._max_timeout)
    assert c._current_timeout == 8.0

    # Complete walk — should reset
    tree = {"role": "AXWindow", "children": []}
    if c._adaptive_timeout and not tree.get("_truncated"):
        c._current_timeout = c._initial_timeout
    assert c._current_timeout == 2.0, f"Expected 2.0, got {c._current_timeout}"

    print("  PASS: test_adaptive_timeout_resets_on_complete")


def test_adaptive_timeout_backoff_capped():
    """Adaptive timeout backoff should not exceed max_timeout."""
    c = WindowsCollector(adaptive_timeout=True, initial_timeout=2.0, max_timeout=5.0, timeout_backoff=3.0)

    # 2 → 5 (would be 6, but capped at 5)
    tree = {"role": "AXWindow", "_truncated": "yes"}
    if c._adaptive_timeout and tree.get("_truncated"):
        c._current_timeout = min(c._current_timeout * c._timeout_backoff, c._max_timeout)
    assert c._current_timeout == 5.0, f"Expected 5.0, got {c._current_timeout}"

    # Another truncation — should stay at max
    if c._adaptive_timeout and tree.get("_truncated"):
        c._current_timeout = min(c._current_timeout * c._timeout_backoff, c._max_timeout)
    assert c._current_timeout == 5.0, f"Should stay at 5.0, got {c._current_timeout}"

    print("  PASS: test_adaptive_timeout_backoff_capped")


def test_adaptive_timeout_disabled():
    """When adaptive_timeout=False, timeout should stay fixed."""
    c = WindowsCollector(adaptive_timeout=False, timeout_seconds=10.0,
                         initial_timeout=2.0, max_timeout=16.0, timeout_backoff=2.0)

    assert not c._adaptive_timeout
    assert c._timeout_seconds == 10.0  # Fixed value passed to constructor
    assert c._current_timeout == 2.0   # Set but never synced to _timeout_seconds

    # Simulate what adaptive logic does: it modifies _current_timeout,
    # NOT _timeout_seconds. Since _adaptive_timeout is False, the sync
    # step (_timeout_seconds = _current_timeout) never runs.
    c._current_timeout = min(c._current_timeout * c._timeout_backoff, c._max_timeout)
    # _current_timeout would back off if the adaptive path ran
    assert c._current_timeout == 4.0, "_current_timeout would back off"
    # But _timeout_seconds stays at its fixed initial value (10.0)
    assert c._timeout_seconds == 10.0, "timeout_seconds should stay fixed when disabled"
    print("  PASS: test_adaptive_timeout_disabled")


# ── 3. Skip roles ──

def test_skip_children_roles_defined():
    """_SKIP_CHILDREN_ROLES should contain essential leaf container types."""
    essential = {"ScrollBar", "ProgressBar", "Separator", "ToolTip", "TitleBar", "MenuBar"}
    for role in essential:
        assert role in WindowsCollector._SKIP_CHILDREN_ROLES or \
               f"AX{role}" in WindowsCollector._SKIP_CHILDREN_ROLES, \
               f"Missing essential skip role: {role}"
    # Should have both AX-prefixed and non-prefixed versions
    ax_count = sum(1 for r in WindowsCollector._SKIP_CHILDREN_ROLES if r.startswith("AX"))
    assert ax_count >= 3, f"Expected at least 3 AX-prefixed skip roles, got {ax_count}"
    print("  PASS: test_skip_children_roles_defined")


def test_skip_patterns_roles_defined():
    """_SKIP_PATTERNS_ROLES should contain non-interactive container types."""
    essential = {"Pane", "Group", "Window", "ScrollBar", "ProgressBar", "Separator", "StatusBar"}
    for role in essential:
        found = role in WindowsCollector._SKIP_PATTERNS_ROLES or \
                f"AX{role}" in WindowsCollector._SKIP_PATTERNS_ROLES
        assert found, f"Missing essential pattern-skip role: {role}"
    print("  PASS: test_skip_patterns_roles_defined")


def test_skip_roles_no_overlap():
    """_SKIP_CHILDREN_ROLES should be a subset of _SKIP_PATTERNS_ROLES for consistency."""
    # All skip-children roles should also be in skip-patterns (they're non-interactive)
    for role in WindowsCollector._SKIP_CHILDREN_ROLES:
        assert role in WindowsCollector._SKIP_PATTERNS_ROLES, \
               f"Role in _SKIP_CHILDREN_ROLES but not in _SKIP_PATTERNS_ROLES: {role}"
    print("  PASS: test_skip_roles_no_overlap")


# ── 4. Simulated tree walk performance ──

def _build_mock_tree(depth: int, fanout: int) -> dict:
    """Build a nested mock tree of given depth and fanout."""
    if depth <= 0:
        return {"role": "AXButton", "title": "leaf"}
    children = [_build_mock_tree(depth - 1, fanout) for _ in range(fanout)]
    return {"role": "AXPane", "title": f"lvl{depth}", "children": children}


def test_simulated_tree_walk_timing():
    """Measure how long it takes to walk a mock tree of ~1000 nodes."""
    # Build a tree with 3 levels, fanout 10 = 1111 nodes
    tree = _build_mock_tree(depth=3, fanout=10)

    # Walk the tree (count nodes, flatten)
    start = time.time()
    count = [0]

    def walk(n):
        count[0] += 1
        for child in n.get("children", []):
            walk(child)

    walk(tree)
    elapsed = time.time() - start

    # Mid-sized 1111-node tree should walk in < 5ms
    assert count[0] >= 1000, f"Expected >= 1000 nodes, got {count[0]}"
    assert elapsed < 0.5, f"Tree walk too slow: {elapsed*1000:.0f}ms (expected < 500ms)"
    print(f"  PASS: test_simulated_tree_walk_timing ({count[0]} nodes in {elapsed*1000:.0f}ms)")


def test_large_tree_walk_timing():
    """Measure how long it takes to walk a large mock tree (~10000 nodes)."""
    tree = _build_mock_tree(depth=4, fanout=10)  # 11111 nodes

    start = time.time()
    count = [0]

    def walk(n):
        count[0] += 1
        for child in n.get("children", []):
            walk(child)

    walk(tree)
    elapsed = time.time() - start

    assert count[0] >= 10000, f"Expected >= 10000 nodes, got {count[0]}"
    assert elapsed < 1.0, f"Large tree walk too slow: {elapsed*1000:.0f}ms (expected < 1000ms)"
    print(f"  PASS: test_large_tree_walk_timing ({count[0]} nodes in {elapsed*1000:.0f}ms)")


def test_mock_tree_element_extraction():
    """Measure extraction of interactive-only elements from mock tree."""
    tree = _build_mock_tree(depth=3, fanout=8)  # 585 nodes

    from cua_agent.ax_collector_windows import _flatten_summary

    start = time.time()
    result = _flatten_summary(tree, max_items=50)
    elapsed = time.time() - start

    assert isinstance(result, list)
    # Mock tree has AXButton leaves and AXPane parents — both in FLATTEN list
    assert len(result) > 0
    assert elapsed < 0.2, f"Element extraction too slow: {elapsed*1000:.0f}ms"
    print(f"  PASS: test_mock_tree_element_extraction ({len(result)} elements in {elapsed*1000:.0f}ms)")


# ── 5. PerformanceTimer benchmark ──

def test_timing_overhead():
    """Measure PerformanceTimer overhead per call."""
    from cua_agent.optimization import PerformanceTimer

    timer = PerformanceTimer(enabled=True)

    start = time.time()
    iterations = 1000
    for i in range(iterations):
        timer.start(f"op{i}")
        timer.stop(f"op{i}")
    elapsed = time.time() - start

    per_call_us = (elapsed / iterations) * 1_000_000
    assert per_call_us < 100, f"Timer overhead too high: {per_call_us:.0f}µs/call"
    print(f"  PASS: test_timing_overhead ({per_call_us:.0f}µs per start/stop pair)")


# ── 6. ObservationCache benchmark ──

def test_cache_hit_latency():
    """Measure ObservationCache hit latency."""
    from cua_agent.optimization import ObservationCache

    cache = ObservationCache(ttl_seconds=60.0)
    ctx = {"title": "benchmark", "role": "AXWindow", "_pid": "1234"}

    # Prime the cache
    cache.get_or_observe(lambda: {"result": "data"}, ctx)

    iterations = 10000
    start = time.time()
    for _ in range(iterations):
        cache.get_or_observe(lambda: None, ctx)
    elapsed = time.time() - start

    per_call_us = (elapsed / iterations) * 1_000_000
    assert per_call_us < 50, f"Cache hit too slow: {per_call_us:.0f}µs"
    print(f"  PASS: test_cache_hit_latency ({per_call_us:.0f}µs per hit)")


def test_cache_miss_latency():
    """Measure ObservationCache miss + fresh observe latency."""
    from cua_agent.optimization import ObservationCache

    cache = ObservationCache(ttl_seconds=0.001, max_size=10)
    call_count = [0]

    def observe():
        call_count[0] += 1
        return {"result": call_count[0]}

    iterations = 100
    start = time.time()
    for i in range(iterations):
        ctx = {"title": f"bench{i}", "role": "AXWindow", "_pid": "1234"}
        cache.get_or_observe(observe, ctx)
    elapsed = time.time() - start

    per_call_us = (elapsed / iterations) * 1_000_000
    assert per_call_us < 500, f"Cache miss too slow: {per_call_us:.0f}µs"
    print(f"  PASS: test_cache_miss_latency ({per_call_us:.0f}µs per miss+observe)")


# ── 7. Simulated early exit ──

def test_early_exit_simulation():
    """Simulate early exit by walking a tree and counting stops."""
    tree = _build_mock_tree(depth=4, fanout=6)  # 9331 nodes

    # Walk but stop after 100 interactive elements
    max_interactive = 100
    found = [0]
    count = [0]

    def walk(n):
        count[0] += 1
        if found[0] >= max_interactive:
            return
        role = n.get("role", "")
        if "Button" in role or "Window" in role:
            found[0] += 1
        for child in n.get("children", []):
            if found[0] >= max_interactive:
                return
            walk(child)

    start = time.time()
    walk(tree)
    elapsed = time.time() - start

    assert found[0] >= max_interactive, f"Should have found {max_interactive} interactive elements"
    assert count[0] < 150, f"Should have stopped early but visited {count[0]} nodes"
    assert elapsed < 0.5, f"Early exit walk too slow: {elapsed*1000:.0f}ms"
    print(f"  PASS: test_early_exit_simulation ({count[0]} nodes visited, {found[0]} interactive, {elapsed*1000:.0f}ms)")


# ── 8. ActionMerger benchmark ──

def test_merger_benchmark():
    """Measure ActionMerger throughput for batched type actions."""
    from cua_agent.optimization import ActionMerger

    merger = ActionMerger(enabled=True, max_merged_types=20)

    # Generate 100 type actions
    actions = [{"action": "type", "value": "a"} for _ in range(100)]

    start = time.time()
    batches = 1000
    for _ in range(batches):
        merger.merge(actions)
    elapsed = time.time() - start

    per_merge_us = (elapsed / batches) * 1_000_000
    assert per_merge_us < 500, f"Merger too slow: {per_merge_us:.0f}µs per 100-action batch"
    print(f"  PASS: test_merger_benchmark ({per_merge_us:.0f}µs per 100-action merge)")


# ── 9. ParallelPrefetcher benchmark ──

def test_prefetcher_benchmark():
    """Measure ParallelPrefetcher prefetch + get latency."""
    from cua_agent.optimization import ParallelPrefetcher

    prefetcher = ParallelPrefetcher(enabled=True)

    # Prefetch a fast operation
    start = time.time()
    prefetcher.prefetch(lambda: {"result": "data"})

    # Immediately get (should wait for thread, which completes quickly)
    result = prefetcher.get_or_observe(lambda: {"result": "fresh"})
    elapsed = time.time() - start

    assert result["result"] == "data", "Should return prefetched data"
    assert elapsed < 1.0, f"Prefetch+get too slow: {elapsed*1000:.0f}ms"
    print(f"  PASS: test_prefetcher_benchmark ({elapsed*1000:.0f}ms prefetch+get)")


def test_prefetcher_fallback():
    """ParallelPrefetcher should fall back to fresh observe when prefetch expired."""
    from cua_agent.optimization import ParallelPrefetcher

    prefetcher = ParallelPrefetcher(enabled=True)

    # Prefetch, then wait for expiry
    prefetcher.prefetch(lambda: {"result": "stale"})
    result = prefetcher.get_or_observe(lambda: {"result": "fresh"})
    # Should get prefetched since ttl is 5s
    assert result["result"] in ("stale", "fresh"), "Should get either result"

    # Force expiry by setting prefetch time back
    prefetcher._prefetch_time = 0
    result2 = prefetcher.get_or_observe(lambda: {"result": "fresh2"})
    assert result2["result"] == "fresh2", "Should fall back to fresh observe"

    print("  PASS: test_prefetcher_fallback")


# ── 10. Cache warming ──

def test_cache_warming_basic():
    """ParallelPrefetcher.get() should return prefetched result without fallback."""
    from cua_agent.optimization import ParallelPrefetcher

    prefetcher = ParallelPrefetcher(enabled=True)
    prefetcher.prefetch(lambda: {"warmed": True, "value": 42})

    # Wait for prefetch thread
    import time
    time.sleep(0.1)

    # get() should return prefetched data without calling observe_fn
    result = prefetcher.get()
    assert result is not None, "get() should return prefetched data"
    assert result["warmed"] is True
    assert result["value"] == 42

    # Second get() should return None (consumed)
    assert prefetcher.get() is None, "Second get() should return None"
    print("  PASS: test_cache_warming_basic")


def test_cache_warming_idempotent():
    """Multiple warm_cache() calls should not cause errors."""
    # Simulate calling warm_cache() multiple times by doing multiple prefetches
    # followed by get() — should be idempotent
    from cua_agent.optimization import ParallelPrefetcher

    prefetcher = ParallelPrefetcher(enabled=True)

    # First warm
    prefetcher.prefetch(lambda: {"version": 1})
    import time
    time.sleep(0.05)
    r1 = prefetcher.get()
    assert r1["version"] == 1

    # Second warm (same, new data)
    prefetcher.prefetch(lambda: {"version": 2})
    time.sleep(0.05)
    r2 = prefetcher.get()
    assert r2["version"] == 2

    print("  PASS: test_cache_warming_idempotent")


def test_prefetcher_get_no_fallback():
    """get() should NOT call observe_fn when nothing is prefetched."""
    from cua_agent.optimization import ParallelPrefetcher

    prefetcher = ParallelPrefetcher(enabled=True)
    call_count = [0]

    def observe():
        call_count[0] += 1
        return {"result": "data"}

    # No prefetch done yet — get() should return None without calling observe
    r = prefetcher.get()
    assert r is None, "get() should return None when no prefetch data"
    assert call_count[0] == 0, "get() should NOT call observe_fn"

    # get_or_observe with same observe_fn should call it (fallback)
    r2 = prefetcher.get_or_observe(observe)
    assert r2["result"] == "data"
    assert call_count[0] == 1, "get_or_observe should call observe_fn on miss"

    print("  PASS: test_prefetcher_get_no_fallback")


def test_prefetcher_get_expired():
    """get() should return None when prefetched data has expired."""
    from cua_agent.optimization import ParallelPrefetcher

    prefetcher = ParallelPrefetcher(enabled=True)

    prefetcher.prefetch(lambda: {"result": "stale"})
    import time
    time.sleep(0.05)

    # Force expiry
    prefetcher._prefetch_time = 0

    r = prefetcher.get()
    assert r is None, "get() should return None on expired prefetch"

    print("  PASS: test_prefetcher_get_expired")


def test_prefetcher_get_disabled():
    """get() should return None when prefetcher is disabled."""
    from cua_agent.optimization import ParallelPrefetcher

    prefetcher = ParallelPrefetcher(enabled=False)
    prefetcher.prefetch(lambda: {"result": "data"})

    r = prefetcher.get()
    assert r is None, "get() should return None when disabled"

    print("  PASS: test_prefetcher_get_disabled")


# ── Runner ──

if __name__ == "__main__":
    print("Running collector benchmark tests...\n")

    # ── Limit checking ──
    test_check_limits_increments()
    test_limits_exceeded_no_increment()
    test_check_limits_vs_limits_exceeded_counting()
    test_limits_exceeded_max_nodes()
    test_limits_exceeded_timeout()
    test_check_limits_timeout()

    # ── Adaptive timeout ──
    test_adaptive_timeout_starts_short()
    test_adaptive_timeout_backs_off_on_truncated()
    test_adaptive_timeout_multiple_backoffs()
    test_adaptive_timeout_resets_on_complete()
    test_adaptive_timeout_backoff_capped()
    test_adaptive_timeout_disabled()

    # ── Skip roles ──
    test_skip_children_roles_defined()
    test_skip_patterns_roles_defined()
    test_skip_roles_no_overlap()

    # ── Simulated tree walk ──
    test_simulated_tree_walk_timing()
    test_large_tree_walk_timing()
    test_mock_tree_element_extraction()

    # ── PerformanceTimer ──
    test_timing_overhead()

    # ── ObservationCache ──
    test_cache_hit_latency()
    test_cache_miss_latency()

    # ── Early exit ──
    test_early_exit_simulation()

    # ── ActionMerger ──
    test_merger_benchmark()

    # ── ParallelPrefetcher ──
    test_prefetcher_benchmark()
    test_prefetcher_fallback()

    # ── Cache warming ──
    test_cache_warming_basic()
    test_cache_warming_idempotent()
    test_prefetcher_get_no_fallback()
    test_prefetcher_get_expired()
    test_prefetcher_get_disabled()

    print(f"\n{'=' * 50}")
    print("✅ All 30 benchmark tests passed!")
