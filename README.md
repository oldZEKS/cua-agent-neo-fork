# CUA (Computer Use Agent) Driver

A production-grade, text-only Windows GUI automation driver. It exposes local desktop apps through accessibility trees, enabling text-only LLMs (such as DeepSeek-v4, Gemini, or Claude) to inspect, control, and interact with the Windows desktop without requiring expensive screenshot/vision processing.

Designed for seamless integration with multi-agent systems (e.g., the **Hermes Agent** framework) via the **Model Context Protocol (MCP)**.

---

## Key Features

- **Text-Only UI Control**: Employs Windows UI Automation (UIA) to serialize the desktop accessibility tree into structured JSON. Low token consumption, high reliability.
- **Robust Orchestration**: Implements an autonomous Observe $\rightarrow$ Think $\rightarrow$ Act loop with `AgentLoop`.
- **Latency & Token Optimization**:
  - **ActionMerger**: Automatically coalesces consecutive character typing or scrolling actions to reduce roundtrips.
  - **ObservationCache**: Caches UI trees to avoid redundant UIA crawls.
  - **TokenOptimizer**: Truncates and compresses large UI trees, omitting redundant elements to fit context windows.
  - **PerformanceTimer**: Tracks execution timing for each phase (UIA collection, LLM inference, Action dispatch).
- **Hardened Security**:
  - **SandboxConfig**: Restricts actions to specific application domains.
  - **ActionValidator**: Blocks unsafe operations (e.g., keyboard shortcuts like Alt+F4, Ctrl+Alt+Del).
  - **RateLimiter**: Controls action dispatch frequencies.
- **Flexible Transports**: Supports both native local **Stdio** MCP connections and remote **SSE** (Server-Sent Events) HTTP transport (ideal for Linux/Mac clients controlling a Windows VM).
- **Error Recovery**: Automatically attempts to recover from stale UI handles, app focus shifts, or failed window restores using retry strategies.

---

## Architecture Overview

```
                  ┌──────────────────────┐
                  │    Primary Agent     │
                  │   (Hermes/Orchestrator)
                  └──────────┬───────────┘
                             │
            ┌────────────────┴────────────────┐
            ▼ (MCP Stdio)                     ▼ (MCP SSE over HTTP)
   ┌─────────────────┐               ┌─────────────────┐
   │   CUA Driver    │               │   CUA Driver    │
   │ (Local Process) │               │  (SSE Server)   │
   └────────┬────────┘               └────────┬────────┘
            │                                 │
            └────────────────┬────────────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │   Windows OS / UIA   │
                  └──────────┬───────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
         [Notepad]        [Chrome]      [Spotify]
```

---

## Quick Start

### Prerequisites

- **OS**: Windows 10/11 or Windows Server.
- **Python**: Version $\ge$ 3.11.
- **System Tool (Optional)**: Tesseract OCR (if text extraction from image fallbacks is required).

### Installation

Clone the repository and install the package in editable mode:

```powershell
# Install package dependencies
pip install -e .
```

### Running the Agent Directly (CLI)

To test the agent's autonomous loop on a task:

```powershell
# Run a simple task (runs on Windows GUI)
python -m cua_agent "Open Notepad, type 'Hello from CUA', and save it"

# Run in mock/stub mode (safe for non-Windows environments)
python -m cua_agent --stub "Run mock loop"
```

---

## MCP Server Integration

The driver can be exposed as an MCP Server. This allows any MCP client (such as Hermes or Claude Desktop) to invoke the driver's capabilities as tools.

### Stdio Transport (Default)

Launch the stdio server:

```powershell
python -m cua_agent.cua_mcp_server
```

To configure it in your client's settings (e.g., `config.yaml` or Claude Desktop config):

```json
{
  "mcpServers": {
    "cua-driver": {
      "command": "python",
      "args": ["-m", "cua_agent.cua_mcp_server"]
    }
  }
}
```

### SSE Transport (Server-Sent Events)

If your primary agent is running in a different environment (like WSL, Linux, or a separate server) and needs to control a remote Windows desktop:

1. **Start the SSE server on the Windows machine**:
   ```powershell
   python -m cua_agent.run_sse --port 8000 --security --optimize
   ```
2. **Configure your remote client** to connect to the Windows machine IP:
   ```yaml
   mcpServers:
     cua-driver:
       url: "http://<WINDOWS_IP_ADDRESS>:8000/sse"
       transport: sse
   ```

---

## Available MCP Tools

- **`get_appstate`**: Collects the accessibility tree of the foreground window or specified application and returns it as a JSON representation.
- **`get_appshot`**: Captures a snapshot containing the tree structure, along with delta information comparing it to the previous state.
- **`execute_action`**: Dispatches a GUI instruction (e.g., `click`, `type`, `key_press`, `scroll`, `hover`, `drag`, `select`).
- **`list_actions`**: Returns a comprehensive list of all supported GUI commands.
- **`observe_and_act`**: Executes a single step of the Observe $\rightarrow$ Think $\rightarrow$ Act loop.
- **`run_task`**: Executes the full, autonomous loop from start to completion.

For a detailed reference on all supported actions and parameters, see [ACTIONS.md](ACTIONS.md).

For guide details on setting up multi-agent flows with Hermes, see [HERMES.md](HERMES.md).

---

## Running the Test Suite

The package includes a comprehensive suite of unit tests. Run them using `pytest`:

```powershell
# Install pytest
pip install pytest

# Run tests
pytest
```

To run individual module test scripts directly:

```powershell
python -m cua_agent.tests.test_optimization
python -m cua_agent.tests.test_prompt_builder
```

---

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
