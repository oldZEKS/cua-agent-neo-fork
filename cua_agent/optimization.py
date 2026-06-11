"""
optimization.py — Performance optimization for the CUA agent.

Reduces latency and token waste through:
  - ActionMerger: Merges compatible consecutive actions (e.g., type keystrokes)
  - ObservationCache: Caches appstate to avoid redundant UIA tree walks
  - PerformanceTimer: Tracks timing of each phase for bottleneck analysis
  - TokenOptimizer: Minimizes prompt token usage
  - ParallelPrefetcher: Pre-fetches observations while LLM is thinking

Usage:
    from cua_agent.optimization import ActionMerger, ObservationCache, PerformanceTimer

    merger = ActionMerger()
    merged = merger.merge([action1, action2, action3])  # Merges type actions

    cache = ObservationCache()
    cached = cache.get_or_observe("window_title", lambda: provider.observe())
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("cua-optimization")


# ── Action Merger ──

@dataclass
class ActionMerger:
    """Merges compatible consecutive actions to reduce round-trips.

    Merger rules:
      - type + type → single type with combined text
      - scroll + scroll → single scroll with combined amount
      - click + wait + type → click-and-type with pre-delay
      - key_press + key_press → single key_press with combined combo
    """

    enabled: bool = True
    max_merged_types: int = 5  # Max type actions to merge at once

    def merge(self, actions: list[dict]) -> list[dict]:
        """Merge compatible consecutive actions into fewer calls.

        Args:
            actions: List of action dicts to potentially merge

        Returns:
            Merged list of action dicts
        """
        if not self.enabled or not actions:
            return actions

        merged: list[dict] = []
        i = 0

        while i < len(actions):
            current = actions[i]
            current_type = current.get("action", "")

            # ── Merge type actions ──
            if current_type == "type":
                combined_text = current.get("value", "")
                merged_count = 1

                j = i + 1
                while j < len(actions) and merged_count < self.max_merged_types:
                    next_action = actions[j]
                    next_type = next_action.get("action", "")
                    if next_type == "type":
                        combined_text += next_action.get("value", "")
                        merged_count += 1
                        j += 1
                    elif next_type == "wait" and int(next_action.get("value", "0")) <= 100:
                        # Small waits between keystrokes are fine
                        j += 1
                    else:
                        break

                if merged_count > 1:
                    merged.append({"action": "type", "value": combined_text,
                                   "_merged": merged_count})
                    logger.debug(f"Merged {merged_count} type actions ({len(combined_text)} chars)")
                    i = j
                    continue
                else:
                    merged.append(current)
                    i += 1
                    continue

            # ── Merge scroll actions ──
            elif current_type == "scroll":
                combined_amount = int(current.get("value", "1"))
                direction = current.get("target", "down")
                merged_count = 1

                j = i + 1
                while j < len(actions):
                    next_action = actions[j]
                    next_type = next_action.get("action", "")
                    next_target = next_action.get("target", "")
                    if next_type == "scroll" and next_target == direction:
                        combined_amount += int(next_action.get("value", "1"))
                        merged_count += 1
                        j += 1
                    elif next_type == "wait" and int(next_action.get("value", "0")) <= 200:
                        j += 1  # Skip small delays between scrolls
                    else:
                        break

                if merged_count > 1:
                    merged_amount = min(combined_amount, 10)  # Cap at 10
                    merged.append({"action": "scroll", "target": direction,
                                   "value": str(merged_amount), "_merged": merged_count})
                    logger.debug(f"Merged {merged_count} scroll {direction} actions")
                    i = j
                    continue
                else:
                    merged.append(current)
                    i += 1
                    continue

            # ── Merge click+wait+type into pre-type click ──
            elif current_type == "click":
                # Check if the next few actions are wait -> type to same target
                target = current.get("target", "")
                j = i + 1
                has_wait = False
                has_type = False
                type_text = ""

                while j < len(actions) and j < i + 5:
                    nxt = actions[j]
                    nt = nxt.get("action", "")
                    if nt == "wait" and not has_wait:
                        has_wait = True
                        j += 1
                    elif nt == "type" and has_wait:
                        has_type = True
                        type_text = nxt.get("value", "")
                        j += 1
                        break
                    else:
                        break

                if has_wait and has_type:
                    # Keep click + immediate type with a small delay
                    merged.append(current)
                    merged.append({"action": "wait", "value": "200"})
                    merged.append({"action": "type", "value": type_text,
                                   "_pre_clicked": True})
                    i = j
                    continue

            # ── Merge key_press + key_press (same key, dedup) ──
            elif current_type == "key_press":
                j = i + 1
                key = current.get("target", "").lower()
                # Skip duplicate identical key presses
                while j < len(actions):
                    nxt = actions[j]
                    if nxt.get("action") == "key_press" and nxt.get("target", "").lower() == key:
                        merged_count = 2
                        j += 1
                    elif nxt.get("action") == "wait" and int(nxt.get("value", "0")) <= 100:
                        j += 1
                    else:
                        break

                if j > i + 1:
                    merged.append({**current, "_deduped": True})
                    i = j
                    continue

            merged.append(current)
            i += 1

        return merged


# ── Observation Cache ──

@dataclass
class ObservationCache:
    """Cache for appstate observations to avoid redundant UIA tree walks.

    The cache uses a fingerprint of the observed state (window title +
    total element count) to detect changes. If nothing changed, the
    cached observation is returned instead of making a new UIA call.

    Useful when the agent loop re-observes after every action, even
    when the action didn't change the UI (e.g., typing text).
    """

    max_size: int = 5
    ttl_seconds: float = 2.0  # Cache lives this long at most
    fingerprint_fields: tuple[str, ...] = ("title", "role", "_pid")

    def __post_init__(self):
        self._cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get_or_observe(
        self,
        observe_fn: Callable[[], Any],
        current_context: dict | None = None,
    ) -> Any:
        """Get cached observation or call observe_fn if cache is stale.

        Args:
            observe_fn: Function that performs the actual observation
            current_context: Optional context dict for cache key fingerprinting

        Returns:
            The observation (cached or fresh)
        """
        now = time.time()
        cache_key = self._make_key(current_context) if current_context else "default"

        # Check cache
        if cache_key in self._cache:
            ts, cached_value = self._cache[cache_key]
            if now - ts < self.ttl_seconds:
                self._cache.move_to_end(cache_key)
                logger.debug(f"Observation cache hit for {cache_key[:40]}")
                return cached_value

        # Cache miss — call observe_fn
        value = observe_fn()

        # Store in cache
        self._cache[cache_key] = (now, value)
        if len(self._cache) > self.max_size:
            self._cache.popitem(last=False)

        logger.debug(f"Observation cache miss for {cache_key[:40]}")
        return value

    def invalidate(self, context: dict | None = None):
        """Invalidate cache entries. If context is None, clear all."""
        if context is None:
            self._cache.clear()
        else:
            key = self._make_key(context)
            self._cache.pop(key, None)

    def _make_key(self, context: dict) -> str:
        """Create a fingerprint hash from context fields."""
        parts = []
        for field in self.fingerprint_fields:
            val = context.get(field, "")
            parts.append(str(val))
        # Also include total element count if available
        if "children" in context:
            parts.append(str(len(context["children"])))
        return hashlib.md5("|".join(parts).encode()).hexdigest()[:16]


# ── Performance Timer ──

@dataclass
class _TimerEntry:
    label: str
    start: float
    end: float | None = None


@dataclass
class PerformanceTimer:
    """Tracks timing of each phase in the agent loop for bottleneck analysis.

    Usage:
        timer = PerformanceTimer()
        timer.start("observe")
        # ... observe ...
        timer.stop("observe")
        timer.start("think")
        # ... call LLM ...
        timer.stop("think")
        print(timer.report())
    """

    enabled: bool = True
    recent_entries: int = 100  # Keep last N entries

    def __post_init__(self):
        self._entries: list[_TimerEntry] = []
        self._open: dict[str, _TimerEntry] = {}

    def start(self, label: str):
        """Start timing a phase."""
        if not self.enabled:
            return
        entry = _TimerEntry(label=label, start=time.perf_counter())
        self._open[label] = entry
        self._entries.append(entry)

    def stop(self, label: str) -> float | None:
        """Stop timing a phase. Returns duration in seconds."""
        if not self.enabled:
            return None
        entry = self._open.pop(label, None)
        if entry is None:
            return None
        entry.end = time.perf_counter()
        duration = entry.end - entry.start

        # Trim old entries
        if len(self._entries) > self.recent_entries:
            self._entries = self._entries[-self.recent_entries:]

        return duration

    def get_stats(self, label: str | None = None) -> dict[str, float]:
        """Get timing statistics for a specific label or all labels."""
        relevant = [e for e in self._entries if e.end is not None
                    and (label is None or e.label == label)]
        if not relevant:
            return {}

        durations = [e.end - e.start for e in relevant]
        return {
            "count": len(durations),
            "total": sum(durations),
            "avg": sum(durations) / len(durations),
            "min": min(durations),
            "max": max(durations),
        }

    def report(self, top_n: int = 5) -> str:
        """Generate a human-readable timing report."""
        from collections import Counter

        label_counts = Counter(e.label for e in self._entries if e.end is not None)

        lines = ["Timing Report:"]
        for label, count in label_counts.most_common(top_n):
            stats = self.get_stats(label)
            if stats:
                lines.append(
                    f"  {label}: {stats['count']} calls, "
                    f"avg {stats['avg']*1000:.0f}ms, "
                    f"total {stats['total']:.1f}s"
                )
        return "\n".join(lines)

    def reset(self):
        """Clear all entries."""
        self._entries.clear()
        self._open.clear()


# ── Token Optimizer ──

@dataclass
class TokenOptimizer:
    """Optimizes prompt tokens by deduplicating and compressing content.

    Works alongside PromptBuilder's budget enforcement to provide
    additional optimizations:
      - Deduplicates consecutive identical appshots
      - Compresses repeated action patterns
      - Removes redundant context
    """

    enabled: bool = True
    max_action_history: int = 15
    max_appshot_history: int = 8

    def compress_action_history(self, actions: list[dict]) -> list[dict]:
        """Compress action history by summarizing repeated patterns.

        For example, 5 consecutive 'type' actions become a single entry
        showing the cumulative result.
        """
        if not self.enabled or not actions:
            return actions

        compressed: list[dict] = []
        i = 0

        while i < len(actions):
            current = actions[i]
            current_type = current.get("action", "")

            # Compress repeated identical action types
            if current_type in ("type", "scroll"):
                count = 1
                j = i + 1
                while j < len(actions) and j - i < 10:
                    if actions[j].get("action") == current_type:
                        count += 1
                        j += 1
                    else:
                        break

                if count > 3:
                    compressed.append({
                        "action": current_type,
                        "summary": f"{current_type} x{count}",
                        "value": current.get("value", "")[:30],
                        "_compressed": count,
                    })
                    i = j
                    continue

            compressed.append(current)
            i += 1

        # Truncate to max history
        if len(compressed) > self.max_action_history:
            compressed = compressed[-self.max_action_history:]

        return compressed

    def compress_appshot_history(self, appshots: list[dict]) -> list[dict]:
        """Compress appshot history by removing redundant snapshots."""
        if not self.enabled or not appshots:
            return appshots

        # Remove consecutive same-summary appshots
        compressed: list[dict] = []
        last_summary = None

        for shot in appshots:
            summary = shot.get("summary", "")
            if summary == last_summary and shot.get("type") == "no_change":
                continue  # Skip consecutive no-change shots
            compressed.append(shot)
            last_summary = summary

        # Truncate
        if len(compressed) > self.max_appshot_history:
            compressed = compressed[-self.max_appshot_history:]

        return compressed


# ── Convenience: create all optimizers ──

@dataclass
class OptimizationConfig:
    """Configuration for all optimizers at once."""
    action_merger: bool = True
    observation_cache: bool = True
    performance_timer: bool = True
    token_optimizer: bool = True
    parallel_prefetcher: bool = True

    max_merged_types: int = 5
    cache_ttl: float = 2.0

    # Keep the max history limits in sync with token budget
    max_action_history: int = 15
    max_appshot_history: int = 8

    def create_prefetcher(self) -> ParallelPrefetcher:
        return ParallelPrefetcher(enabled=self.parallel_prefetcher)

    def create_merger(self) -> ActionMerger:
        return ActionMerger(enabled=self.action_merger, max_merged_types=self.max_merged_types)

    def create_cache(self) -> ObservationCache:
        return ObservationCache(ttl_seconds=self.cache_ttl, max_size=5)

    def create_timer(self) -> PerformanceTimer:
        return PerformanceTimer(enabled=self.performance_timer)

    def create_token_optimizer(self) -> TokenOptimizer:
        return TokenOptimizer(
            enabled=self.token_optimizer,
            max_action_history=self.max_action_history,
            max_appshot_history=self.max_appshot_history,
        )


# ── Parallel Prefetcher ──

@dataclass
class ParallelPrefetcher:
    """Pre-fetches observations in the background while the LLM is thinking.

    Reduces perceived latency by overlapping observation (UIA tree walk)
    with LLM inference. When enabled, the agent loop triggers a background
    observation immediately after executing an action, so by the time the
    LLM finishes thinking, the next observation is already cached.

    This works because:
    1. Action execution takes ~50-200ms (SendKeys, Click, etc.)
    2. UIA tree walk takes ~200ms-5s depending on app complexity
    3. LLM inference takes ~1-5s
    4. Overlapping 2 and 3 means 0 additional wait time for observation

    Usage:
        prefetcher = ParallelPrefetcher()
        prefetcher.prefetch(lambda: provider.observe())
        # ... LLM thinks ...
        obs = prefetcher.get_or_observe(lambda: provider.observe())
        # Returns prefetched observation if available
    """

    enabled: bool = True

    def __post_init__(self):
        self._prefetched: Any = None
        self._prefetch_time: float = 0.0
        self._prefetch_ttl: float = 5.0  # Prefetched obs valid for 5s
        self._thread: Any = None

    def prefetch(self, observe_fn: Callable[[], Any]):
        """Start a background prefetch of the next observation.

        Spawns a daemon thread to run observe_fn. The result can be
        retrieved later with get_or_observe().

        Safe to call even if the previous prefetch hasn't completed -
        the new prefetch replaces the old one.
        """
        if not self.enabled:
            return

        import threading

        def _do_prefetch():
            try:
                result = observe_fn()
                self._prefetched = result
                self._prefetch_time = time.time()
            except Exception:
                pass

        self._thread = threading.Thread(target=_do_prefetch, daemon=True)
        self._thread.start()

    def get_or_observe(self, observe_fn: Callable[[], Any]) -> Any:
        """Get the prefetched observation or call observe_fn if expired/missing.

        If the prefetch thread is still running, waits for it (up to 5s).
        If observe_fn is None, returns None on miss instead of calling it.
        """
        if not self.enabled:
            return observe_fn() if observe_fn else None

        # If a prefetch thread is running, wait a bit for it
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)

        # Check if prefetched result is still fresh
        if self._prefetched is not None:
            age = time.time() - self._prefetch_time
            if age < self._prefetch_ttl:
                logger.debug(f"Using prefetched observation (age={age*1000:.0f}ms)")
                result = self._prefetched
                self._prefetched = None
                return result

        # Prefetch missed or expired - do fresh observation
        if observe_fn is not None:
            logger.debug("Prefetch miss, doing fresh observation")
            return observe_fn()
        return None

    def get(self) -> Any:
        """Get prefetched result if available without fallback observe.

        Returns None if prefetch hasn't completed, is expired, or disabled.
        Never calls observe_fn — returns None instead. Useful for zero-cost
        peeks at the prefetch buffer (e.g., in the observe() pipeline where
        other cache tiers exist to fall back on).
        """
        if not self.enabled or self._prefetched is None:
            return None
        if time.time() - self._prefetch_time > self._prefetch_ttl:
            return None
        result = self._prefetched
        self._prefetched = None
        return result

    def cancel(self):
        """Cancel any pending prefetch."""
        self._prefetched = None
        self._thread = None
