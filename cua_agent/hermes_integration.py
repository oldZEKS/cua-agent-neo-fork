"""
hermes_integration.py — Hermes Agent integration for the CUA driver.

Provides:
1. CUAConfig — Configuration for connecting the CUA driver to Hermes via MCP
2. NativeConfig — Configuration for registering CUA tools natively in Hermes
3. HermesIntegration — Wraps the agent loop as Hermes tools
4. Subagent templates — Prompt templates for Hermes subagents
5. Task decomposition — Breaks complex tasks into subagent work
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


# ── CUA Configuration for Hermes (MCP mode) ──

@dataclass
class CUAConfig:
    """
    Configuration to add to Hermes agent's config.yaml
    to enable CUA capabilities via MCP.
    """
    
    mcp_server_name: str = "cua-driver"
    mcp_command: str = "python"
    mcp_args: list[str] = field(default_factory=lambda: ["cua_mcp_server.py"])
    mcp_transport: str = "stdio"
    
    # CUA-specific settings
    default_max_steps: int = 50
    default_task_timeout: int = 300  # seconds
    require_confirmation_for: list[str] = field(default_factory=lambda: [
        "open_app", "type", "key_press"
    ])
    
    # Security
    allowed_apps: list[str] | None = None  # None = all apps
    
    def to_yaml_block(self) -> str:
        """Generate the YAML block to add to hermes config.yaml."""
        yaml = f"""
# ── CUA Driver (Computer Use Agent) ──
mcpServers:
  {self.mcp_server_name}:
    command: {self.mcp_command}
    args: {json.dumps(self.mcp_args)}
    transport: {self.mcp_transport}

# CUA settings
cua:
  max_steps: {self.default_max_steps}
  task_timeout: {self.default_task_timeout}
  require_confirmation: {json.dumps(self.require_confirmation_for)}
"""
        if self.allowed_apps:
            yaml += f"  allowed_apps: {json.dumps(self.allowed_apps)}\n"
        return yaml
    
    def to_native_registration_code(self) -> str:
        """Generate Python code to register CUA tools natively in Hermes.
        
        This is the NATIVE alternative to MCP. Instead of running a separate
        MCP server, tools are registered directly in the Hermes process.
        """
        return """
# ── Native CUA Registration (alternative to MCP) ──
# Add to your Hermes agent's Python entrypoint:

from cua_agent.hermes_native_tools import register_tools

# Register all CUA tools (get_appstate, execute_action, etc.)
cua_tools = register_tools()

# Pass the tools dict to Hermes' tool registry
# This makes tools available with clean names (no mcp_ prefix)
agent.register_toolset(cua_tools)
"""


# ── Native CUA Configuration ──

@dataclass
class NativeConfig:
    """
    Configuration for the native Hermes toolset (no MCP layer).
    
    Tools are registered as direct Python callables in the Hermes process.
    Requires Windows; on other platforms the tools operate in stub mode.
    """
    
    # Toolset metadata
    toolset_name: str = "windows_cua"
    auto_initialize: bool = True
    
    # Caching and performance
    warm_cache_on_init: bool = True
    default_filter_interactive: bool = True
    
    # Security
    enable_security: bool = True
    require_confirmation_for: list[str] = field(default_factory=lambda: [
        "open_app", "type", "key_press"
    ])
    
    def to_registration_block(self) -> str:
        """Generate Python code to register native CUA tools."""
        lines = [
            "# ── Native CUA Registration ──",
            "from cua_agent.hermes_native_tools import register_tools",
            "",
            f"cua_tools = register_tools()",
            f"# Returns dict with toolset_name='{self.toolset_name}', 9 tools",
            "# Available tools: get_appstate, get_appshot, execute_action,",
            "#   list_actions, find_element, take_screenshot,",
            "#   get_focused_element, list_windows, get_provider_info",
            "# Pass to Hermes: agent.register_toolset(cua_tools)",
        ]
        return "\n".join(lines)
    
    @staticmethod
    def tool_list() -> list[dict[str, str]]:
        """Get a list of all native tools and their descriptions."""
        return [
            {"name": "get_appstate", "description": "Get desktop accessibility tree as structured JSON"},
            {"name": "get_appshot", "description": "Get state diff showing what changed since last observation"},
            {"name": "execute_action", "description": "Perform UI action: click, type, press, scroll, open"},
            {"name": "list_actions", "description": "List all supported action types with descriptions"},
            {"name": "find_element", "description": "Search for element by name, position, or automation ID"},
            {"name": "take_screenshot", "description": "Capture full screen as base64 PNG"},
            {"name": "get_focused_element", "description": "Get info about the currently focused element"},
            {"name": "list_windows", "description": "List all open top-level windows"},
            {"name": "get_provider_info", "description": "Get provider diagnostics (platform, uptime, mode)"},
            {"name": "reset_provider", "description": "Reset provider caches and observation history"},
        ]


# ── Hermes System Prompt Additions ──

class HermesIntegration:
    """
    Provides prompt additions and tool templates for integrating
    the CUA driver with the Hermes Agent framework.
    
    When Hermes detects the cua-driver MCP server, these prompts
    tell it how to use CUA tools effectively.
    """
    
    @staticmethod
    def get_system_prompt_addition() -> str:
        """System prompt snippet to add to Hermes when CUA is enabled (MCP mode)."""
        return """
## Computer Use (CUA) Capabilities — MCP Mode

You have access to a Computer Use Agent (CUA) driver that can control
the desktop computer via MCP. This is NOT a vision-based system — instead, it
reads the application state as structured data (accessibility tree).

### Available CUA Tools

1. **get_appstate** — Get the current desktop state as a structured
   list of interactive elements (buttons, text fields, menus, etc.)
   
2. **get_appshot** — Take a state snapshot showing what changed since
   the last observation. Use this after each action to verify results.
   
3. **execute_action** — Perform a UI action: click, type text, press
   keys, scroll, or open an app. Reference elements by their [ID] or
   by role+label path like Button["Submit"].
   
4. **list_actions** — List all available action types with descriptions.

### CUA Workflow

For any computer-use task, follow this pattern:

1. **Plan**: Break the task into steps before starting
2. **Observe**: Call get_appstate to see the current screen
3. **Act**: Call execute_action with element ID or path
4. **Verify**: Call get_appshot to see what changed
5. **Repeat**: Continue until the task is complete

### Important Rules

- Element IDs like [0], [1] refer to positions in the element list
- Always check the full element list before clicking — don't guess
- Use keyboard shortcuts for efficiency (Cmd+C, Cmd+V, Cmd+Tab)
- Wait for actions to complete (use execute_action with wait)
- If an action fails, try an alternative approach
- You CANNOT see screenshots — you work entirely from structured state

### For Complex Tasks

Use the `delegate` tool to spawn specialized subagents:
- A "Navigator" subagent to open apps and navigate the file system
- A "FormFiller" subagent to complete multi-field forms
- A "ContentReader" subagent to extract text from documents
"""
    
    @staticmethod
    def get_native_system_prompt_addition() -> str:
        """System prompt snippet when CUA tools are registered natively (no MCP)."""
        return """
## Computer Use (CUA) Capabilities — Native Mode

You have access to Windows desktop control tools registered DIRECTLY
in the Hermes process (no MCP layer). You can observe and control the
desktop via structured accessibility trees — no vision needed.

### Available Native Tools

1. **get_appstate** — Get the full desktop accessibility tree.
   `get_appstate(filter_interactive_only=True, include_window_info=True)`
   
2. **get_appshot** — Get a state diff since last observation.
   Call this AFTER EVERY ACTION to verify results.
   
3. **execute_action** — Perform a UI action.
   `execute_action(action_type, target, value)`
   
4. **list_actions** — List all 20+ supported action types.
   
5. **find_element** — Search for an element by name, ID, or position.
   `find_element(target, include_tree=False)`
   
6. **take_screenshot** — Capture full screen as base64 PNG (data URI).
   
7. **get_focused_element** — Get detail on the currently focused element.
   
8. **list_windows** — List all open top-level windows.
   
9. **get_provider_info** — Diagnostics about the CUA provider.
   
10. **reset_provider** — Clear caches and observation history.

### Key Differences from MCP Mode

- **Clean tool names** — No `mcp_` prefix. Tools are `get_appstate`, not `mcp_cua_driver_get_appstate`.
- **Direct calls** — Lower latency (~1-5ms vs ~10-50ms per call).
- **Same process** — Tools run in the Hermes process. A crash could affect Hermes.
- **Windows only** — Full functionality on Windows only. Stub mode on other platforms.

### CUA Workflow (same as MCP)

1. Observe → Call be explicit about which app you want to focus on. Use `get_appstate` to see what's on screen.
   `get_appshot` to see what changed since last observation.
2. Use the state to decide on the next action.
3. Call `execute_action` to perform the action.
4. Call `get_appshot` to verify the result.
5. Repeat until the task is complete.

### Important Rules (same as MCP)

- Works purely from structured state, no screenshots processed by the LLM
- Element IDs like [0], [1] refer to positions in the element list
- Always verify with get_appshot after each action
- Use keyboard shortcuts for speed
- When lost, pause, scan, diagnose, then recover with small safe actions
"""
    
    @staticmethod
    def get_subagent_templates() -> dict[str, str]:
        """Get prompt templates for specialized CUA subagents."""
        return {
            "navigator": """You are a Navigator subagent. Your job is to open applications and navigate the file system.

Your task: {task}

You have the CUA driver tools available:
- get_appstate — See what's on screen
- execute_action — Open apps, click, type

Be quick and efficient. Open the app, navigate to the right location, then report back what you found.

When done, report: "Navigated to {location}" with a summary of what's visible.
""",
            
            "form_filler": """You are a FormFiller subagent. Your job is to fill in forms and input fields.

Your task: {task}

You have the CUA driver tools available:
- get_appstate — See the form fields
- execute_action — Click fields, type text, submit

Rules:
1. Always click a field before typing into it
2. Read the field label to know what to enter
3. Use Tab to move between fields when faster than clicking
4. Verify the entered text matches what was requested
5. Click Submit/OK when all fields are filled

When done, report: "Form completed with {number_of_fields} fields filled and submitted."
""",
            
            "content_reader": """You are a ContentReader subagent. Your job is to extract text content from documents, web pages, or applications.

Your task: {task}

You have the CUA driver tools available:
- get_appstate — See the content structure
- execute_action — Scroll, select text, copy

Strategy:
1. Open the document or page
2. Use get_appstate to see headings and content structure
3. Scroll through the content to read it all
4. Copy important text using Cmd+C

When done, report: "Read content from {source} — extracted {summary}."
""",
            
            "monitor": """You are a Monitor subagent. Your job is to watch for specific state changes or conditions and report back.

Monitoring for: {task}

You have the CUA driver tools available:
- get_appstate — Check current state
- get_appshot — Get diff of what changed

Your loop:
1. Observe current state
2. Check if the condition you're watching for has occurred
3. If yes, report back immediately
4. If no, wait {interval_seconds}s and observe again
5. Repeat up to {max_checks} times

When the condition is detected, report: "Condition detected: {condition_details}"
If not detected within the limit, report: "Condition not detected after {max_checks} checks"
""",
        }
    
    @staticmethod
    def get_native_subagent_templates() -> dict[str, str]:
        """Get prompt templates for specialized CUA subagents (native mode)."""
        templates = HermesIntegration.get_subagent_templates()
        # Add native-specific guidance
        for key in templates:
            templates[key] = templates[key].replace(
                "You have the CUA driver tools available:",
                "You have the native CUA tools available (no MCP):"
            )
        return templates
    
    @staticmethod
    def get_task_decomposition_prompt() -> str:
        """
        Prompt for Hermes to decompose complex tasks into CUA sub-agent work.
        
        This is used when the primary Hermes agent receives a complex
        multi-step computer task. It should break it down and delegate.
        """
        return """
## Task Decomposition for CUA

The task "{task}" involves multiple steps. Decompose it into subtasks
that can be delegated to specialized subagents:

### Decomposition Strategy

1. **Identify phases**: What are the sequential phases of this task?
   - e.g., "Open app" → "Navigate to feature" → "Fill form" → "Submit"

2. **Identify parallel work**: Can any steps happen simultaneously?
   - e.g., Reading instructions while a file downloads
   - e.g., Monitoring a progress bar while continuing other work

3. **Assign subagents**:
   - Navigator: App launching, file system navigation
   - FormFiller: Data entry in structured forms
   - ContentReader: Extracting text/values from the UI
   - Monitor: Watching for state changes, errors, progress

4. **Define handoffs**: What data does each subagent need to pass on?
   - e.g., Navigator passes "found file at path X" → FormFiller uses path X

### Output Format

```
Subtasks:
1. [Subagent type]: [Description] → [Output to pass on]
2. [Subagent type]: [Description] → [Output to pass on]
...

Sequential order: [1, 2, 3, ...]
Parallel groups: [[1, 2], [3]]  (1 and 2 can run in parallel)
```
"""


# ── Subagent Result Schema ──

@dataclass
class SubagentResult:
    """
    Schema for what a CUA subagent should report back to the parent Hermes agent.
    """
    subagent_type: str
    task: str
    success: bool
    summary: str
    extracted_data: dict | None = None
    errors: list[str] = field(default_factory=list)
    steps_taken: int = 0
    final_appshot: dict | None = None
    
    def to_dict(self) -> dict:
        return {
            "subagent_type": self.subagent_type,
            "task": self.task[:100],
            "success": self.success,
            "summary": self.summary,
            "extracted_data": self.extracted_data,
            "errors": self.errors,
            "steps_taken": self.steps_taken,
        }
    
    def to_report(self) -> str:
        """Format as a report string for the parent agent."""
        lines = [
            f"=== CUA Subagent: {self.subagent_type} ===",
            f"Task: {self.task[:80]}",
            f"Status: {'✅ Completed' if self.success else '❌ Failed'}",
            f"Summary: {self.summary}",
        ]
        if self.extracted_data:
            lines.append(f"Data: {json.dumps(self.extracted_data, indent=2)}")
        if self.errors:
            lines.append(f"Errors: {'; '.join(self.errors)}")
        if self.steps_taken:
            lines.append(f"Steps: {self.steps_taken}")
        lines.append("=" * 40)
        return "\n".join(lines)
