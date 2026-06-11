"""
cua_agent — Text-only Computer Use Agent for DeepSeek v4.

A complete agent loop system that enables text-only LLMs (no vision)
to control desktop computers by reading structured accessibility trees
(appstate/appshots) instead of processing screenshots.

Designed for integration with the Hermes Agent framework via MCP.

Key components:
    - AgentLoop: The Observe→Think→Act orchestration loop
    - PromptBuilder: Builds optimized prompts for text-only LLMs
    - ErrorRecovery: Classifies errors and applies retry strategies
    - SecurityContext: Rate limiting, action validation, sandboxing
    - OptimizationConfig: Action merging, caching, performance timing
    - HermesIntegration: Hermes subagent templates and MCP configs
"""

from .agent_loop import AgentLoop, LoopConfig
from .prompt_builder import PromptBuilder, PromptConfig
from .error_recovery import ErrorRecovery, RetryPolicy
from .hermes_integration import HermesIntegration, CUAConfig, NativeConfig
from .appshot_manager import AppshotManager
from .cua_mcp_server import main as run_mcp_server
from .cua_mcp_server import WindowsActionProvider, StubActionProvider
from .agent_loop import MCPActionProvider

from .hermes_native_tools import register_tools as register_native_tools
from .security import SecurityContext, ActionValidator, RateLimiter, SandboxConfig
from .optimization import (
    ActionMerger, ObservationCache, PerformanceTimer,
    TokenOptimizer, OptimizationConfig,
)

try:
    from .ax_collector_windows import WindowsCollector, is_available as windows_available
except ImportError:
    WindowsCollector = None  # type: ignore
    def windows_available() -> bool: return False


# ── Convenience runner ──

def run_agent(
    task: str,
    max_steps: int = 50,
    model: str = "deepseek-chat",
    temperature: float = 0.1,
    use_stub: bool = False,
) -> dict:
    """Run the CUA agent on a task with minimal setup.

    This is the quickest way to use the agent from code:

        from cua_agent import run_agent
        result = run_agent("Open Notepad and type 'Hello World'")

    Args:
        task: The task description
        max_steps: Maximum agent steps
        model: LLM model name
        temperature: LLM temperature
        use_stub: If True, use StubActionProvider (no actual UI)

    Returns:
        Result dict with success, steps, summary, etc.
    """
    if use_stub:
        provider = StubActionProvider()
    elif windows_available():
        provider = WindowsActionProvider()
    else:
        raise RuntimeError(
            "No action provider available. Windows is required, or pass use_stub=True"
        )

    config = LoopConfig(max_steps=max_steps, model=model, temperature=temperature,
                        report_progress=True)
    prompt_config = PromptConfig(max_total_tokens=64000)
    prompt_builder = PromptBuilder(prompt_config)

    loop = AgentLoop(
        action_provider=provider,
        prompt_builder=prompt_builder,
        config=config,
    )
    return loop.run(task)


__all__ = [
    "AgentLoop",
    "PromptBuilder",
    "PromptConfig",
    "ErrorRecovery",
    "RetryPolicy",
    "HermesIntegration",
    "CUAConfig",
    "NativeConfig",
    "AppshotManager",
    "register_native_tools",
    "run_mcp_server",
    "run_agent",
    "WindowsCollector",
    "windows_available",
    "SecurityContext",
    "ActionValidator",
    "RateLimiter",
    "SandboxConfig",
    "ActionMerger",
    "ObservationCache",
    "PerformanceTimer",
    "TokenOptimizer",
    "OptimizationConfig",
    "WindowsActionProvider",
    "StubActionProvider",
    "MCPActionProvider",
]

__version__ = "0.1.0"
