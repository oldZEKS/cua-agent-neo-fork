# CUA Agent — All Actions Reference

This document catalogs every action the CUA agent can perform. Actions are executed via
`execute_action()` (tool-level) or dispatched through `provider.act()` (programmatic).

---

## 1. Element Interaction

### `click`
Click an interactive element (button, link, checkbox, etc.).

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"click"` |
| `target` | ✅ | Element name, position `"200,300"`, or automation ID `"id:xxx"` |

```json
{"action": "click", "target": "[0]"}
{"action": "click", "target": "Submit"}
{"action": "click", "target": "200,300"}
```

### `double_click`
Double-click an element (opens files, selects words).

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"double_click"` |
| `target` | ✅ | Element name or position |

```json
{"action": "double_click", "target": "[3]"}
```

### `right_click`
Right-click to show a context menu.

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"right_click"` |
| `target` | ✅ | Element name or position |

```json
{"action": "right_click", "target": "[1]"}
```

### `hover`
Hover over an element to reveal tooltips, previews, or hover effects.

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"hover"` |
| `target` | ✅ | Element name, `"id:xxx"`, or `"x,y"` position |

```json
{"action": "hover", "target": "[0]"}
{"action": "hover", "target": "200,300"}
```

### `drag`
Drag a source element to a target position or element (drag-and-drop).

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"drag"` |
| `target` | ✅ | Source element name, `"id:xxx"`, or `"x,y"` position |
| `value` | ✅ | Target position `"400,500"` or element name to drop on |

```json
{"action": "drag", "target": "[0]", "value": "400,500"}
{"action": "drag", "target": "[0]", "value": "Trash"}
```

### `select`
Select an option from a dropdown/combobox by value. Clicks the dropdown to expand,
finds the option by name, and clicks it. Falls back to keyboard (Alt+Down, type, Tab).

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"select"` |
| `target` | ✅ | Combobox/dropdown element name or position |
| `value` | ✅ | Option text/value to select |

```json
{"action": "select", "target": "[3]", "value": "Option A"}
```

---

## 2. Text Input

### `type`
Type text into the currently focused element.

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"type"` |
| `value` | ✅ | Text to type |

```json
{"action": "type", "value": "hello"}
```

### `type_into`
Click an element first, then type text into it (target-first typing).

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"type_into"` |
| `target` | ✅ | Element name or position to focus |
| `value` | ✅ | Text to type |

```json
{"action": "type_into", "target": "[2]", "value": "hello"}
```

### `clear_text`
Clear a text field (click to focus, Ctrl+A, Delete).

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"clear_text"` |
| `target` | ✅ | Element name or position |

```json
{"action": "clear_text", "target": "[2]"}
```

### `key_press`
Press a key or keyboard shortcut.

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"key_press"` |
| `target` | ✅ | Key name or combo |

Supported keys: `enter`, `tab`, `escape`, `delete`, `backspace`, `space`,
`up`, `down`, `left`, `right`, `home`, `end`, `pageup`, `pagedown`, `f1`–`f12`.

Supported combos: `ctrl+c`, `alt+f4`, `ctrl+shift+s`, `win+d`, etc.

```json
{"action": "key_press", "target": "enter"}
{"action": "key_press", "target": "ctrl+c"}
{"action": "key_press", "target": "alt+f4"}
```

---

## 3. Screenshot & Visual

### `take_screenshot`
Capture the full screen as a base64-encoded PNG image using Pillow.

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"take_screenshot"` |

```json
{"action": "take_screenshot"}
```

Returns: `.screenshot` — `data:image/png;base64,...` data URI,
`.width`, `.height`, `.format` = `"png"`.

### `take_region_screenshot`
Capture a screenshot of a specific screen region by coordinates or element name.

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"take_region_screenshot"` |
| `target` | ❌ | Region `"x,y,width,height"` or element name |
| `value` | ❌ | Optional fallback region `"x,y,width,height"` |

```json
{"action": "take_region_screenshot", "target": "100,200,400,300"}
{"action": "take_region_screenshot", "target": "Submit"}
```

Returns: `.screenshot`, `.region`, `.width`, `.height`, `.format` = `"png"`.

### `find_element`
Find a UI element and return its structured info (name, role, position, size, state).

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"find_element"` |
| `target` | ✅ | Element name, `"id:xxx"`, or `"x,y"` position |

```json
{"action": "find_element", "target": "Submit"}
{"action": "find_element", "target": "200,300"}
```

Returns: info object with `name`, `role`, `automation_id`, `pos`, `size`,
`rect`, `focused`, `enabled`, `value`.

---

## 4. Window Management

### `resize_window`
Resize the foreground window to specified dimensions.

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"resize_window"` |
| `value` | ✅ | Dimensions in `"width,height"` format (e.g., `"1024,768"`) |

Bounds: 200–7680 × 100–4320 pixels.

```json
{"action": "resize_window", "value": "1024,768"}
```

### `minimize_window`
Minimize the foreground window to the taskbar.

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"minimize_window"` |

```json
{"action": "minimize_window"}
```

Implementation: `ShowWindow(hwnd, SW_MINIMIZE=6)`.

### `maximize_window`
Maximize the foreground window to fill the screen.

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"maximize_window"` |

```json
{"action": "maximize_window"}
```

Implementation: `ShowWindow(hwnd, SW_MAXIMIZE=3)`.

### `close_window`
Close the foreground window gracefully via WM_CLOSE.

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"close_window"` |

```json
{"action": "close_window"}
```

Implementation: `PostMessageW(hwnd, WM_CLOSE=0x0010)`. Allows apps to prompt
"Save changes?" before closing (graceful, not forced).

---

## 5. Keyboard Shortcuts

### `shortcut`
Trigger a named keyboard shortcut. Maps human-readable names to platform-specific
key combos. Unknown names fall through to `key_press`.

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"shortcut"` |
| `target` | ✅ | Shortcut name or raw key combo |

#### Text Editing Shortcuts
| Name | Key Combo | Description |
|------|-----------|-------------|
| `copy` | `Ctrl+C` | Copy selected text |
| `paste` | `Ctrl+V` | Paste from clipboard |
| `cut` | `Ctrl+X` | Cut selected text |
| `select_all` | `Ctrl+A` | Select all content |
| `undo` | `Ctrl+Z` | Undo last action |
| `redo` | `Ctrl+Y` | Redo previously undone action |
| `save` | `Ctrl+S` | Save current document |
| `find` | `Ctrl+F` | Open find dialog |
| `replace` | `Ctrl+H` | Open find & replace |
| `find_next` | `F3` | Find next match |
| `new_document` | `Ctrl+N` | Create new document |
| `open_file` | `Ctrl+O` | Open file dialog |
| `print` | `Ctrl+P` | Print dialog |
| `bold` | `Ctrl+B` | Toggle bold |
| `italic` | `Ctrl+I` | Toggle italic |
| `underline` | `Ctrl+U` | Toggle underline |

#### Navigation Shortcuts
| Name | Key Combo | Description |
|------|-----------|-------------|
| `close_tab` | `Ctrl+F4` | Close current tab |
| `close_window` | `Alt+F4` | Close current window |
| `new_tab` | `Ctrl+T` | Open new tab |
| `switch_next_tab` | `Ctrl+Tab` | Switch to next tab |
| `switch_prev_tab` | `Ctrl+Shift+Tab` | Switch to previous tab |
| `refresh` | `F5` | Refresh page |
| `hard_refresh` | `Ctrl+F5` | Hard refresh (clear cache) |
| `go_back` | `Alt+Left` | Navigate back |
| `go_forward` | `Alt+Right` | Navigate forward |

#### System Shortcuts
| Name | Key Combo | Description |
|------|-----------|-------------|
| `lock_screen` | `Win+L` | Lock workstation |
| `task_switcher` | `Alt+Tab` | Switch between open apps |
| `show_desktop` | `Win+D` | Minimize all / show desktop |
| `screenshot` | `Win+Shift+S` | Snipping tool screenshot |
| `open_run` | `Win+R` | Open Run dialog |
| `open_settings` | `Win+I` | Open Settings |
| `open_search` | `Win+S` | Open Windows Search |
| `open_file_explorer` | `Win+E` | Open File Explorer |
| `minimize_all` | `Win+M` | Minimize all windows |

#### Developer Shortcuts
| Name | Key Combo | Description |
|------|-----------|-------------|
| `comment` | `Ctrl+K, Ctrl+C` | Toggle comment (VS Code) |
| `uncomment` | `Ctrl+K, Ctrl+U` | Uncomment (VS Code) |
| `format_code` | `Shift+Alt+F` | Format document |
| `build` | `Ctrl+Shift+B` | Build project |
| `run` | `F5` | Run / start debugging |
| `debug` | `F5` | Start debugging |
| `step_over` | `F10` | Step over (debugger) |
| `step_into` | `F11` | Step into (debugger) |
| `toggle_breakpoint` | `F9` | Toggle breakpoint |

```json
{"action": "shortcut", "target": "copy"}
{"action": "shortcut", "target": "new_tab"}
{"action": "shortcut", "target": "lock_screen"}
{"action": "shortcut", "target": "ctrl+shift+s"}  // raw combo passthrough
```

---

## 6. Navigation & Scrolling

### `scroll`
Scroll in a direction using mouse wheel or arrow keys.

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"scroll"` |
| `target` | ✅ | Direction: `"up"`, `"down"`, `"left"`, `"right"` |
| `value` | ❌ | Number of scroll steps (1–10, default 3) |

```json
{"action": "scroll", "target": "down", "value": "3"}
{"action": "scroll", "target": "left"}
```

---

## 7. Application & URL

### `open_app`
Open an application by name or path. Uses `start` shell command on Windows.

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"open_app"` |
| `target` | ✅ | App name or path |

```json
{"action": "open_app", "target": "notepad"}
{"action": "open_app", "target": "chrome"}
```

### `open_url`
Open a URL in the default browser.

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"open_url"` |
| `value` | ✅ | Full URL including `https://` |

```json
{"action": "open_url", "value": "https://example.com"}
```

---

## 8. Timing & Flow Control

### `wait`
Wait before the next action (for state settling, animation completion, etc.).

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"wait"` |
| `value` | ✅ | Milliseconds to wait (500–2000 typical) |

```json
{"action": "wait", "value": "1000"}
```

### `done`
Signal that the task is complete. Used by the agent loop, not directly callable
via `execute_action`.

| Field | Required | Description |
|-------|----------|-------------|
| `action` | ✅ | `"done"` |
| `result` | ✅ | Summary of what was accomplished |

```json
{"action": "done", "result": "Successfully opened Notepad and typed 'Hello World'"}
```

---

## 9. Information (MCP Tools)

These are available as MCP tools rather than `execute_action` types:

| Tool | Description |
|------|-------------|
| `get_appstate` | Get the full accessibility tree of the foreground window |
| `get_appshot` | Get a state diff/snapshot since the last observation |
| `get_element_info` | Look up a specific element's details |
| `take_screenshot` | Capture full screen as base64 PNG (also callable as action) |
| `take_region_screenshot` | Capture a screen region as base64 PNG (also callable as action) |
| `list_actions` | List all supported action types with descriptions |
| `run_task` | Run the full autonomous agent loop for a task |
| `observe_and_act` | Single-step observe→think→act (step-by-step control) |

---

## 10. Implementation Details

### Windows API (WindowsActionProvider)

| Action | API Call | Notes |
|--------|----------|-------|
| `click` | `auto.Click(x, y)` | Uses `find_element()` for robust targeting |
| `hover` | `auto.MoveTo(x, y)` | 300ms tooltip wait |
| `drag` | `mouse_event(LEFTDOWN)`, `SetCursorPos` steps, `mouse_event(LEFTUP)` | 10–20 step smooth path |
| `select` | Click combobox → `find_element(option)` → Click | Falls back to `Alt+Down` + type + Tab |
| `resize_window` | `SetWindowPos(hwnd, 0, x, y, w, h, SWP_NOZORDER)` | Validates bounds |
| `minimize_window` | `ShowWindow(hwnd, SW_MINIMIZE=6)` | — |
| `maximize_window` | `ShowWindow(hwnd, SW_MAXIMIZE=3)` | — |
| `close_window` | `PostMessageW(hwnd, WM_CLOSE=0x0010)` | Graceful close |
| `take_screenshot` | `PIL.ImageGrab.grab()` | Returns base64 data URI |
| `take_region_screenshot` | `PIL.ImageGrab.grab(bbox=region)` | Returns base64 data URI |
| `shortcut` | `auto.SendKeys(combo)` | Maps 30+ named shortcuts |
| `type` / `key_press` | `auto.SendKeys(text)` | Escapes special characters |
| `scroll` | `auto.WheelDown(n)` / `auto.WheelUp(n)` | Arrow keys for horizontal |

### MCP Tools via FastMCP

Server runs on stdio with JSON-RPC 2.0. Compatible with any MCP client
(Hermes Agent, Claude Desktop, etc.).

---

## 11. Security Classification

All actions are classified by `security.py`:

| Level | Value | Behavior |
|-------|-------|----------|
| `SAFE` | 0 | Always allowed |
| `CAUTION` | 1 | Allowed, logged with warning |
| `DANGEROUS` | 2 | Requires confirmation (or blocked via `--block-dangerous`) |
| `CRITICAL` | 3 | Blocked by default |

**SAFE actions:** `click`, `type`, `scroll`, `double_click`, `right_click`,
`hover`, `drag`, `select`, `resize_window`, `minimize_window`, `maximize_window`,
`close_window`, `take_screenshot`, `take_region_screenshot`, `shortcut`, `wait`.

**CAUTION/DANGEROUS apps:** Shell (`cmd`, `powershell`), registry editor,
disk management, task manager, etc. — flagged dynamically by pattern matching.

---

## 12. Testing

All actions have three levels of tests:

| Level | File | Purpose |
|-------|------|---------|
| Dispatch | `tests/test_agent_loop.py` | Verifies `_act()` dispatches correctly |
| MCP Tool | `tests/test_mcp_tools.py` | Verifies `execute_action()` tool function |
| E2E | `tests/test_e2e_mcp_server.py` | Verifies full pipeline through function API |

Run:
```bash
python -m cua_agent.tests.test_agent_loop      # 31 tests
python -m cua_agent.tests.test_mcp_tools        # 29 tests
python -m cua_agent.tests.test_e2e_mcp_server   # 20 tests
python -m cua_agent.tests.test_security         # 26 tests
```
