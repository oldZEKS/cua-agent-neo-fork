"""
__main__.py — CLI entry point for the CUA Agent.

Enables `python -m cua_agent` to run the agent in two modes:
  1. Direct agent mode:  python -m cua_agent "task description"
  2. MCP server mode:    python -m cua_agent --mcp [options]

Examples:
    # Run a task directly
    python -m cua_agent "Open Notepad and type 'Hello World'"

    # Start the MCP server
    python -m cua_agent --mcp

    # MCP server with security and optimization
    python -m cua_agent --mcp --security --optimize

    # Run a task with stub provider (for testing without UI)
    python -m cua_agent --stub "Test the agent flow"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

logger = logging.getLogger("cua-agent")


def run_direct(task: str, use_stub: bool = False, max_steps: int = 10) -> dict:
    """Run the agent loop directly on a task.

    Args:
        task: The task description for the agent
        use_stub: If True, use StubActionProvider (no actual UI actions)
        max_steps: Maximum steps before stopping

    Returns:
        Result dict from the agent loop
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from .agent_loop import AgentLoop, LoopConfig
    from .prompt_builder import PromptBuilder, PromptConfig as PBC
    from .cua_mcp_server import WindowsActionProvider, StubActionProvider

    # Create provider
    if use_stub:
        provider = StubActionProvider()
        print("⚠️  Using STUB provider — no actual UI actions will execute")
    else:
        if sys.platform != "win32":
            print("❌ Windows required for direct execution. Use --stub for testing.")
            sys.exit(1)
        provider = WindowsActionProvider()
        print("🔍 Using Windows action provider")

    # Create configs
    loop_config = LoopConfig(max_steps=max_steps, report_progress=True)
    prompt_config = PBC(max_total_tokens=64000)
    prompt_builder = PromptBuilder(prompt_config)

    # Create and run the loop
    loop = AgentLoop(
        action_provider=provider,
        prompt_builder=prompt_builder,
        config=loop_config,
    )

    print(f"\n📋 Task: {task}")
    print(f"⏱️  Max steps: {max_steps}")
    print(f"{'─' * 50}\n")

    start = time.time()
    result = loop.run(task)
    elapsed = time.time() - start

    # Print summary
    print(f"\n{'─' * 50}")
    status = "✅" if result.get("success") else "❌"
    print(f"{status} Status: {'Completed' if result.get('success') else 'Failed'}")
    print(f"   Steps: {result.get('total_steps', 0)} ({elapsed:.1f}s)")
    if result.get("summary"):
        print(f"   Summary: {result['summary']}")
    if result.get("error"):
        print(f"   Error: {result['error']}")

    return result


def main():
    parser = argparse.ArgumentParser(
        description="CUA Agent — Text-only Computer Use Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Mode
    parser.add_argument("task", nargs="?", default=None,
                        help="Task to execute (if omitted, starts MCP server)")

    # Options for direct mode
    parser.add_argument("--stub", action="store_true", default=False,
                        help="Use stub provider (no actual UI actions)")
    parser.add_argument("--max-steps", type=int, default=10,
                        help="Maximum steps for direct execution (default: 10)")

    # MCP server mode
    parser.add_argument("--mcp", action="store_true", default=False,
                        help="Start as MCP server (default if no task given)")

    # MCP server options (delegated to cua_mcp_server.main)
    parser.add_argument("--collector", choices=["windows", "stub"], default=None)
    parser.add_argument("--log-level", default="WARNING",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--security", action="store_true", default=False)
    parser.add_argument("--no-block-dangerous", action="store_true")
    parser.add_argument("--security-allow-apps", type=str, default=None)
    parser.add_argument("--security-block-apps", type=str, default=None)
    parser.add_argument("--security-read-only", action="store_true", default=False)
    parser.add_argument("--optimize", action="store_true", default=False)

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # Determine mode
    if args.mcp or args.task is None:
        # MCP server mode
        print("🚀 Starting CUA Agent MCP server...")
        # Re-route to cua_mcp_server.main with relevant args
        from .cua_mcp_server import main as mcp_main
        # Inject args into sys.argv for the MCP server parser
        mcp_argv = ["cua_mcp_server.py"]
        if args.collector:
            mcp_argv += ["--collector", args.collector]
        if args.log_level:
            mcp_argv += ["--log-level", args.log_level]
        if args.security:
            mcp_argv += ["--security"]
        if args.no_block_dangerous:
            mcp_argv += ["--no-block-dangerous"]
        if args.security_allow_apps:
            mcp_argv += ["--security-allow-apps", args.security_allow_apps]
        if args.security_block_apps:
            mcp_argv += ["--security-block-apps", args.security_block_apps]
        if args.security_read_only:
            mcp_argv += ["--security-read-only"]
        if args.optimize:
            mcp_argv += ["--optimize"]
        # Temporarily replace sys.argv
        old_argv = sys.argv
        sys.argv = mcp_argv
        try:
            mcp_main()
        finally:
            sys.argv = old_argv
    else:
        # Direct execution mode
        run_direct(args.task, use_stub=args.stub, max_steps=args.max_steps)


if __name__ == "__main__":
    main()
