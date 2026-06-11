"""
synthetic_tree_debug.py — CLI debug utility for inspecting synthetic trees.

Usage:
    python -m cua_agent.synthetic_tree_debug            # Quick summary
    python -m cua_agent.synthetic_tree_debug --windows   # List all visible windows
    python -m cua_agent.synthetic_tree_debug --tree      # Foreground HWND tree
    python -m cua_agent.synthetic_tree_debug --tree --depth 3
    python -m cua_agent.synthetic_tree_debug --uia       # UIA tree from collector
    python -m cua_agent.synthetic_tree_debug --ocr       # OCR engine status
    python -m cua_agent.synthetic_tree_debug --ocr --x 500 --y 400 --w 100 --h 30
    python -m cua_agent.synthetic_tree_debug --pipeline  # Full UIA+HWND+OCR merge
    python -m cua_agent.synthetic_tree_debug --search "OK"  # DFS search by title
    python -m cua_agent.synthetic_tree_debug --compare   # Side-by-side UIA vs synthetic
    python -m cua_agent.synthetic_tree_debug --dump-json # Export as JSON
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Any

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s [%(name)s] %(message)s",
)

from cua_agent.synthetic_tree import (
    HwndTreeWalker,
    OcrEngine,
    SyntheticTreeBuilder,
    create_default_builder,
    is_available,
)


# ── ANSI helpers ──

class _Style:
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    RESET = "\033[0m"


def _ok(s: str) -> str:
    return f"{_Style.GREEN}{s}{_Style.RESET}"

def _warn(s: str) -> str:
    return f"{_Style.YELLOW}{s}{_Style.RESET}"

def _err(s: str) -> str:
    return f"{_Style.RED}{s}{_Style.RESET}"

def _dim(s: str) -> str:
    return f"{_Style.DIM}{s}{_Style.RESET}"

def _bold(s: str) -> str:
    return f"{_Style.BOLD}{s}{_Style.RESET}"

def _cyan(s: str) -> str:
    return f"{_Style.CYAN}{s}{_Style.RESET}"


# ── Tree rendering ──

def render_tree(node: dict, max_depth: int = 5, depth: int = 0) -> str:
    """Render a tree node as an indented string."""
    if depth > max_depth:
        return "  " * depth + _dim("...\n")

    indent = "  " * depth
    role = node.get("role", "?")
    title = node.get("title", "")
    source = node.get("source", "uia")
    hwnd = node.get("hwnd")
    pos = node.get("pos")
    size = node.get("size")

    parts = [indent]

    # Source indicator
    if source == "hwnd":
        parts.append(_cyan(f"[{source}]"))
    elif "ocr" in str(source):
        parts.append(_warn(f"[{source}]"))
    else:
        parts.append(_dim(f"[{source}]"))

    parts.append(f" {_bold(role)}")

    if title:
        # Truncate long titles
        t = title[:60] + "..." if len(title) > 60 else title
        parts.append(f" '{t}'")

    if hwnd:
        parts.append(_dim(f" ({hwnd})"))

    if pos:
        parts.append(f" @({pos.get('x', '?')},{pos.get('y', '?')})")
        if size:
            parts.append(f" {size.get('w', '?')}x{size.get('h', '?')}")

    # Extra flags
    extras = []
    if node.get("_ocr_sourced"):
        conf = node.get("_ocr_confidence", 0)
        extras.append(f"ocr={conf:.0f}%")
    if node.get("_ocr_skipped"):
        extras.append(f"skip:{node['_ocr_skipped'][:20]}")
    if node.get("_leaf"):
        extras.append("leaf")
    if node.get("_focused_hwnd"):
        extras.append("focused")
    if extras:
        parts.append(_dim(f" [{', '.join(extras)}]"))

    parts.append("\n")

    # Render children
    for child in node.get("children", []):
        parts.append(render_tree(child, max_depth, depth + 1))

    return "".join(parts)


def render_summary(node: dict, label: str = "") -> str:
    """Render a one-line summary of a tree."""
    role = node.get("role", "?")
    title = node.get("title", "") or "(no title)"
    children = len(node.get("children", []))
    count = _count_nodes(node)
    source = node.get("_synthetic_sources", [node.get("source", "?")])
    error = node.get("error")

    prefix = f"{label}: " if label else ""
    if error:
        return f"  {prefix}{_err(error)}"

    return (
        f"  {prefix}{_bold(role)} '{title[:50]}' "
        f"| {_ok(f'{count} nodes')} | {len(node.get('children', []))} children "
        f"| sources: {_dim(str(source))}"
    )


def _count_nodes(node: dict) -> int:
    """Count total nodes in a tree."""
    count = 1
    for child in node.get("children", []):
        count += _count_nodes(child)
    return count


def _print_header(title: str):
    """Print a section header."""
    print()
    print(_bold(f"═══ {title} ═══"))
    print()


# ── Commands ──

def cmd_platform_check():
    """Check platform compatibility."""
    _print_header("Platform Check")
    if is_available():
        print(f"  {_ok('✓')} Platform: Windows (supported)")
    else:
        print(f"  {_err('✗')} Platform: {sys.platform} (not supported — requires Windows)")
        sys.exit(1)


def cmd_windows(args: argparse.Namespace):
    """List all visible top-level windows."""
    _print_header("Visible Top-Level Windows")
    windows = HwndTreeWalker.list_all_windows()
    if not windows:
        print(f"  {_warn('No visible windows found.')}")
        return

    print(f"  Found {_ok(str(len(windows)))} visible windows:\n")
    for i, w in enumerate(windows):
        hwnd = w["hwnd"]
        title = w.get("title", "") or _dim("(no title)")
        cls = w.get("class_name", "?")
        pos = w.get("pos", {})
        sz = w.get("size", {})
        pid = w.get("pid", "?")

        print(f"  {_bold(f'#{i+1}')} [{hwnd}] {_cyan(cls)} = '{title[:50]}'")
        print(f"       pos=({pos.get('x','?')},{pos.get('y','?')}) "
              f"size={sz.get('w','?')}x{sz.get('h','?')} pid={pid}")

        if args.detail and i < args.detail:
            # Show child HWNDs via EnumChildWindows
            children = _get_child_hwnds_static(hwnd)
            if children:
                print(f"       children: {len(children)} windows")
                for ch in children[:10]:
                    ch_title = _get_window_title(ch)
                    ch_cls = _get_window_class(ch)
                    print(f"         [{ch}] {_dim(ch_cls)} = '{ch_title[:30] if ch_title else '(no title)'}'")
                if len(children) > 10:
                    print(f"         {_dim(f'... and {len(children) - 10} more')}")

        print()


def _get_child_hwnds_static(hwnd: int) -> list[int]:
    """Get child HWNDs via EnumChildWindows (duplicate for debug CLI)."""
    import ctypes
    from ctypes import wintypes
    children: list[int] = []
    try:
        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _proc(ch: int, _lp: int) -> bool:
            children.append(ch)
            return True
        ctypes.windll.user32.EnumChildWindows(hwnd, _proc, 0)
    except Exception:
        pass
    return children


def _get_window_title(hwnd: int) -> str:
    """Get window title text."""
    import ctypes
    try:
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
        return (buf.value or "").strip()
    except Exception:
        return ""


def _get_window_class(hwnd: int) -> str:
    """Get window class name."""
    import ctypes
    try:
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
        return buf.value or ""
    except Exception:
        return ""


def cmd_tree(args: argparse.Namespace):
    """Render the foreground HWND tree."""
    _print_header("Foreground HWND Tree")

    walker = HwndTreeWalker(
        max_depth=args.depth or 10,
        max_children=args.max_children or 200,
        max_nodes=args.max_nodes or 1000,
    )
    tree = walker.get_foreground_tree()

    if tree.get("error"):
        print(f"  {_err(tree['error'])}")
        return

    print(f"  Depth limit: {_bold(str(args.depth or 10))}")
    print(render_tree(tree, max_depth=args.depth or 5))
    print(render_summary(tree, "Summary"))


def cmd_uia(args: argparse.Namespace):
    """Get and render the UIA accessibility tree."""
    _print_header("UIA Accessibility Tree")

    try:
        from cua_agent.cua_mcp_server import WindowsActionProvider
        provider = WindowsActionProvider()
        obs = provider.observe()
        appstate = obs.appstate

        if appstate.get("error"):
            print(f"  {_err(appstate['error'])}")
        else:
            print(render_tree(appstate, max_depth=args.depth or 5))
            print(render_summary(appstate, "Summary"))
    except Exception as e:
        print(f"  {_err(f'Failed to get UIA tree: {e}')}")
        import traceback
        traceback.print_exc()


def cmd_ocr(args: argparse.Namespace):
    """Test OCR engine and optionally extract text from a region."""
    _print_header("OCR Engine")

    ocr = OcrEngine(tesseract_path=args.tesseract_path)

    if ocr.is_available:
        print(f"  {_ok('✓')} Tesseract OCR is available")
    else:
        print(f"  {_warn('✗')} Tesseract OCR is NOT available")
        print(f"  Install from: https://github.com/UB-Mannheim/tesseract/wiki")

    if args.x is not None and args.y is not None:
        w = args.w or 100
        h = args.h or 30
        print()
        print(f"  Extracting text from region: "
              f"({args.x}, {args.y}) {w}x{h}")

        result = ocr.extract_text(args.x, args.y, w, h)
        print(f"  Text:       {_bold(result.get('text', '') or _dim('(empty)'))}")
        print(f"  Confidence: {result.get('confidence', 0):.1f}%")
        if result.get("error"):
            print(f"  Error:      {_err(result['error'])}")

    # Also show which windows are at the given coordinates
    if args.x is not None and args.y is not None:
        print()
        print(f"  Windows containing ({args.x}, {args.y}):")
        try:
            import ctypes
            from ctypes import wintypes
            point = wintypes.POINT()
            point.x = args.x
            point.y = args.y
            # Use WindowFromPoint to find the window at that position
            hwnd = ctypes.windll.user32.WindowFromPoint(point)
            if hwnd:
                title = _get_window_title(hwnd)
                cls = _get_window_class(hwnd)
                print(f"    [{hwnd}] {_cyan(cls)} = '{title[:50] if title else _dim('(no title)')}'")
                # Walk up to parent
                parent = ctypes.windll.user32.GetParent(hwnd)
                if parent:
                    parent_title = _get_window_title(parent)
                    parent_cls = _get_window_class(parent)
                    print(f"    └─ parent: [{parent}] {_dim(parent_cls)} = "
                          f"'{parent_title[:50] if parent_title else _dim('(no title)')}'")
            else:
                print(f"    {_warn('No window found at this position')}")
        except Exception as e:
            print(f"    {_err(str(e))}")


def cmd_pipeline(args: argparse.Namespace):
    """Run the full UIA → synthetic tree pipeline."""
    _print_header("Full Pipeline: UIA → Synthetic Tree")

    builder = create_default_builder()

    # Phase 1: Get UIA tree
    print(f"  Phase 1: Collecting UIA tree...")
    try:
        from cua_agent.cua_mcp_server import WindowsActionProvider
        provider = WindowsActionProvider()
        obs = provider.observe()
        uia_tree = obs.appstate
        print(f"    UIA tree: {render_summary(uia_tree)}")
    except Exception as e:
        print(f"    {_err(f'UIA collection failed: {e}')}")
        print(f"    Falling back to empty UIA tree")
        uia_tree = {"error": f"UIA collection failed", "role": "", "children": []}

    time.sleep(0.1)

    # Phase 2: Build synthetic tree
    print(f"  Phase 2: Building synthetic tree...")
    t0 = time.perf_counter()
    merged = builder.build_synthetic_tree(uia_tree)
    elapsed = time.perf_counter() - t0
    print(f"    Built in {elapsed*1000:.1f}ms")
    print(f"    Synthetic tree: {render_summary(merged)}")

    print()
    print(f"  Sources: {merged.get('_synthetic_sources', [])}")
    print(f"  HWND fallback: {merged.get('_has_hwnd_fallback', False)}")
    print(f"  OCR enriched: {merged.get('_ocr_enriched', False)}")
    print(f"  UIA node count: {merged.get('_uia_node_count', '?')}")
    print(f"  HWND extra children: {merged.get('_hwnd_found_extra', 0)}")
    print(f"  UIA error: {merged.get('_uia_error', 'none')}")

    if args.render:
        print()
        print(f"  {_bold('Tree:')}")
        print(render_tree(merged, max_depth=args.depth or 5))

    # Phase 3: DFS search
    if args.search:
        print()
        print(f"  Phase 3: DFS search for '{args.search}'...")
        from cua_agent.cua_mcp_server import WindowsActionProvider
        import types
        from types import SimpleNamespace

        searcher = SimpleNamespace()
        searcher._dfs_search_synthetic = types.MethodType(
            WindowsActionProvider._dfs_search_synthetic, searcher
        )

        found = searcher._dfs_search_synthetic(merged, args.search)
        if found:
            print(f"    {_ok('✓ Found:')}")
            print(f"      role={found.get('role','?')} title='{found.get('title','?')}'")
            print(f"      source={found.get('source','?')} hwnd={found.get('hwnd','?')}")
            print(f"      pos={found.get('pos','?')} size={found.get('size','?')}")
            if found.get('_ocr_confidence'):
                print(f"      ocr_confidence={found['_ocr_confidence']}")
        else:
            print(f"    {_warn('✗ Not found')}")


def cmd_search(args: argparse.Namespace):
    """DFS search the synthetic tree for a title."""
    if not args.search:
        print(f"  {_err('No search term provided. Use --search <term>')}")
        return

    _print_header(f"DFS Search: '{args.search}'")

    builder = create_default_builder()

    # Get UIA tree
    try:
        from cua_agent.cua_mcp_server import WindowsActionProvider
        provider = WindowsActionProvider()
        obs = provider.observe()
        uia_tree = obs.appstate
    except Exception:
        uia_tree = {"error": "UIA failed", "role": "", "children": []}

    merged = builder.build_synthetic_tree(uia_tree)

    from cua_agent.cua_mcp_server import WindowsActionProvider
    import types
    from types import SimpleNamespace

    searcher = SimpleNamespace()
    searcher._dfs_search_synthetic = types.MethodType(
        WindowsActionProvider._dfs_search_synthetic, searcher
    )

    found = searcher._dfs_search_synthetic(merged, args.search)
    if found:
        print(f"  {_ok('✓ Found:')}")
        print(f"    role:    {_bold(found.get('role', '?'))}")
        print(f"    title:   '{found.get('title', '')}'")
        print(f"    source:  {found.get('source', '?')}")
        print(f"    hwnd:    {found.get('hwnd', '?')}")
        if found.get("pos"):
            print(f"    pos:     ({found['pos'].get('x', '?')}, {found['pos'].get('y', '?')})")
        if found.get("size"):
            print(f"    size:    {found['size'].get('w', '?')}x{found['size'].get('h', '?')}")
        if found.get("_ocr_confidence"):
            print(f"    ocr:     {found['_ocr_confidence']:.1f}% confidence")
    else:
        print(f"  {_warn('✗ Not found in synthetic tree')}")


def cmd_compare(args: argparse.Namespace):
    """Side-by-side comparison of UIA vs synthetic tree."""
    _print_header("Comparison: UIA Tree vs Synthetic Tree")

    builder = create_default_builder()

    # Get UIA tree
    try:
        from cua_agent.cua_mcp_server import WindowsActionProvider
        provider = WindowsActionProvider()
        obs = provider.observe()
        uia_tree = obs.appstate
    except Exception as e:
        print(f"  {_err(f'Could not collect UIA tree: {e}')}")
        uia_tree = {"error": "UIA failed", "role": "", "children": []}

    # Get synthetic tree
    merged = builder.build_synthetic_tree(uia_tree)

    # Metrics comparison
    uia_nodes = _count_nodes(uia_tree) if not uia_tree.get("error") else 0
    uia_interactive = SyntheticTreeBuilder._count_interactive(uia_tree) if not uia_tree.get("error") else 0
    syn_nodes = _count_nodes(merged)
    syn_interactive = SyntheticTreeBuilder._count_interactive(merged)

    print(f"  {'Metric':<25} {'UIA':<15} {'Synthetic':<15} {'Δ':<10}")
    print(f"  {'─'*25} {'─'*15} {'─'*15} {'─'*10}")
    print(f"  {'Total nodes':<25} {str(uia_nodes):<15} {str(syn_nodes):<15} "
          f"{_ok('+' + str(syn_nodes - uia_nodes)) if syn_nodes > uia_nodes else _dim(str(syn_nodes - uia_nodes))}")
    print(f"  {'Interactive nodes':<25} {str(uia_interactive):<15} {str(syn_interactive):<15}")
    print(f"  {'Sources':<25} {'uia':<15} {str(merged.get('_synthetic_sources', [])):<15}")
    print(f"  {'OCR enriched':<25} {'N/A':<15} {str(merged.get('_ocr_enriched', False)):<15}")
    print(f"  {'HWND fallback':<25} {'N/A':<15} {str(merged.get('_has_hwnd_fallback', False)):<15}")

    if not uia_tree.get("error") and uia_tree.get("children"):
        uia_titles = sorted(
            c.get("title", "") for c in uia_tree.get("children", []) if c.get("title")
        )
        syn_titles = sorted(
            c.get("title", "") for c in merged.get("children", []) if c.get("title")
        )
        added = set(syn_titles) - set(uia_titles)
        missing = set(uia_titles) - set(syn_titles)
        if added:
            print(f"\n  {_ok(f'New titles from synthetic: {list(added)[:10]}')}"
                  f"{' ...' if len(added) > 10 else ''}")
        if missing:
            print(f"  {_warn(f'Missing from synthetic: {list(missing)[:10]}')}"
                  f"{' ...' if len(missing) > 10 else ''}")

    print()
    print(f"  {_bold('UIA Tree:')}")
    if uia_tree.get("error"):
        print(f"    {_err(uia_tree['error'])}")
    else:
        print(render_tree(uia_tree, max_depth=args.depth or 3))

    print(f"  {_bold('Synthetic Tree:')}")
    print(render_tree(merged, max_depth=args.depth or 3))


def cmd_dump_json(args: argparse.Namespace):
    """Export the synthetic tree as JSON."""
    _print_header("JSON Export")

    builder = create_default_builder()

    # Get UIA tree
    try:
        from cua_agent.cua_mcp_server import WindowsActionProvider
        provider = WindowsActionProvider()
        obs = provider.observe()
        uia_tree = obs.appstate
    except Exception:
        uia_tree = {"error": "UIA failed", "role": "", "children": []}

    merged = builder.build_synthetic_tree(uia_tree)

    # Strip internal keys with _ prefix for cleaner output
    if args.clean:
        merged = _strip_internal(merged)

    json_str = json.dumps(merged, indent=2, default=str)
    print(json_str)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(json_str)
        print(f"\n  Written to {_ok(args.output)}")


def _strip_internal(node: dict) -> dict:
    """Remove internal keys (prefixed with _) for cleaner output."""
    return {
        k: v for k, v in node.items()
        if not k.startswith("_")
    }


def cmd_lint(args: argparse.Namespace):
    """Run all diagnostic checks on the synthetic tree module."""
    _print_header("Synthetic Tree Diagnostics")

    # 1. Platform
    print(f"  1. Platform: {'Windows' if is_available() else sys.platform} "
          f"{_ok('✓') if is_available() else _err('✗')}")

    # 2. Import check
    print(f"  2. Import check: ", end="")
    try:
        HwndTreeWalker, OcrEngine, SyntheticTreeBuilder, create_default_builder
        print(f"{_ok('✓')}")
    except ImportError as e:
        print(f"{_err(f'✗ {e}')}")

    # 3. HwndTreeWalker
    print(f"  3. HwndTreeWalker: ", end="")
    walker = HwndTreeWalker()
    tree = walker.get_foreground_tree()
    if tree.get("error"):
        error_msg = tree.get("error", "unknown")
        print(f"{_warn(f'✗ {error_msg}')}")
    else:
        count = _count_nodes(tree)
        print(f"{_ok(f'✓ {count} nodes')}")

    # 4. OcrEngine
    print(f"  4. OcrEngine: ", end="")
    ocr = OcrEngine(tesseract_path=args.tesseract_path)
    print(f"{_ok('✓ available') if ocr.is_available else _warn('✗ not available')}")

    # 5. SyntheticTreeBuilder
    print(f"  5. SyntheticTreeBuilder: ", end="")
    try:
        builder = SyntheticTreeBuilder(hwnd_walker=walker, ocr_engine=ocr)
        merged = builder.build_synthetic_tree(tree)
        sources = merged.get("_synthetic_sources", [])
        print(f"{_ok(f'✓ sources={sources}')}")
    except Exception as e:
        print(f"{_err(f'✗ {e}')}")

    # 6. Window count
    print(f"  6. Visible windows: ", end="")
    windows = HwndTreeWalker.list_all_windows()
    print(f"{_ok(str(len(windows)))} windows")

    # 7. Role mapping test
    from cua_agent.synthetic_tree import CLASS_ROLE_MAP
    print(f"  7. Class→Role mappings: {_ok(str(len(CLASS_ROLE_MAP)))} patterns loaded")

    print()
    print(f"  {_ok('All diagnostics complete.')}")


# ── Main ──

def main():
    safe_epilog = (__doc__ or "").replace("\u2192", "->")
    parser = argparse.ArgumentParser(
        description="Synthetic Tree Debug Utility",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=safe_epilog,
    )

    # Commands (mutually exclusive-ish via subcommand or flags)
    output_group = parser.add_argument_group("Commands")
    output_group.add_argument("--windows", action="store_true", help="List all visible windows")
    output_group.add_argument("--tree", action="store_true", help="Show foreground HWND tree")
    output_group.add_argument("--uia", action="store_true", help="Show UIA accessibility tree")
    output_group.add_argument("--ocr", action="store_true", help="Test OCR engine")
    output_group.add_argument("--pipeline", action="store_true", help="Run full UIA->synthetic pipeline")
    output_group.add_argument("--search", type=str, help="DFS search synthetic tree by title")
    output_group.add_argument("--compare", action="store_true", help="Side-by-side UIA vs synthetic")
    output_group.add_argument("--dump-json", action="store_true", help="Export synthetic tree as JSON")
    output_group.add_argument("--lint", action="store_true", help="Run diagnostic checks")
    output_group.add_argument("--all", action="store_true", help="Run all diagnostic commands")

    # Options
    parser.add_argument("--depth", type=int, default=None, help="Max tree depth (default: auto)")
    parser.add_argument("--max-children", type=int, default=200, help="Max children per node")
    parser.add_argument("--max-nodes", type=int, default=1000, help="Max total nodes")
    parser.add_argument("--x", type=int, default=None, help="X coordinate for OCR test")
    parser.add_argument("--y", type=int, default=None, help="Y coordinate for OCR test")
    parser.add_argument("--w", type=int, default=None, help="Width for OCR test")
    parser.add_argument("--h", type=int, default=None, help="Height for OCR test")
    parser.add_argument("--detail", type=int, default=0, help="Window detail level (0=none)")
    parser.add_argument("--output", type=str, default=None, help="Output file for --dump-json")
    parser.add_argument("--clean", action="store_true", help="Strip internal keys on JSON export")
    parser.add_argument("--render", action="store_true", help="Render tree in pipeline output")
    parser.add_argument("--tesseract-path", type=str, default=None, help="Custom tesseract path")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger("synthetic-tree").setLevel(logging.DEBUG)

    # Check platform for HWND-dependent commands
    hwnd_commands = any([
        args.windows, args.tree, args.ocr, args.pipeline,
        args.search, args.compare, args.dump_json, args.lint, args.all,
    ])
    if hwnd_commands and not is_available():
        print(f"{_err('Synthetic tree requires Windows.')}")
        sys.exit(1)

    # Determine which commands to run
    commands = []
    if args.all or not any([args.windows, args.tree, args.uia, args.ocr,
                            args.pipeline, args.search, args.compare,
                            args.dump_json, args.lint]):
        # Default: quick summary
        commands = ["lint"]
    else:
        if args.windows:
            commands.append("windows")
        if args.tree:
            commands.append("tree")
        if args.uia:
            commands.append("uia")
        if args.ocr:
            commands.append("ocr")
        if args.pipeline:
            commands.append("pipeline")
        if args.search:
            commands.append("search")
        if args.compare:
            commands.append("compare")
        if args.dump_json:
            commands.append("dump-json")
        if args.lint:
            commands.append("lint")
        if args.all:
            commands = ["lint", "windows", "tree", "uia", "ocr", "pipeline", "compare"]

    # Run each command, errors are non-fatal
    exit_code = 0
    for cmd_name in commands:
        try:
            fn = {
                "lint": cmd_lint,
                "windows": cmd_windows,
                "tree": cmd_tree,
                "uia": cmd_uia,
                "ocr": cmd_ocr,
                "pipeline": cmd_pipeline,
                "search": cmd_search,
                "compare": cmd_compare,
                "dump-json": cmd_dump_json,
            }[cmd_name]
            fn(args)
        except Exception as e:
            print(f"  {_err(f'Error in {cmd_name}: {e}')}")
            if args.verbose:
                import traceback
                traceback.print_exc()
            exit_code = 1

    if commands:
        print(f"\n  {_dim('─' * 40)}")
        print(f"  Done. Use --help for options.")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
