"""
prompt_builder.py — Builds optimized prompts for DeepSeek v4 (text-only CUA).

Core design principles:
1. Replace screenshots with structured accessibility tree JSON
2. Replace pixel coordinates with element IDs ([0], [1], ...)
3. Use appshot diffs instead of full state replays to save context
4. Include action history as structured examples (few-shot)
5. Keep system prompt constant; vary only the state/context
6. ENFORCE token budgets — never exceed context window
"""

from __future__ import annotations

import json
import math
import textwrap
from dataclasses import dataclass, field
from typing import Any


# ── Configuration ──

@dataclass
class PromptConfig:
    """Configuration for prompt construction and token budget enforcement."""

    # Model constraints
    max_total_tokens: int = 64000   # DeepSeek v4 context window
    reserved_output_tokens: int = 512  # Space for LLM response
    safety_margin: int = 256          # Extra buffer to avoid hard cutoffs

    # Budget allocation (fractions of remaining context)
    system_prompt_budget: float = 0.15       # 15% for system rules
    current_appstate_budget: float = 0.35    # 35% for current state
    appshot_history_budget: float = 0.25     # 25% for recent appshots
    action_history_budget: float = 0.15      # 15% for action history
    task_description_budget: float = 0.10    # 10% for task

    # Truncation strategy levels (increasing aggressiveness)
    # Level 0: Full state with positions
    # Level 1: Strip positions, keep full tree
    # Level 2: Strip positions, max depth 2
    # Level 3: Strip positions, max depth 1, limit children per parent
    # Level 4: Summary-only (no element list, just counts)
    max_truncation_level: int = 4

    # Formatting options — show more elements for thorough analysis
    compact_elements: bool = True
    max_elements: int = 50       # Show more elements so the model can scan the full tree
    max_appshots: int = 8        # Keep more state history for context
    max_action_history: int = 10 # Keep more action history for context

    # Few-shot examples
    include_examples: bool = True

    @property
    def available_tokens(self) -> int:
        """Total tokens available for the prompt (excluding output reservation and margin)."""
        return self.max_total_tokens - self.reserved_output_tokens - self.safety_margin

    def get_budget(self, field_name: str) -> int:
        """Get the token budget for a specific section, as an integer token count."""
        budget_key = f"{field_name}_budget"
        fraction = getattr(self, budget_key, 0.1)
        return max(128, int(self.available_tokens * fraction))


# ── Shared constants ──

INTERACTIVE_ROLES: frozenset[str] = frozenset({
    "AXButton", "AXTextField", "AXComboBox", "AXCheckBox",
    "AXRadioButton", "AXSlider", "AXPopUpButton", "AXMenuButton",
    "AXLink", "AXTab", "AXDisclosureTriangle", "AXStepper",
    "AXCell", "AXMenuItem", "AXMenuBarItem", "AXToolbarButton",
    "AXStaticText", "AXImage", "AXTable", "AXList", "AXOutline",
    "AXWindow", "AXSheet", "AXDialog", "AXGroup", "AXSplitGroup",
    "AXToolbar", "AXScrollArea", "AXApplication",
    # Without AX prefix (from Windows/Linux)
    "Button", "TextField", "ComboBox", "CheckBox", "RadioButton",
    "Slider", "PopUpButton", "MenuButton", "Link", "Tab",
    "Cell", "MenuItem", "StaticText", "Image", "Table", "List",
    "Window", "Sheet", "Dialog", "Group", "Toolbar", "Application",
})


# ── Token Estimation ──

def estimate_tokens(text: str) -> int:
    """
    Estimate the number of tokens a string will consume.
    
    For DeepSeek v4 / most LLMs, a good rule of thumb is ~4 chars per token
    for English text, and ~1 token per symbol/number.
    
    This is a fast, conservative estimate (overestimates slightly to be safe).
    """
    if not text:
        return 0
    
    # Count characters
    char_count = len(text)
    
    # Count words (rough proxy for token count in natural language)
    word_count = len(text.split())
    
    # Count special token-heavy elements
    # JSON and code tends to be more token-dense (~3 chars/token)
    # Natural language is ~4 chars/token
    # Use a blended rate: 3.5 chars per token (conservative)
    estimated = math.ceil(char_count / 3.5)
    
    # Additional overhead estimate
    estimated = max(estimated, word_count)  # At minimum, 1 token per word
    
    return estimated


def estimate_dict_tokens(d: dict | list | str) -> int:
    """Estimate tokens for a JSON-serializable value."""
    if isinstance(d, str):
        return estimate_tokens(d)
    try:
        serialized = json.dumps(d, indent=2, ensure_ascii=False, default=str)
        return estimate_tokens(serialized)
    except (TypeError, ValueError):
        return estimate_tokens(str(d))


# ── Aggressive Tree Compression ──

def compress_appstate_tree(
    node: dict,
    truncation_level: int = 0,
    depth: int = 0,
    max_depth: int | None = None,
    strip_positions: bool = False,
    max_children: int | None = None,
) -> dict | None:
    """
    Recursively compress an accessibility tree node to fit within budget.
    
    Args:
        node: The tree node to compress
        truncation_level: 0-4, higher = more aggressive
        depth: Current recursion depth
        max_depth: Max depth to recurse (None = no limit)
        strip_positions: Remove position/size data
        max_children: Max children per node
    
    Returns:
        Compressed node, or None if the node should be omitted
    """
    if not node:
        return None
    
    # Determine max depth from truncation level if not specified
    if max_depth is None:
        max_depth = {0: 99, 1: 99, 2: 2, 3: 1, 4: 0}[min(truncation_level, 4)]
    
    if depth > max_depth:
        return None
    
    # Strip positions if configured
    result = {}
    for key in ("role", "title", "value", "label", "desc", "help"):
        if key in node and node[key]:
            result[key] = node[key]
    
    # State flags (always keep these — they're tiny)
    for key in ("focused", "selected", "enabled"):
        if key in node:
            result[key] = node[key]
    
    # Actions (keep as short strings)
    if "actions" in node and node["actions"]:
        result["actions"] = [
            a.replace("AX", "").lower()[:8] for a in node["actions"][:5]
        ]
    
    # Position/size — strip if configured
    if not strip_positions and "pos" in node and "size" in node:
        result["pos"] = node["pos"]
        result["size"] = node["size"]
    
    # Children — recursively compress
    children = node.get("children", [])
    if children:
        max_kids = max_children or {0: 999, 1: 50, 2: 20, 3: 10, 4: 0}[
            min(truncation_level, 4)
        ]
        
        compressed_children = []
        for child in children[:max_kids]:
            cc = compress_appstate_tree(
                child,
                truncation_level=truncation_level,
                depth=depth + 1,
                max_depth=max_depth,
                strip_positions=strip_positions,
                max_children=max_children,
            )
            if cc is not None:
                compressed_children.append(cc)
        
        if compressed_children:
            result["children"] = compressed_children
    
    return result if (depth == 0 or result != {}) else None


def tree_element_count(node: dict) -> int:
    """Count total elements in a tree (including nested)."""
    count = 1
    for child in node.get("children", []):
        count += tree_element_count(child)
    return count


def flatten_to_summary(node: dict) -> str:
    """
    Extreme compression: produce a text summary instead of a tree.
    Used at truncation level 4 (last resort).
    """
    role = node.get("role", "").replace("AX", "")
    title = node.get("title", node.get("label", ""))
    
    if role in ("Window", "AXWindow", "Dialog", "AXDialog", "Sheet", "AXSheet"):
        # Window-level: count children by role
        role_counts = {}
        def _count_roles(n: dict):
            r = n.get("role", "").replace("AX", "")
            if r and r not in ("Window", "AXWindow", "Group", "AXGroup", ""):
                role_counts[r] = role_counts.get(r, 0) + 1
            for c in n.get("children", []):
                _count_roles(c)
        _count_roles(node)
        
        parts = [f"Window \"{title}\""]
        if role_counts:
            type_strs = [f"{c}x {r}" for r, c in 
                        sorted(role_counts.items(), key=lambda x: -x[1])[:5]]
            parts.append(", ".join(type_strs))
        return " | ".join(parts)
    
    return f"({role} '{title[:40]}')"


# ── Prompt Builder ──

class PromptBuilder:
    """
    Builds structured prompts for text-only computer-use agents.
    
    Converts raw appstate, appshots, and action history into
    an optimized prompt that enables DeepSeek v4 to reason about
    and control the desktop without vision.
    
    Token budget enforcement:
    - build_messages() computes budgets and enforces them
    - If a section exceeds its budget, it's truncated aggressively
    - Truncation has 5 levels (0=full, 4=summary-only)
    """
    
    def __init__(self, config: PromptConfig | None = None):
        self.config = config or PromptConfig()
    
    def build_messages(
        self,
        appstate: dict,
        appshots: list[dict],
        action_history: list[dict],
        task: str,
        available_actions: list[dict] | None = None,
    ) -> list[dict]:
        """
        Build the full messages array for the DeepSeek v4 API call,
        enforcing token budgets for each section.
        
        Returns [system_message, user_message] with all context embedded.
        """
        config = self.config
        
        # ── 1. Build system prompt and measure it ──
        system = self._build_system_prompt(available_actions)
        system_tokens = estimate_tokens(system)
        system_budget = config.get_budget("system_prompt")
        
        if system_tokens > system_budget:
            # Truncate action list from system prompt to fit
            system = self._truncate_system_prompt(system, system_budget)
        
        # ── 2. Build user message sections with budget enforcement ──
        sections = []
        remaining_budget = (
            config.available_tokens 
            - system_tokens 
            - estimate_tokens("---\n\n") * 4  # Separators between sections
        )
        
        # Budgets for each section
        state_budget = min(
            config.get_budget("current_appstate"),
            int(remaining_budget * 0.5)  # At most 50% of remaining
        )
        appshot_budget = min(
            config.get_budget("appshot_history"),
            int(remaining_budget * 0.3)
        )
        history_budget = min(
            config.get_budget("action_history"),
            int(remaining_budget * 0.14)
        )
        reflection_budget = int(remaining_budget * 0.02)
        task_budget = min(
            config.get_budget("task_description"),
            int(remaining_budget * 0.1)
        )
        action_prompt_budget = int(remaining_budget * 0.04)
        
        # ── State section ──
        state_section = self._format_current_state(
            appstate, 
            token_budget=state_budget,
        )
        sections.append(state_section)
        
        # ── Appshot timeline ──
        if appshots:
            timeline_section = self._format_appshot_timeline(
                appshots, 
                token_budget=appshot_budget,
            )
            if timeline_section.strip():
                sections.append(timeline_section)
        
        # ── Action history ──
        if action_history:
            history_section = self._format_action_history(
                action_history,
                token_budget=history_budget,
            )
            if history_section.strip():
                sections.append(history_section)
        
        # ── Post-action reflection (after first action) ──
        if action_history and appshots and len(action_history) >= 1:
            reflection_text = self._format_reflection(
                action_history[-1],
                appshots[-1] if len(appshots) >= 1 else None,
                token_budget=reflection_budget,
            )
            if reflection_text.strip():
                sections.append(reflection_text)
        
        # ── Task description ──
        task_section = self._format_task(task, token_budget=task_budget)
        sections.append(task_section)
        
        # ── Action prompt ──
        action_prompt = self._build_action_prompt(action_prompt_budget)
        sections.append(action_prompt)
        
        user_message = "\n\n---\n\n".join(sections)
        
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user_message},
        ]
    
    def build_continue_message(
        self,
        appstate: dict,
        appshot: dict,
        last_action_result: dict | None = None,
    ) -> list[dict]:
        """
        Build a continuation message for the next step in the loop.
        Lighter weight than the initial message — assumes system prompt
        is already in context.
        """
        # For continuation, we have more room — use 50% of available tokens
        budget = int(self.config.available_tokens * 0.5)
        
        parts = []
        budget_left = budget
        
        # Compact state update
        state = self._format_compact_state(appstate, max_elements=12)
        parts.append(state)
        budget_left -= estimate_tokens(state)
        
        # Appshot
        shot_text = f"## Appshot (Step {appshot.get('id', '?')})\n{appshot.get('summary', '')}"
        parts.append(shot_text)
        budget_left -= estimate_tokens(shot_text)
        
        # Action result feedback
        if last_action_result and budget_left > 100:
            result_text = self._format_action_result_feedback(last_action_result)
            if estimate_tokens(result_text) <= budget_left:
                parts.append(result_text)
                budget_left -= estimate_tokens(result_text)
        
        # Action prompt (abbreviated)
        parts.append("\n## Next Action\nRespond with a JSON action object.")
        
        return [
            {"role": "user", "content": "\n\n".join(parts)}
        ]
    
    # ── System prompt ──
    
    def _build_system_prompt(self, available_actions: list[dict] | None) -> str:
        """Build the system prompt with role, rules, and action definitions."""
        base = textwrap.dedent("""\
            You are a computer-use agent controlling a desktop computer.
            
            ## How You Operate
            You do NOT see screenshots. Instead, you receive the current application
            state as a structured accessibility tree — a machine-readable description
            of every interactive element on the screen (buttons, text fields, links,
            menus, etc.) with their labels, roles, values, and positions.
            
            ## Mandatory Analysis Phase
            Before EVERY action, you MUST complete a full analysis. This is not optional.
            Skipping steps or rushing will cause mistakes.
            
            Your internal reasoning for each step should follow this structure:
            
            **1. Scan the Element List** — Read every single element in "Current State".
               For each element, note its [ID], role, label, value, and state (focused,
               enabled, selected). Do not skip any element.
            
            **2. Identify What's Relevant** — Which elements are relevant to the current
               task step? What is their purpose in the interface?
            
            **3. Choose Your Action Target** — Exactly which element should you interact
               with, and why? Consider: is this a button? A text field? A menu? A link?
               What action type fits best (click, type, select, etc.)?
            
            **4. Predict the Outcome** — What do you expect to happen after this action?
               A new dialog? Text appearing? A menu opening? This helps you verify later.
            
            **5. Select the Right Action Type** — Based on the element's role:
               - Button → click
               - TextField → click to focus, then type
               - ComboBox → click to expand, then select
               - CheckBox → click to toggle
               - Table/List → scroll to find target, then click
               - Link → click
               - Menu/MenuItem → click to open menu, then click item
               - Unfamiliar element → hover first to read tooltip/context, then decide
            
            **6. Reflect on the Outcome** — After the action executes, compare what
               actually happened with your prediction. Did the expected dialog appear?
               Was the text entered correctly? Did a menu open? If the outcome doesn't
               match, diagnose why — was the wrong element targeted? Wrong action type?
               Timing issue? — and adjust your next action accordingly.
               Use the appshot diff to see exactly what changed.
            
            **7. Rate Your Confidence** — Before acting, rate your confidence (1-5):
               - 5: Very confident — clear target, known element, predictable result
               - 4: Confident — familiar pattern, likely to work
               - 3: Moderate — some uncertainty about element behavior or result
               - 2: Low — unfamiliar element, uncertain outcome, proceed with caution
               - 1: Very low — guessing entirely, find a safer alternative first
               
               If confidence < 3, do NOT proceed directly. Instead:
               - Hover over the element to read its tooltip first
               - Look for alternative, more familiar elements
               - Try a smaller exploratory action (e.g., hover, find_element)
               - Press Escape to dismiss unexpected states before retrying
            
            ## The Agent Loop
            1. **Reflect** — review the previous action's outcome. Did it match your
               expectation? Check the appshot diff to confirm what changed. If the
               outcome was unexpected, diagnose why before proceeding.
            2. **Scan the full element list** — read every element carefully
            3. **Analyze** — determine what each element does and its relevance
            4. **Decide** — pick the single next action that moves toward the task goal
            5. **Output** — exactly one JSON action object
            6. The action is executed, and you receive the updated state + result
            
            ## Handling Unexpected States
            Sometimes the desktop state won't match your expectations. This happens.
            When it does, follow this recovery protocol:
            
            1. **Pause** — Do NOT act immediately. An incorrect action when lost will
               only make things worse.
            2. **Scan the entire state** — Read every element. Look for familiar
               landmarks: window title bars, known menu items, the taskbar, system tray.
            3. **Diagnose** — Ask yourself: what's different from what I expected?
               Is there a new dialog? Did a window close unexpectedly? Am I in a
               different application?
            4. **Get your bearings** — Look for:
               - The window title (usually the first element in the tree)
               - "Close", "Cancel", "X" buttons (to dismiss unexpected dialogs)
               - The taskbar / dock (to switch windows)
               - Known UI patterns (menu bars, address bars, search fields)
            5. **Recover** — Take a small, safe action:
               - Press Escape to dismiss unexpected dialogs or popups
               - Press Alt+Tab to cycle back to the task window
               - Click the window title bar to bring it to focus
               - Find and click a "Close" or "Cancel" button on unexpected dialogs
            6. **Retry** — Once you've regained context, scan again and continue
               toward the original goal.
            
            **Key principle:** When confused, do less, not more. A single Escape key
            press is safer than clicking on unknown elements.
            
            ## Critical Rules
            - Always examine EVERY element in the list before acting — never skip ahead
            - Reference elements by their [ID] (e.g., [0], [1]) — NOT by pixel coordinates
            - The right action depends on the element's ROLE: buttons are clicked, fields
              are typed into, menus are expanded, checkboxes are toggled
            - Use keyboard shortcuts when faster than clicking (Ctrl+C, Ctrl+V, Ctrl+Tab)
            - If an element isn't visible, scroll or navigate to find it first
            - Wait for state changes to complete (use wait action when needed)
            - If an action fails, analyze what went wrong and try a different approach
            - You can take screenshots for debugging, but your primary input is state
            - Take your time. Rushing causes errors. Deliberate, careful analysis produces
              correct results.
            
            ## Task Strategy
            Break complex tasks into sequential steps. For each step, follow the
            Mandatory Analysis Phase above. Do not try to do multiple actions in one
            step — one action at a time, with full analysis each time.
            """)
        
        if available_actions or self.config.include_examples:
            base += "\n\n## Available Actions\n"
            
            actions_desc = [
                'click {"action": "click", "target": "[0]"} — Click element by ID',
                'click_path {"action": "click", "target": \'Button["Submit"]\'} — Click by role+label path',
                'double_click {"action": "double_click", "target": "[3]"}',
                'type {"action": "type", "value": "hello", "target": "[2]"} — Type into element',
                'type_focused {"action": "type", "value": "hello"} — Type into focused element',
                'type_into {"action": "type_into", "target": "[2]", "value": "hello"} — Click target then type',
                'hover {"action": "hover", "target": "[0]"} — Hover over element for tooltip/preview',
                'select {"action": "select", "target": "[3]", "value": "Option A"} — Select option from dropdown/combobox',
                'resize_window {"action": "resize_window", "value": "1024,768"} — Resize foreground window',
                'minimize_window {"action": "minimize_window"} — Minimize foreground window to taskbar',
                'maximize_window {"action": "maximize_window"} — Maximize foreground window to fill screen',
                'close_window {"action": "close_window"} — Close foreground window gracefully',
                'take_screenshot {"action": "take_screenshot"} — Capture full screen as base64 PNG image',
                'take_region_screenshot {"action": "take_region_screenshot", "target": "100,200,400,300"} — Capture a specific screen region as base64 PNG',
                'shortcut {"action": "shortcut", "target": "copy"} — Trigger named keyboard shortcut (copy, paste, save, new_tab, etc.)',
                'drag {"action": "drag", "target": "[0]", "value": "400,500"} — Drag element to position',
                'drag_to_element {"action": "drag", "target": "[0]", "value": "Trash"} — Drag element onto another element',
                'find_element {"action": "find_element", "target": "Submit"} — Find element by name',
                'find_element_pos {"action": "find_element", "target": "200,300"} — Find element by position',
                'key_press {"action": "key_press", "target": "enter"}',
                'key_combo {"action": "key_press", "target": "cmd+q"}',
                'scroll {"action": "scroll", "target": "down", "value": "3"}',
                'open_app {"action": "open_app", "target": "com.apple.Safari"}',
                'wait {"action": "wait", "value": "1000"} — Wait 1000ms',
            ]
            
            if available_actions:
                names = {a.get("name") for a in available_actions}
                for desc in actions_desc:
                    name = desc.split()[0]
                    if name in names:
                        base += f"  - {desc.split(' ', 1)[1]}\n"
            else:
                for desc in actions_desc:
                    base += f"  - {desc.split(' ', 1)[1]}\n"
        
        base += textwrap.dedent("""\
            
            ## Response Format
            Respond with ONLY a single JSON object. No markdown, no code fences,
            no explanation. Example:
            {"action": "click", "target": "[0]"}
            
            If you are done with the task, respond with:
            {"action": "done", "result": "Summary of what was accomplished"}
            """)
        
        return base
    
    def _truncate_system_prompt(self, system: str, budget: int) -> str:
        """Truncate the system prompt to fit within budget by removing examples."""
        # Remove action examples first (they're the largest removable part)
        lines = system.split("\n")
        kept = []
        in_actions = False
        action_count = 0
        max_actions = max(1, int((budget - estimate_tokens("\n".join(
            l for l in lines if not l.strip().startswith("- {")
        ))) / 50))  # ~50 tokens per action line
        
        for line in lines:
            if "## Available Actions" in line:
                in_actions = True
                kept.append(line)
                continue
            if in_actions and line.strip().startswith("- "):
                action_count += 1
                if action_count <= max_actions:
                    kept.append(line)
                continue
            if in_actions and not line.strip().startswith("- ") and not line.strip() == "":
                in_actions = False
            
            kept.append(line)
        
        return "\n".join(kept)
    
    # ── State formatting with budget enforcement ──
    
    def _format_current_state(self, appstate: dict, token_budget: int) -> str:
        """
        Format the current appstate as a concise, LLM-friendly element list.
        
        Enforces token_budget by progressively increasing truncation level.
        """
        # Try building with increasing truncation until it fits
        for truncation_level in range(0, self.config.max_truncation_level + 1):
            result = self._build_state_section(appstate, truncation_level)
            tokens = estimate_tokens(result)
            if tokens <= token_budget:
                return result
        
        # Last resort: minimal summary
        summary = flatten_to_summary(appstate)
        return f"## Current State\n*{summary}*\n\n*({tree_element_count(appstate)} elements — state compressed to fit context limit)*"
    
    def _build_state_section(self, appstate: dict, truncation_level: int) -> str:
        """Build a state section at a specific truncation level."""
        # Map truncation level to tree parameters
        depth_map = {0: 99, 1: 99, 2: 2, 3: 1, 4: 0}
        children_map = {0: 999, 1: 999, 2: 50, 3: 20, 4: 0}
        
        # Compress tree
        compressed = compress_appstate_tree(
            appstate,
            truncation_level=truncation_level,
            strip_positions=(truncation_level >= 1),
            max_depth=depth_map[min(truncation_level, 4)],
            max_children=children_map[min(truncation_level, 4)],
        )
        
        if truncation_level == 4 or not compressed:
            # Summary-only mode
            summary = flatten_to_summary(appstate)
            total = tree_element_count(appstate)
            return f"## Current State — Summary\n{summary}\n\n*({total} elements in tree)*"
        
        # Extract flat elements
        max_elements = {0: 50, 1: 40, 2: 30, 3: 20, 4: 0}[min(truncation_level, 4)]
        elements = self._flatten_interactive(compressed, max_elements=max_elements)
        
        parts = []
        app_name = compressed.get("title") or compressed.get("role", "Desktop")
        parts.append(f"## Current State — \"{app_name}\"")
        
        if elements:
            lines = []
            for i, el in enumerate(elements):
                line = self._format_element_line(i, el)
                if line:
                    lines.append(line)
            
            parts.append("**Elements:**")
            parts.append("\n".join(lines))
            
            if truncation_level >= 1:
                parts.append("*Position data omitted to save context*")
            
            total_els = tree_element_count(appstate)
            if total_els > len(elements):
                parts.append(f"*({len(elements)} of {total_els} elements shown)*")
        else:
            parts.append("*No interactive elements detected.*")
        
        return "\n".join(parts)
    
    def _format_compact_state(self, appstate: dict, max_elements: int = 12) -> str:
        """Compact state representation for continuation messages."""
        elements = self._flatten_interactive(appstate, max_elements=min(max_elements, 15))
        
        lines = ["## State Update"]
        for i, el in enumerate(elements[:max_elements]):
            line = self._format_element_line(i, el)
            if line:
                lines.append(f"  {line}")
        
        if len(elements) > max_elements:
            lines.append(f"  ... and {len(elements) - max_elements} more elements")
        
        return "\n".join(lines)
    
    def _format_element_line(self, index: int, el: dict) -> str:
        """Format a single element as a compact line."""
        parts = [f"[{index}]"]
        
        role = el.get("role", "?").replace("AX", "")
        parts.append(role)
        
        label = el.get("title") or el.get("label") or el.get("desc") or ""
        if label:
            label_str = str(label)[:60]
            parts.append(f"=\"{label_str}\"")
        
        value = el.get("value")
        if value is not None and value != "" and str(value) != str(label):
            val_str = str(value)[:40]
            parts.append(f"val=\"{val_str}\"")
        
        # State indicators (compact symbols)
        if el.get("focused"):
            parts.append("ⓘ")
        if el.get("selected"):
            parts.append("✓")
        if el.get("enabled") is False:
            parts.append("✗")
        if el.get("actions"):
            acts = [a.replace("AX", "").lower()[:4] for a in el["actions"]]
            parts.append(f"({','.join(acts)})")
        
        return " ".join(parts)
    
    # ── Appshot timeline with budget ──
    
    def _format_appshot_timeline(self, appshots: list[dict], token_budget: int) -> str:
        """Format the recent appshot timeline, respecting token budget."""
        shots = appshots[-self.config.max_appshots:]
        
        # Build progressively until we fit the budget
        for max_shots in range(min(len(shots), self.config.max_appshots), 0, -1):
            result = self._build_timeline_section(shots[-max_shots:])
            if estimate_tokens(result) <= token_budget:
                return result
        
        # Ultra-compact: just summaries
        summaries = [s.get("summary", "") for s in shots[-3:]]
        result = "## State Timeline\n" + "\n".join(f"- {s}" for s in summaries)
        if estimate_tokens(result) <= token_budget:
            return result
        
        # Last resort: just the most recent summary
        return f"## State Timeline\n- {shots[-1].get('summary', '')}"
    
    def _build_timeline_section(self, shots: list[dict]) -> str:
        """Build the full timeline section."""
        lines = ["## State Timeline", "```"]
        for shot in shots:
            marker = "*" if shot.get("important") else " "
            summary = shot.get("summary", "")
            diff = shot.get("diff", {})
            
            diff_info = ""
            if diff and diff.get("type") != "initial":
                adds = diff.get("added_count", 0)
                rems = diff.get("removed_count", 0)
                focus = diff.get("focus_changed_to")
                if adds or rems:
                    diff_info = f" (+{adds}/-{rems})"
                if focus:
                    diff_info += f" focus→{focus}"
            
            lines.append(f"  {marker} Step {shot.get('id', '?')}: {summary}{diff_info}")
        
        lines.append("```")
        return "\n".join(lines)
    
    # ── Action history with budget ──
    
    def _format_action_history(self, history: list[dict], token_budget: int) -> str:
        """Format recent action history, respecting token budget."""
        actions = history[-self.config.max_action_history:]
        
        for count in range(min(len(actions), self.config.max_action_history), 0, -1):
            result = self._build_action_section(actions[-count:])
            if estimate_tokens(result) <= token_budget:
                return result
        
        # Ultra-compact: just successes and failures
        successes = sum(1 for a in actions if a.get("success"))
        failures = sum(1 for a in actions if not a.get("success"))
        return f"## Recent Actions\n{successes} succeeded, {failures} failed (last {len(actions)} actions)"
    
    def _build_action_section(self, actions: list[dict]) -> str:
        """Build the full action history section."""
        lines = ["## Recent Actions", "```"]
        for act in actions:
            action_type = act.get("action", act.get("action_type", "?"))
            target = act.get("target", "")
            value = act.get("value", "")
            success = act.get("success", False)
            err = act.get("error", "")
            
            action_str = action_type
            if target:
                action_str += f" {target}"
            if value:
                val_short = str(value)[:30]
                action_str += f"=\"{val_short}\""
            
            status = "✓" if success else "✗"
            error_info = f" → {err[:60]}" if err else ""
            lines.append(f"  {status} {action_str}{error_info}")
        
        lines.append("```")
        return "\n".join(lines)
    
    # ── Task and action prompt ──
    
    def _format_task(self, task: str, token_budget: int) -> str:
        """Format the task description, truncating if needed."""
        full = f"## Task\n{task}\n\nComplete this task step by step."
        if estimate_tokens(full) <= token_budget:
            return full
        
        # Truncate the task itself
        max_task_chars = max(50, int(token_budget * 3.5) - 50)
        truncated_task = task[:max_task_chars]
        if len(task) > max_task_chars:
            truncated_task += "..."
        return f"## Task\n{truncated_task}\n\nComplete this task step by step."
    
    def _build_action_prompt(self, token_budget: int = 500) -> str:
        """Build the action instruction that concludes each prompt."""
        full = textwrap.dedent("""\
            ## Your Next Action
            
            Before outputting your action, show your analysis in the following format.
            This is MANDATORY — you must think through each step:
            
            ANALYSIS:
            - Window/context: [what app is this? what step of the task are we on?]
            - Elements scanned: [list which element IDs you reviewed and what they are]
            - Target element: [which element ID you chose and why]
            - Action type: [why this action type is right for this element's role]
            - Expected outcome: [what should happen next]
            - Confidence: [1-5, where 5 = very confident]
            
            Then output your action as JSON on the final line.
            
            Rules:
            - ONE action at a time — never combine multiple actions
            - Prefer element IDs like [0], [1] over role+label paths
            - Use keyboard shortcuts for efficiency (Ctrl+C, Ctrl+V)
            - Wait 500-1000ms between critical actions for state to settle
            - Take your time — deliberate analysis produces correct results
            
            Action:
            """)
        
        if estimate_tokens(full) <= token_budget:
            return full
        
        # Compact version
        return textwrap.dedent("""\
            ## Next Action
            Before your JSON action, write a brief analysis:
            - What elements did you review?
            - Which element are you targeting and why?
            - What do you expect to happen?
            
            Then output one JSON action.
            """)
    
    # ── Helpers ──
    
    def _format_reflection(self, last_action: dict, last_appshot: dict | None,
                             token_budget: int = 300) -> str:
        """Format a post-action reflection section that asks the model to
        compare the expected outcome with the actual outcome.
        """
        action_type = last_action.get("action", "?")
        target = last_action.get("target", "")
        value = last_action.get("value", "")
        success = last_action.get("success", False)
        error = last_action.get("error", "")
        
        action_desc = action_type
        if target:
            action_desc += f" \"{target}\""
        if value:
            val_short = str(value)[:40]
            action_desc += f"=\"{val_short}\""
        
        appshot_summary = (last_appshot or {}).get("summary", "") if last_appshot else ""
        
        full = textwrap.dedent(f"""\
            ## Reflection — Last Action Outcome
            
            **Action taken:** {action_desc}
            **Result:** {'✅ Success' if success else '❌ Failed'}
            {f'**Error:** {error[:80]}' if error else ''}
            {"**State change:** " + appshot_summary if appshot_summary else ""}
            
            Before planning your next action, reflect on this outcome:
            1. **Did the action have the expected effect?** Check the appshot diff above.
            2. **If unexpected**, what went wrong? Wrong target? Wrong action type?
            3. **What do you see in the updated state?** Are there new elements?
            4. **What should you do differently?** Adjust your strategy accordingly.
            """)
        
        if estimate_tokens(full) <= token_budget:
            return full
        
        # Compact version
        return textwrap.dedent(f"""\
            ## Reflection
            Last action: {action_desc} | {'✅' if success else '❌'}
            {appshot_summary}
            Consider: was the outcome expected? What should change?
            """)

    def _format_action_result_feedback(self, result: dict) -> str:
        """Format the result of the last action as feedback."""
        success = result.get("success", False)
        error = result.get("error", "")
        
        if success:
            return f"## Last Action: ✅ {result.get('action', 'Action')} completed."
        else:
            return f"## Last Action: ❌ Failed\nError: {error[:100]}"
    
    def _flatten_interactive(self, node: dict, max_elements: int = 35,
                             depth: int = 0) -> list[dict]:
        """Flatten accessibility tree to a flat list of interactive elements."""
        # Note: macOS AX trees are true trees (not DAGs), so duplicates
        # from parent-sharing are not expected. Dedup via position to be safe.
        seen_positions = set()
        elements = []
        
        def _walk(n: dict, d: int):
            if len(elements) >= max_elements:
                return
            
            role = n.get("role", "")
            title = n.get("title", "") or n.get("label", "") or n.get("desc", "") or ""
            
            # Dedup using position when available (unique in the tree)
            pos = n.get("pos")
            pos_key = f"{pos.get('x')}:{pos.get('y')}:{pos.get('w')}:{pos.get('h')}" if pos else None
            
            is_interactive = role in INTERACTIVE_ROLES
            if is_interactive and (pos_key is None or pos_key not in seen_positions):
                if pos_key:
                    seen_positions.add(pos_key)
                el = {
                    "role": role,
                    "title": title,
                    "value": n.get("value"),
                    "focused": n.get("focused", False),
                    "selected": n.get("selected", False),
                    "enabled": n.get("enabled", True),
                    "actions": n.get("actions", []),
                }
                if n.get("pos") and n.get("size"):
                    el["pos"] = n["pos"]
                    el["size"] = n["size"]
                elements.append(el)
            
            for child in n.get("children", []):
                if len(elements) >= max_elements:
                    break
                _walk(child, d + 1)
        
        _walk(node, 0)
        return elements


# ── Convenience factory ──

def create_prompt_builder(**kwargs) -> PromptBuilder:
    """Create a PromptBuilder with default or custom config."""
    config = PromptConfig(**kwargs)
    return PromptBuilder(config)
