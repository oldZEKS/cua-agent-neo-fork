---
name: hermes-integration
description: Hermes Agent integration for the CUA (Computer Use Agent) driver. Enables text-only LLMs to control desktop computers via structured accessibility trees — no vision needed. Covers both MCP and native toolset modes, subagent templates, task decomposition, and agent loop orchestration.
license: MIT
metadata:
  author: Buffy (Codebuff)
  version: "2.0"
---

# Hermes Agent — CUA Integration

This skill teaches the agent how to work with the **Hermes Agent** framework integrated with the **CUA (Computer Use Agent) driver**. Hermes is a multi-agent framework that coordinates specialized subagents. The CUA driver provides desktop computer control via structured state (accessibility trees), not screenshots.

Two integration modes are supported:
- **MCP Mode** — CUA runs as a separate MCP stdio server. Better isolation, cross-platform support.
- **Native Mode** — CUA tools registered directly in the Hermes process. Lower latency, cleaner tool names.

## Architecture Overview

### MCP Mode (Default)

```
User Request
    │
    ▼
┌──────────────────┐
│  Hermes Agent    │  ← Orchestrator: decomposes tasks, delegates work
│  (Primary LLM)   │
└──────┬───────────┘
       │ MCP stdio (JSON-RPC over stdio)
       ▼
┌──────────────────┐
│  CUA Driver      │  ← Separate Python process
│  (MCP Server)    │     Crash-isolated from Hermes
└──────┬───────────┘
       │ Windows API / uiautomation
       ▼
┌──────────────────┐
│  Desktop Apps    │  ← The actual computer (Notepad, Chrome, VS Code, etc.)
└──────────────────┘
```

### Native Mode (In-Process)

```
User Request
    │
    ▼
┌──────────────────────────────────┐
│  Hermes Agent Process            │
│                                  │
│  ┌────────────────────────────┐  │
│  │  Native CUA Tools          │  │  ← In-process, no serialization
│  │  ─────────────────         │  │
│  │  get_appstate()            │  │    Direct Python function calls
│  │  execute_action()          │  │    No subprocess management
│  │  get_appshot()             │  │    ~1-5ms per call
│  │  ...                       │  │
│  └──────────┬─────────────────┘  │
└─────────────┼────────────────────┘
              │ Windows API / uiautomation
              ▼
┌──────────────────┐
│  Desktop Apps    │
└──────────────────┘
```

### Key Differences

| Aspect | MCP Mode | Native Mode |
|--------|----------|-------------|
| **Process isolation** | ✅ CUA crash doesn't affect Hermes | ❌ Crash takes down Hermes |
| **Latency** | ~10-50ms per call | ~1-5ms per call |
| **Tool names** | `mcp_cua_driver_get_appstate` | `get_appstate` (clean) |
| **Cross-platform** | ✅ Hermes on Linux → Windows VM | ❌ Hermes must run on Windows |
| **Multi-client** | ✅ One server, many clients | ❌ Per-process only |
| **Hot-reload** | ✅ Restart MCP server mid-session | ❌ Full restart needed |
| **Setup complexity** | YAML config + server start | One-line import + registration |

### When to Use Each

- **Use MCP** when: Hermes runs on Linux/macOS, you need crash isolation, or you need to share the CUA driver across multiple clients.
- **Use Native** when: Hermes runs on bare-metal Windows, you want the lowest latency, or you want clean `get_appstate` / `execute_action` tool names.

---

## 1. Enabling CUA in Hermes

### MCP Configuration

Add the CUA driver as an MCP server in Hermes's `config.yaml`:

```yaml
mcpServers:
  cua-driver:
    command: python
    args: ["-m", "cua_agent.cua_mcp_server"]
    transport: stdio

cua:
  max_steps: 50
  task_timeout: 300        # seconds
  require_confirmation:    # actions that need approval
    - open_app
    - type
    - key_press
  # allowed_apps:          # uncomment to restrict (None = all apps)
  #   - notepad
  #   - chrome
```

### Native Registration

Register CUA tools directly in the Hermes process with a single import:

```python
from cua_agent.hermes_native_tools import register_tools

# Register all 10 CUA tools as a Hermes toolset
cua_tools = register_tools()
# Returns: {
#     "toolset_name": "windows_cua",
#     "toolset_description": "...",
#     "tools": { get_appstate, execute_action, ... }
# }

# Pass to Hermes' tool registry:
agent.register_toolset(cua_tools)
```

### Programmatic Initialization

```python
from cua_agent import CUAConfig, NativeConfig, HermesIntegration

# --- MCP mode ---
mcp_config = CUAConfig(
    mcp_server_name="cua-driver",
    default_max_steps=50,
)
print(mcp_config.to_yaml_block())

# --- Native mode ---
native_config = NativeConfig(
    toolset_name="windows_cua",
    warm_cache_on_init=True,
)
print(native_config.to_registration_block())

# Get system prompt additions
integration = HermesIntegration()

# For MCP mode:
prompt_addition = integration.get_system_prompt_addition()

# For Native mode:
native_prompt = integration.get_native_system_prompt_addition()
```

---

## 2. Available CUA Tools

### Native Tools (Direct Python Callables)

These tools are available natively when using `register_tools()`:

| Tool | Signature | Purpose |
|------|-----------|---------|
| `get_appstate` | `(filter_interactive_only=True, include_window_info=True) -> str` | Get the full desktop accessibility tree as structured JSON |
| `get_appshot` | `() -> str` | Get a state diff showing what changed since last observation |
| `execute_action` | `(action_type: str, target: str = "", value: str = "") -> str` | Perform a UI action: click, type, press, scroll, open |
| `list_actions` | `() -> str` | List all 20+ supported action types with descriptions |
| `find_element` | `(target: str, include_tree: bool = False) -> str` | Search for element by name, position, or automation ID |
| `take_screenshot` | `() -> str` | Capture full screen as base64 PNG (data URI) |
| `get_focused_element` | `() -> str` | Get info about the currently focused element |
| `list_windows` | `() -> str` | List all open top-level windows with titles, PIDs, positions |
| `get_provider_info` | `() -> str` | Provider diagnostics (platform, uptime, stub/live mode) |
| `reset_provider` | `() -> str` | Reset caches, observation history, and appshot state |

### MCP Tools (Via MCP Server)

These same tools are available via MCP when the server is running:

| Tool | Purpose | When to Use |
|------|---------|-------------|
| `get_appstate` | Get the full desktop accessibility tree | Beginning of a task, or after significant navigation |
| `get_appshot` | Get a state diff since last observation | After EVERY action to verify the result |
| `get_element_info` | Look up a specific element's details | When you need precise element data before acting |
| `take_screenshot` | Capture full screen as base64 PNG | When the accessibility tree is insufficient (debugging) |
| `take_region_screenshot` | Capture a screen region as base64 PNG | To inspect a specific area visually |

### UI Actions (Both Modes)

Supported action types for `execute_action`:

| Action | Example | Description |
|--------|---------|-------------|
| `click` | `target="Submit"` | Click an interactive element (button, link, checkbox) |
| `double_click` | `target="File"` | Double-click to open or select |
| `right_click` | `target="Desktop"` | Right-click to show context menu |
| `hover` | `target="Help"` | Hover to reveal tooltips/previews |
| `type` | `value="Hello World"` | Type text into the currently focused element |
| `type_into` | `target="Search", value="query"` | Click first, then type text |
| `key_press` | `target="enter"` or `"ctrl+c"` | Press a key or keyboard combo |
| `scroll` | `target="down"` | Scroll up/down/left/right |
| `select` | `target="Theme", value="Dark"` | Select option from dropdown/combobox |
| `shortcut` | `target="copy"` | Trigger named shortcut (copy, paste, save, new_tab) |
| `open_app` | `target="notepad"` | Launch an application by name |
| `open_url` | `value="https://example.com"` | Open URL in default browser |
| `drag` | `target="File", value="Trash"` | Drag element to target position or element |
| `wait` | `value="500"` | Pause N milliseconds for state settling |
| `resize_window` | `value="1024,768"` | Resize foreground window |
| `minimize_window` | — | Minimize foreground window to taskbar |
| `maximize_window` | — | Maximize foreground window to full screen |
| `close_window` | — | Close foreground window gracefully |
| `take_screenshot` | — | Capture full screen as base64 PNG |
| `take_region_screenshot` | `target="100,200,800,600"` | Capture a screen region |
| `find_element` | `target="Submit"` | Search for and return element info |

### Autonomous Execution

| Tool | Purpose |
|------|---------|
| `run_task` | Run the full autonomous Observe→Think→Act loop for a task |
| `observe_and_act` | Single step of observation + action (step-by-step control) |
| `list_actions` | List all supported action types with descriptions |

---

## 3. The Observe→Think→Act Loop

Every computer-use task follows this loop. This is the core workflow.

### Step-by-Step Pattern

```
┌─────────────────────────────────────────────────┐
│  1. OBSERVE                                      │
│     Call get_appstate to see the current screen  │
│     Parse the element list: roles, labels, IDs   │
└──────────────────┬──────────────────────────────┘
                   ▼
┌─────────────────────────────────────────────────┐
│  2. THINK                                        │
│     Which element to interact with?              │
│     What action type fits this element's role?   │
│     What outcome do you expect?                  │
│     Output a single JSON action object           │
└──────────────────┬──────────────────────────────┘
                   ▼
┌─────────────────────────────────────────────────┐
│  3. ACT                                          │
│     Call execute_action with your chosen action  │
└──────────────────┬──────────────────────────────┘
                   ▼
┌─────────────────────────────────────────────────┐
│  4. VERIFY                                       │
│     Call get_appshot to see what changed         │
│     Did the outcome match your expectation?      │
│     If not, diagnose and recover                 │
└──────────────────┬──────────────────────────────┘
                   ▼
            Repeat from Step 1
```

### Element IDs and Targeting

Elements are referenced by their position in the element list as `[0]`, `[1]`, `[2]`, etc. Always check the element list and reference elements by their ID.

```json
// Click the first element in the list
{"action": "click", "target": "[0]"}

// Type into element [2]
{"action": "type_into", "target": "[2]", "value": "Hello World"}

// Press Enter
{"action": "key_press", "target": "enter"}
```

### Action Selection by Element Role

| Element Role | Recommended Action |
|--------------|-------------------|
| Button | `click` |
| TextField | `click` to focus → `type` |
| ComboBox | `click` to expand → `select` with value |
| CheckBox | `click` to toggle |
| RadioButton | `click` to select |
| Link | `click` to navigate |
| Menu / MenuItem | `click` to open → `click` item |
| Slider | `click` or `drag` to adjust |
| Table / List | `scroll` to find target → `click` |
| Tab | `click` to switch tab |

---

## 4. Task Decomposition

Complex multi-step tasks should be broken down and delegated to specialized CUA subagents. Hermes supports this via the `delegate` tool.

### Decomposition Strategy

1. **Identify phases** — What are the sequential phases?
   - e.g., "Open app" → "Navigate to feature" → "Fill form" → "Submit"

2. **Identify parallel work** — Can any steps happen simultaneously?
   - Reading instructions while a file downloads
   - Monitoring progress while continuing other work

3. **Assign subagents** — Who handles each phase?

4. **Define handoffs** — What data flows between subagents?

### Example Decomposition

```
Task: "Open Excel, create a budget spreadsheet with 5 categories,
       fill in Q1 data, and save to Desktop"

Subtasks:
1. [Navigator]: Open Excel and create new workbook
   → "Excel is open with a blank workbook"
2. [Navigator]: Navigate to cell A1 and enter category headers
   → "Category headers entered in columns A-E"
3. [FormFiller]: Fill in Q1 data for each category
   → "All Q1 data entered across 5 categories"
4. [Navigator]: Save file to Desktop as "Q1-Budget.xlsx"
   → "File saved successfully"

Sequential order: [1, 2, 3, 4]
Parallel groups: none
```

---

## 5. Subagent Templates

These are ready-to-use subagent prompt templates for common CUA tasks.

### Navigator Subagent

Launches apps and navigates the file system.

```python
template = """
You are a Navigator subagent. Your job is to open applications
and navigate the file system.

Your task: {task}

You have the CUA tools available:
- get_appstate — See what's on screen
- execute_action — Open apps, click, type

Be quick and efficient. Open the app, navigate to the right
location, then report back what you found.

When done, report: "Navigated to {location}" with a summary
of what's visible.
"""
```

### FormFiller Subagent

Completes multi-field forms and input dialogs.

```python
template = """
You are a FormFiller subagent. Your job is to fill in forms
and input fields.

Your task: {task}

Rules:
1. Always click a field before typing into it
2. Read the field label to know what to enter
3. Use Tab to move between fields when faster
4. Verify the entered text matches what was requested
5. Click Submit/OK when all fields are filled

When done, report: "Form completed with {N} fields filled."
"""
```

### ContentReader Subagent

Extracts text content from documents, web pages, or apps.

```python
template = """
You are a ContentReader subagent. Your job is to extract text
content from documents, web pages, or applications.

Your task: {task}

Strategy:
1. Open the document or page
2. Use get_appstate to see headings and content structure
3. Scroll through the content to read it all
4. Copy important text using Cmd+C

When done, report: "Read content from {source} — extracted {summary}."
"""
```

### Monitor Subagent

Watches for specific state changes and reports back.

```python
template = """
You are a Monitor subagent. Your job is to watch for specific
state changes or conditions and report back.

Monitoring for: {task}

Your loop:
1. Observe current state
2. Check if the condition has occurred
3. If yes, report back immediately
4. If no, wait {interval_seconds}s and observe again
5. Repeat up to {max_checks} times

When detected, report: "Condition detected: {details}"
If not detected: "Condition not detected after {N} checks"
"""
```

---

## 6. Error Recovery Patterns

When actions fail, use these recovery strategies.

### Common Errors and Fixes

| Error Pattern | Likely Cause | Recovery |
|--------------|-------------|----------|
| Element not found | Wrong window/tab focused | Call `get_appstate` to verify current state. Press Alt+Tab to switch windows. |
| Click didn't work | Element obscured or not ready | Wait 500ms, try again. Use `find_element` to verify position. |
| Type didn't input text | Wrong element focused | Click the target first, then type. Use `type_into` instead of `type`. |
| Unexpected dialog | App prompted for confirmation | Look for "OK", "Save", "Cancel", "Yes", "No" buttons and click appropriately. |
| Action timed out | App is busy / not responding | Wait 2-3 seconds, retry. Press Escape to dismiss frozen states. |

### Recovery Protocol

When the desktop state doesn't match expectations:

1. **PAUSE** — Do NOT act immediately. An incorrect action when lost makes things worse.
2. **SCAN** — Read every element in the current state. Look for familiar landmarks: window titles, known buttons, the taskbar.
3. **DIAGNOSE** — What's different from what you expected? New dialog? Window closed?
4. **RECOVER** with small, safe actions:
   - Press Escape to dismiss unexpected dialogs
   - Press Alt+Tab to cycle back to the task window
   - Click the window title bar to bring it to focus
   - Find and click "Close" or "Cancel" on unexpected dialogs
5. **RETRY** — Once context is regained, continue toward the original goal.

**Key principle:** When confused, do less, not more. A single Escape key press is safer than clicking unknown elements.

---

## 7. Best Practices

### DO
- ✅ Call `get_appshot` after every action to verify outcomes
- ✅ Use element IDs (`[0]`, `[1]`) — they're the most reliable reference
- ✅ Wait 500-1000ms between critical actions for state to settle
- ✅ Use keyboard shortcuts for efficiency (`Ctrl+C`, `Ctrl+V`, `Ctrl+Tab`)
- ✅ Break complex tasks into subagent work using the `delegate` tool
- ✅ Take your time — deliberate analysis produces correct results
- ✅ Examine EVERY element in the list before acting — never skip ahead

### DON'T
- ❌ Don't guess element coordinates — use element IDs
- ❌ Don't combine multiple actions in one step — one action at a time
- ❌ Don't ignore the appshot — it tells you if your action worked
- ❌ Don't rush — rushing causes errors
- ❌ Don't assume screenshots are available (they're for debugging only)
- ❌ Don't skip the analysis phase before outputting an action

### Element Targeting Priority

When referencing elements, use this priority order:

1. **Element ID** — `[0]`, `[1]`, `[2]` — most reliable, references position in the element list
2. **Element name** — `Button["Submit"]`, `TextField["Search"]` — role+label path
3. **Automation ID** — `id:myButton` — for elements with stable automation IDs
4. **Position** — `200,300` — last resort, only when element can't be found by name/ID

---

## 8. Quick Reference

### Subagent Result Schema

When a subagent completes its work, it should report back using this structure:

```python
{
    "subagent_type": "navigator | form_filler | content_reader | monitor",
    "task": "The task description",
    "success": True/False,
    "summary": "What was accomplished",
    "extracted_data": { ... },  # optional: data extracted from the UI
    "errors": [],                # optional: any errors encountered
    "steps_taken": 5,            # how many actions were executed
}
```

### Native Tools Quick-Start

```python
from cua_agent.hermes_native_tools import register_tools

# Register all CUA tools
cua_tools = register_tools()

# Check available tools
tools = cua_tools["tools"]  # dict of 10 tool definitions

# Available tool names:
# - get_appstate       - get_appshot         - execute_action
# - list_actions       - find_element        - take_screenshot
# - get_focused_element - list_windows       - get_provider_info
# - reset_provider
```

### MCP Server CLI

```bash
# Start the CUA MCP server (for Hermes to connect via stdio)
python -m cua_agent.cua_mcp_server

# Run directly with a task (no MCP needed)
python -m cua_agent "Open Notepad and type 'Hello World'"
```

### Programmatic Agent Run

```python
from cua_agent import run_agent

result = run_agent(
    task="Open Notepad and type 'Hello World'",
    max_steps=50,
    use_stub=False,  # Set to True for testing without actual UI
)
print(f"Success: {result['success']}")
print(f"Steps: {result['total_steps']}")
print(f"Summary: {result.get('summary', '')}")
```

---

## 9. Security

The CUA driver includes a security context that classifies actions by danger level.

| Level | Value | Behavior |
|-------|-------|----------|
| `SAFE` | 0 | Always allowed |
| `CAUTION` | 1 | Allowed, logged with warning |
| `DANGEROUS` | 2 | Requires confirmation |
| `CRITICAL` | 3 | Blocked by default |

**SAFE actions:** click, type, scroll, double_click, right_click, hover, drag, select, resize_window, minimize_window, maximize_window, close_window, take_screenshot, shortcut, wait.

**DANGEROUS actions:** Opening shell apps (cmd, powershell), registry editor, disk management — flagged dynamically.

When using Hermes with CUA, respect the security configuration — dangerous actions will be blocked or require confirmation depending on the setup.
