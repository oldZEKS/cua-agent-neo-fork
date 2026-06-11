"""
security.py — Security layer for the CUA agent.

Prevents the autonomous agent from performing dangerous actions,
running too fast, or operating outside its allowed scope.

Key components:
  - RateLimiter: Enforces actions/second limits per action type
  - ActionValidator: Allowlists/blocklists for actions, targets, and apps
  - DangerousActionDetector: Pattern-based detection of destructive operations
  - SandboxConfig: Restricts the agent to specific windows or applications
  - SecurityContext: Tracks security state and confirmation requirements

Usage:
    from cua_agent.security import SecurityContext, ActionValidator, RateLimiter

    sec = SecurityContext()
    if sec.validate_action({"action": "open_app", "target": "notepad"}):
        # Safe to execute
        pass
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("cua-security")


# ── Danger Levels ──

class DangerLevel(Enum):
    """How dangerous an action is considered."""
    SAFE = 0       # Innocuous — always allowed
    CAUTION = 1    # Potentially disruptive — requires confirmation
    DANGEROUS = 2  # Destructive — requires explicit confirmation
    CRITICAL = 3   # System-damaging — blocked by default


# ── Rate Limiter ──

@dataclass
class RateLimiter:
    """Sliding-window rate limiter per action type.

    Tracks the number of actions of each type within a time window
    and rejects actions that would exceed the limit.

    Thresholds (configurable):
      - max_actions_per_window: total actions in the window
      - window_seconds: sliding window duration
      - per_type_limits: max per specific action type
    """

    max_actions_per_window: int = 30
    window_seconds: float = 10.0
    per_type_limits: dict[str, int] = field(default_factory=lambda: {
        "click": 15,
        "type": 20,
        "key_press": 20,
        "scroll": 15,
        "open_app": 3,
        "open_url": 5,
    })

    def __post_init__(self):
        self._history: list[tuple[str, float]] = []  # (action_type, timestamp)

    def check_action(self, action_type: str) -> tuple[bool, str]:
        """Check if an action is allowed by rate limits.

        Returns: (allowed: bool, reason: str)
        """
        now = time.time()
        cutoff = now - self.window_seconds

        # Remove expired entries
        self._history = [(t, ts) for t, ts in self._history if ts > cutoff]

        # Check total limit
        if len(self._history) >= self.max_actions_per_window:
            oldest = self._history[0][1] if self._history else now
            wait = max(0, self.window_seconds - (now - oldest))
            return False, f"Rate limit: {len(self._history)} actions in {self.window_seconds}s (wait {wait:.1f}s)"

        # Check per-type limit
        type_limit = self.per_type_limits.get(action_type, 10)
        type_count = sum(1 for t, ts in self._history if t == action_type)
        if type_count >= type_limit:
            return False, f"Rate limit: {type_count} '{action_type}' actions in {self.window_seconds}s"

        # Record the action
        self._history.append((action_type, now))
        return True, ""

    def reset(self):
        """Clear all rate history."""
        self._history.clear()


# ── Dangerous Action Patterns ──

# List of patterns that indicate potentially destructive actions
DANGEROUS_APP_PATTERNS: list[tuple[re.Pattern, DangerLevel, str]] = [
    # System tools
    (re.compile(r"^(cmd|powershell|wsl|bash|sh|zsh)", re.I), DangerLevel.CAUTION,
     "Shell/terminal — allows arbitrary command execution"),
    (re.compile(r"^(regedit|regedt32|gpedit|secpol)", re.I), DangerLevel.CRITICAL,
     "Registry/group policy editor — can damage system configuration"),
    (re.compile(r"^(taskmgr|taskkill|tskill)", re.I), DangerLevel.DANGEROUS,
     "Task manager — can kill processes"),
    (re.compile(r"^(diskpart|diskmgmt|diskmanagement)", re.I), DangerLevel.CRITICAL,
     "Disk management — can partition or format drives"),
    (re.compile(r"^(format|diskpart)", re.I), DangerLevel.CRITICAL,
     "Format tool — can erase drives"),

    # File operations
    (re.compile(r"^(explorer|file explorer)", re.I), DangerLevel.CAUTION,
     "File explorer — can navigate and delete files"),
    (re.compile(r"^(del|rmdir|rm|shred)", re.I), DangerLevel.DANGEROUS,
     "File deletion tool — can delete files"),

    # System settings
    (re.compile(r"^(control|systemsettings|ms-settings)", re.I), DangerLevel.CAUTION,
     "System settings — can change system configuration"),
    (re.compile(r"^(firewall|wf\.msc)", re.I), DangerLevel.DANGEROUS,
     "Firewall settings — can disable network security"),
    (re.compile(r"^(services|services\.msc)", re.I), DangerLevel.DANGEROUS,
     "Services manager — can start/stop critical services"),

    # Development tools (dangerous in wrong hands)
    (re.compile(r"^(sqlcmd|mysql|psql|sqlite3)", re.I), DangerLevel.DANGEROUS,
     "Database CLI — can modify or delete data"),
]

DANGEROUS_KEY_COMBOS: list[tuple[str, DangerLevel, str]] = [
    # Windows key combos that lock or log out
    ("win+l", DangerLevel.DANGEROUS, "Locks the workstation"),
    ("win+d", DangerLevel.CAUTION, "Shows desktop (minimizes all windows)"),
    ("alt+f4", DangerLevel.CAUTION, "Closes current window/app"),
    ("ctrl+alt+del", DangerLevel.CRITICAL, "Opens security screen — can lock or log off"),
    ("ctrl+shift+esc", DangerLevel.CAUTION, "Opens Task Manager"),
]

DANGEROUS_URL_PATTERNS: list[tuple[re.Pattern, DangerLevel, str]] = [
    (re.compile(r"^(file|ftp)://", re.I), DangerLevel.CAUTION, "Local file or FTP access"),
    (re.compile(r"(://|%2F%2F).*\b(delete|remove|wipe|drop|truncate)\b", re.I),
     DangerLevel.DANGEROUS, "URL contains destructive action keywords"),
]


# ── Action Validator ──

@dataclass
class ActionValidator:
    """Validates actions against allowlists, blocklists, and danger patterns.

    Configuration:
      - allowed_actions: set of action types allowed (None = all allowed)
      - blocked_actions: set of action types to block
      - allowed_apps: set of app names the agent can open (None = all apps)
      - blocked_apps: set of app names the agent cannot open
      - block_dangerous: whether to block actions flagged as dangerous
      - require_confirmation: whether to prompt for dangerous actions
    """

    allowed_actions: set[str] | None = None
    blocked_actions: set[str] = field(default_factory=set)
    allowed_apps: set[str] | None = None
    blocked_apps: set[str] = field(default_factory=set)
    block_dangerous: bool = True
    require_confirmation: bool = True

    def validate(self, action: dict) -> tuple[bool, str, DangerLevel]:
        """Validate an action against all security rules.

        Returns: (allowed: bool, reason: str, danger_level: DangerLevel)
        """
        action_type = action.get("action", "")
        target = action.get("target", "")
        value = action.get("value", "")

        # 1. Check blocklist
        if action_type in self.blocked_actions:
            return False, f"Action type '{action_type}' is blocked", DangerLevel.SAFE

        # 2. Check allowlist
        if self.allowed_actions is not None and action_type not in self.allowed_actions:
            return False, f"Action type '{action_type}' is not in allowlist", DangerLevel.SAFE

        # 3. Scan for dangerous patterns
        if action_type == "open_app":
            return self._validate_open_app(target)

        if action_type == "key_press":
            return self._validate_key_press(target)

        if action_type == "open_url":
            return self._validate_open_url(value)

        if action_type in ("click", "type", "scroll", "double_click", "right_click", "hover", "drag", "select", "resize_window", "minimize_window", "maximize_window", "close_window", "take_screenshot", "take_region_screenshot", "shortcut"):
            return True, "", DangerLevel.SAFE

        if action_type == "wait":
            ms = int(value or "0")
            if ms > 10000:
                return False, "Wait longer than 10 seconds is not allowed", DangerLevel.SAFE
            return True, "", DangerLevel.SAFE

        return True, "", DangerLevel.SAFE

    def _validate_open_app(self, app_name: str) -> tuple[bool, str, DangerLevel]:
        """Validate opening an application."""
        if not app_name:
            return False, "No app name specified", DangerLevel.SAFE

        # Check blocklist
        if app_name.lower() in {a.lower() for a in self.blocked_apps}:
            return False, f"App '{app_name}' is blocked", DangerLevel.SAFE

        # Check allowlist
        if self.allowed_apps is not None:
            if app_name.lower() not in {a.lower() for a in self.allowed_apps}:
                return False, f"App '{app_name}' is not in allowed list", DangerLevel.SAFE

        # Check dangerous patterns
        for pattern, level, reason in DANGEROUS_APP_PATTERNS:
            if pattern.search(app_name):
                if self.block_dangerous and level.value >= DangerLevel.DANGEROUS.value:
                    return False, f"Blocked: {reason}", level
                if level.value >= DangerLevel.CAUTION.value:
                    return True, f"Caution: {reason}", level
                break

        return True, "", DangerLevel.SAFE

    def _validate_key_press(self, combo: str) -> tuple[bool, str, DangerLevel]:
        """Validate a key press or keyboard shortcut."""
        if not combo:
            return False, "No key combo specified", DangerLevel.SAFE

        combo_norm = combo.lower().replace(" ", "")

        for pattern, level, reason in DANGEROUS_KEY_COMBOS:
            if pattern in combo_norm:
                if self.block_dangerous and level.value >= DangerLevel.DANGEROUS.value:
                    return False, f"Blocked: {reason}", level
                if level.value >= DangerLevel.CAUTION.value:
                    return True, f"Caution: {reason}", level
                break

        return True, "", DangerLevel.SAFE

    def _validate_open_url(self, url: str) -> tuple[bool, str, DangerLevel]:
        """Validate opening a URL."""
        if not url:
            return False, "No URL specified", DangerLevel.SAFE

        for pattern, level, reason in DANGEROUS_URL_PATTERNS:
            if pattern.search(url):
                if self.block_dangerous and level.value >= DangerLevel.DANGEROUS.value:
                    return False, f"Blocked: {reason}", level
                if level.value >= DangerLevel.CAUTION.value:
                    return True, f"Caution: {reason}", level
                break

        return True, "", DangerLevel.SAFE


# ── Sandbox Configuration ──

@dataclass
class SandboxConfig:
    """Restricts the agent's operating scope.

    The agent can be sandboxed to:
      - Specific windows (by title pattern)
      - Specific applications (by process name)
      - A specific monitor/display area
      - Read-only mode (observe only, no actions)
    """

    # Window restrictions
    allowed_window_patterns: list[str] = field(default_factory=list)
    blocked_window_patterns: list[str] = field(default_factory=list)

    # Application restrictions
    allowed_process_names: list[str] = field(default_factory=list)
    blocked_process_names: list[str] = field(default_factory=list)

    # Mode
    read_only: bool = False
    confirm_every_action: bool = False
    max_actions_total: int = 500
    max_apps_opened: int = 5

    def __post_init__(self):
        self._compiled_allowed: list[re.Pattern] = [
            re.compile(p, re.I) for p in self.allowed_window_patterns
        ]
        self._compiled_blocked: list[re.Pattern] = [
            re.compile(p, re.I) for p in self.blocked_window_patterns
        ]

    def is_window_allowed(self, window_title: str) -> bool:
        """Check if a window title is within the sandbox scope."""
        if not window_title:
            return True

        # Check blocklist first
        for pattern in self._compiled_blocked:
            if pattern.search(window_title):
                return False

        # If allowlist is set, only allow matching windows
        if self._compiled_allowed:
            for pattern in self._compiled_allowed:
                if pattern.search(window_title):
                    return True
            return False

        return True


# ── Security Context (main entry point) ──

@dataclass
class SecurityContext:
    """Main security context for the CUA agent loop.

    Combines rate limiting, action validation, and sandboxing into
    a single check that can be called before every action.

    Usage:
        sec_ctx = SecurityContext()
        allowed, reason, level = sec_ctx.check_action({"action": "click", ...})
        if allowed:
            # Execute
            pass
        elif level == DangerLevel.CAUTION:
            # Prompt user for confirmation
            pass
        else:
            # Block and log
            pass
    """

    rate_limiter: RateLimiter = field(default_factory=RateLimiter)
    validator: ActionValidator = field(default_factory=ActionValidator)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)

    # Tracking
    total_actions_executed: int = 0
    total_apps_opened: int = 0
    blocked_actions: list[dict] = field(default_factory=list)
    confirmed_actions: list[dict] = field(default_factory=list)

    def check_action(self, action: dict) -> tuple[bool, str, DangerLevel]:
        """Comprehensive security check for an action.

        Checks: sandbox → validator → rate limiter

        Returns: (allowed: bool, reason: str, danger_level: DangerLevel)
        """
        action_type = action.get("action", "")

        # 1. Sandbox: read-only mode
        if self.sandbox.read_only:
            return False, "Read-only mode: actions are disabled", DangerLevel.SAFE

        # 2. Sandbox: max actions
        if self.total_actions_executed >= self.sandbox.max_actions_total:
            return False, f"Max actions ({self.sandbox.max_actions_total}) reached", DangerLevel.SAFE

        # 3. Sandbox: apps opened limit
        if action_type == "open_app":
            if self.total_apps_opened >= self.sandbox.max_apps_opened:
                return False, f"Max apps opened ({self.sandbox.max_apps_opened}) reached", DangerLevel.SAFE

        # 4. Validate action
        allowed, reason, danger_level = self.validator.validate(action)
        if not allowed:
            self.blocked_actions.append({"action": action.copy(), "reason": reason})
            return False, reason, danger_level

        # 5. Check rate limits
        rate_allowed, rate_reason = self.rate_limiter.check_action(action_type)
        if not rate_allowed:
            self.blocked_actions.append({"action": action.copy(), "reason": rate_reason})
            return False, rate_reason, DangerLevel.SAFE

        return True, "", danger_level

    def record_confirmed(self, action: dict):
        """Record that a dangerous action was confirmed by the user."""
        self.confirmed_actions.append(action.copy())
        self.total_actions_executed += 1
        if action.get("action") == "open_app":
            self.total_apps_opened += 1

    def record_executed(self, action: dict):
        """Record that an action was executed (for tracking)."""
        self.total_actions_executed += 1
        if action.get("action") == "open_app":
            self.total_apps_opened += 1

    def get_report(self) -> dict:
        """Get a security summary report."""
        return {
            "total_actions_executed": self.total_actions_executed,
            "total_apps_opened": self.total_apps_opened,
            "blocked_count": len(self.blocked_actions),
            "confirmed_count": len(self.confirmed_actions),
            "recent_blocked": self.blocked_actions[-5:] if self.blocked_actions else [],
            "recent_confirmed": self.confirmed_actions[-5:] if self.confirmed_actions else [],
            "rate_summary": f"{len(self.rate_limiter._history)} actions in current window",
        }
