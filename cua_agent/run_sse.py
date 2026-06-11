"""
run_sse.py — Run the CUA MCP server over SSE (Server-Sent Events) transport.

Allows Hermes Agent running on a different machine (e.g., WSL, Linux, Mac)
to connect to the CUA driver over HTTP instead of stdio.

Usage:
    # Start server on all interfaces
    python -m cua_agent.run_sse --host 0.0.0.0 --port 8000

    # Start on specific interface
    python -m cua_agent.run_sse --host 192.168.1.100 --port 8000

    # Start with security and optimization
    python -m cua_agent.run_sse --port 8000 --security --optimize

Hermes config.yaml for remote connection:
    mcpServers:
      cua-driver:
        command: python
        args: []
        transport: sse
        url: http://{WINDOWS_IP}:8000/sse
"""

from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger("cua-sse")


def main():
    parser = argparse.ArgumentParser(
        description="CUA MCP Server (SSE transport)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="Bind address (default: 0.0.0.0 — all interfaces)",
    )
    parser.add_argument(
        "--port", type=int, default=8000,
        help="Listen port (default: 8000)",
    )
    parser.add_argument(
        "--log-level", default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument(
        "--security", action="store_true", default=False,
        help="Enable security context (rate limiting, action validation)",
    )
    parser.add_argument(
        "--no-block-dangerous", action="store_true",
        help="Don't block dangerous actions, just warn",
    )
    parser.add_argument(
        "--security-allow-apps", type=str, default=None,
        help="Comma-separated list of allowed app names",
    )
    parser.add_argument(
        "--security-block-apps", type=str, default=None,
        help="Comma-separated list of blocked app names",
    )
    parser.add_argument(
        "--security-read-only", action="store_true", default=False,
        help="Block all mutation actions (read-only mode)",
    )
    parser.add_argument(
        "--optimize", action="store_true", default=False,
        help="Enable optimization (action merging, caching, timing)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # Import and configure security/optimization on the mcp module
    from . import cua_mcp_server as srv

    if args.security:
        from .security import SecurityContext
        srv._security_context = SecurityContext(
            block_dangerous=not args.no_block_dangerous,
        )
        if args.security_allow_apps:
            srv._security_context.allowed_apps = args.security_allow_apps.split(",")
        if args.security_block_apps:
            srv._security_context.blocked_apps = args.security_block_apps.split(",")
        if args.security_read_only:
            srv._security_context.read_only = True
        logger.info("Security enabled")

    if args.optimize:
        from .optimization import OptimizationConfig
        srv._optimization_config = OptimizationConfig()
        srv._optimization_merger = srv._optimization_config.create_merger()
        srv._optimization_cache = srv._optimization_config.create_cache()
        srv._optimization_timer = srv._optimization_config.create_timer()
        srv._optimization_tokenizer = srv._optimization_config.create_tokenizer()
        logger.info("Optimization enabled")

    print(f"🚀 CUA Driver MCP server starting on {args.host}:{args.port} (SSE)...")
    sys.stdout.flush()

    # Run FastMCP with SSE transport
    srv.mcp.run(transport="sse", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
