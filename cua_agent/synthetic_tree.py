"""
synthetic_tree.py — Builds a synthetic accessibility tree from Win32 HWND
hierarchy + OCR text, merging both sources into a unified element list.

Architecture:
  1. HwndTreeWalker — Walks the Win32 window hierarchy (EnumWindows →
     EnumChildWindows) and produces a tree in UIA-compatible format.
  2. OcrEngine — Captures screen regions via PIL and runs Tesseract OCR
     to extract text from custom/non-AX controls.
  3. SyntheticTreeBuilder — Merges UIA tree + HWND tree + OCR text into
     a single unified tree with per-node source annotations.

Usage:
    from cua_agent.synthetic_tree import HwndTreeWalker, OcrEngine, SyntheticTreeBuilder

    hwnd_walker = HwndTreeWalker()
    ocr = OcrEngine()
    builder = SyntheticTreeBuilder(hwnd_walker, ocr)

    # From a UIA tree, produce the merged tree
    raw = collector.get_foreground_app_state()
    merged = builder.build_synthetic_tree(raw)
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from ctypes import wintypes
from typing import Any

logger = logging.getLogger("synthetic-tree")

# ── Constants ──

# Window class name patterns → AX role mapping
CLASS_ROLE_MAP: dict[str, str] = {
    "Button": "AXButton",
    "Edit": "AXTextField",
    "Static": "AXStaticText",
    "ComboBox": "AXComboBox",
    "ListBox": "AXList",
    "ScrollBar": "AXScrollBar",
    "Scrollbar": "AXScrollBar",
    "msctls_statusbar32": "AXStatusBar",
    "msctls_progress32": "AXProgressBar",
    "msctls_trackbar32": "AXSlider",
    "msctls_updown32": "AXStepper",
    "SysTabControl32": "AXTab",
    "SysTreeView32": "AXTree",
    "SysListView32": "AXList",
    "SysHeader32": "AXHeader",
    "ToolbarWindow32": "AXToolBar",
    "ToolTips_class32": "AXToolTip",
    "RICHEDIT": "AXTextField",
    "RichEdit20W": "AXTextField",
    "RichEdit50W": "AXTextField",
    "Internet Explorer_Server": "AXWebView",
    "Chrome_WidgetWin_1": "AXWindow",
    "Chrome_RenderWidgetHostHWND": "AXWebView",
    "MozillaWindowClass": "AXWindow",
    "MozillaContentWindowClass": "AXWebView",
    "Windows.UI.Core.CoreWindow": "AXWindow",
    "ApplicationFrameWindow": "AXWindow",
    "WindowsForms10.Window": "AXWindow",
    "WindowsForms10.Button": "AXButton",
    "WindowsForms10.Edit": "AXTextField",
    "WindowsForms10.Static": "AXStaticText",
    "WindowsForms10.ComboBox": "AXComboBox",
    "Shell_TrayWnd": "AXDock",
    "TrayNotifyWnd": "AXGroup",
    "Shell_SecondaryTrayWnd": "AXDock",
    "Progman": "AXDesktop",
    "WorkerW": "AXPane",
    "#32770": "AXDialog",        # Dialog
    "#32768": "AXMenu",          # Menu
    "#32771": "AXMenuBar",       # Menu bar
}

# Roles that are NOT interactive — skip OCR for these
_NON_INTERACTIVE_ROLES: frozenset = frozenset({
    "AXPane", "AXGroup", "AXWindow", "AXTitleBar", "AXMenuBar",
    "AXToolBar", "AXStatusBar", "AXScrollBar", "AXProgressBar",
    "AXSeparator", "AXHeader", "AXAppBar", "AXToolTip",
    "AXDock", "AXDesktop",
})

# Max depth for HWND tree walk
_MAX_HWND_DEPTH = 10
# Max children per node in HWND tree
_MAX_HWND_CHILDREN = 200
# Max total nodes in HWND tree
_MAX_HWND_NODES = 1000


# ── HwndTreeWalker ──

class HwndTreeWalker:
    """Walks the Win32 HWND hierarchy and produces a tree in UIA-compatible format.

    This is a fallback for when UIA returns no/few elements — e.g. game engines,
    Electron/web views, legacy MFC/Win32, or custom controls that don't expose
    UI Automation properties.

    Even if a window is invisible to UIA, most still have an HWND with a title,
    class name, and bounding rectangle accessible via Win32 API.
    """

    def __init__(
        self,
        max_depth: int = _MAX_HWND_DEPTH,
        max_children: int = _MAX_HWND_CHILDREN,
        max_nodes: int = _MAX_HWND_NODES,
    ):
        self._max_depth = max_depth
        self._max_children = max_children
        self._max_nodes = max_nodes
        self._node_count = 0

    def get_foreground_tree(self) -> dict:
        """Build a synthetic tree from the foreground window's HWND hierarchy.

        Returns:
            A tree dict in UIA-compatible format with nodes containing:
                role, title, pos, size, hwnd, class_name, source="hwnd"
            Returns an error dict if no foreground window is found.
        """
        self._node_count = 0
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                return self._error_result("No foreground window found")

            return self._build_hwnd_node(hwnd, depth=0)
        except Exception as e:
            return self._error_result(f"HWND walk failed: {e}")

    def get_tree_for_window(self, hwnd: int) -> dict:
        """Build a synthetic tree from a specific window HWND.

        Args:
            hwnd: Window handle to start from.

        Returns:
            Tree dict in UIA-compatible format.
        """
        self._node_count = 0
        return self._build_hwnd_node(hwnd, depth=0)

    def _build_hwnd_node(self, hwnd: int, depth: int) -> dict:
        """Recursively build a tree node from an HWND.

        Produces nodes matching the UIA tree format so the PromptBuilder
        can process them seamlessly.
        """
        if depth > self._max_depth:
            return self._leaf_node(hwnd, depth)
        if self._node_count > self._max_nodes:
            return self._leaf_node(hwnd, depth)

        node = self._hwnd_to_node(hwnd)
        self._node_count += 1

        # Skip invisible / zero-size windows
        if not self._is_visible(hwnd):
            return node  # Return as leaf (no children)

        rect = node.get("_rect")
        if rect and (rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0):
            return node

        # Build children via EnumChildWindows
        children = self._get_child_hwnds(hwnd)
        kept = []
        for i, child_hwnd in enumerate(children):
            if len(kept) >= self._max_children:
                node["_truncated"] = f"children limited to {self._max_children}"
                break
            if self._node_count > self._max_nodes:
                node["_truncated"] = f"max nodes ({self._max_nodes}) reached"
                break

            child_node = self._build_hwnd_node(child_hwnd, depth + 1)
            if child_node is not None:
                kept.append(child_node)

        if kept:
            node["children"] = kept

        return node

    def _hwnd_to_node(self, hwnd: int) -> dict:
        """Convert an HWND to a UIA-compatible node dict."""
        node: dict = {
            "hwnd": hwnd,
            "source": "hwnd",
            "role": "AXPane",  # Default role
        }

        # Title
        try:
            buf = ctypes.create_unicode_buffer(512)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
            title = (buf.value or "").strip()
            if title:
                node["title"] = title
        except Exception:
            pass

        # Class name → role mapping
        try:
            cls_buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetClassNameW(hwnd, cls_buf, 256)
            class_name = (cls_buf.value or "")
            node["class_name"] = class_name
            # Map to AX role
            for pattern, role in CLASS_ROLE_MAP.items():
                if pattern in class_name:
                    node["role"] = role
                    break
        except Exception:
            pass

        # Bounding rectangle → pos + size
        try:
            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w > 0 and h > 0:
                node["pos"] = {
                    "x": round((rect.left + rect.right) / 2),
                    "y": round((rect.top + rect.bottom) / 2),
                }
                node["size"] = {"w": w, "h": h}
                node["_rect"] = (rect.left, rect.top, rect.right, rect.bottom)
        except Exception:
            pass

        # Visibility
        try:
            node["_visible"] = bool(ctypes.windll.user32.IsWindowVisible(hwnd))
        except Exception:
            node["_visible"] = False

        # Foreground / focused
        try:
            fg = ctypes.windll.user32.GetForegroundWindow()
            node["_focused_hwnd"] = (hwnd == fg)
        except Exception:
            pass

        return node

    def _leaf_node(self, hwnd: int, depth: int) -> dict:
        """Create a minimal leaf node (for early termination)."""
        node = self._hwnd_to_node(hwnd)
        node["_leaf"] = True
        return node

    @staticmethod
    def _is_visible(hwnd: int) -> bool:
        """Check if a window handle is visible."""
        try:
            return bool(ctypes.windll.user32.IsWindowVisible(hwnd))
        except Exception:
            return False

    @staticmethod
    def _get_child_hwnds(hwnd: int) -> list[int]:
        """Get child HWNDs of a window via EnumChildWindows.

        Returns sorted list (z-order, topmost first).
        """
        children: list[int] = []

        try:
            @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            def _enum_child_proc(child_hwnd: int, _lparam: int) -> bool:
                children.append(child_hwnd)
                return True  # Continue enumeration

            ctypes.windll.user32.EnumChildWindows(hwnd, _enum_child_proc, 0)
        except Exception:
            pass

        return children

    @staticmethod
    def _error_result(msg: str) -> dict:
        return {"error": msg, "role": "", "title": "", "children": [], "source": "hwnd"}

    @staticmethod
    def list_all_windows() -> list[dict]:
        """List all top-level windows with their HWND info.

        Returns:
            A list of dicts with: hwnd, title, class_name, pos, size, pid
        """
        windows: list[dict] = []

        try:
            @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            def _enum_proc(hwnd: int, _lparam: int) -> bool:
                try:
                    if not ctypes.windll.user32.IsWindowVisible(hwnd):
                        return True

                    buf = ctypes.create_unicode_buffer(512)
                    ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
                    title = buf.value or ""

                    cls_buf = ctypes.create_unicode_buffer(256)
                    ctypes.windll.user32.GetClassNameW(hwnd, cls_buf, 256)
                    class_name = cls_buf.value or ""

                    rect = wintypes.RECT()
                    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))

                    pid = ctypes.c_ulong()
                    ctypes.windll.user32.GetWindowThreadProcessId(
                        hwnd, ctypes.byref(pid)
                    )

                    windows.append({
                        "hwnd": hwnd,
                        "title": title,
                        "class_name": class_name,
                        "pos": {
                            "x": round((rect.left + rect.right) / 2),
                            "y": round((rect.top + rect.bottom) / 2),
                        },
                        "size": {
                            "w": rect.right - rect.left,
                            "h": rect.bottom - rect.top,
                        },
                        "pid": pid.value,
                    })
                except Exception:
                    pass
                return True

            ctypes.windll.user32.EnumWindows(_enum_proc, 0)
        except Exception:
            pass

        return windows


# ── OcrEngine ──

class OcrEngine:
    """Extracts text from screen regions using Tesseract OCR.

    Used to read text from custom/non-AX controls that don't expose
    their text content through UIA or Win32 GetWindowTextW.

    Gracefully handles missing Tesseract installation — returns empty
    results without raising errors.
    """

    def __init__(self, tesseract_path: str | None = None):
        """Initialize the OCR engine.

        Args:
            tesseract_path: Optional custom path to tesseract executable.
                If None, uses system PATH or default install location.
        """
        self._available = False
        self._pytesseract = None
        self._init_tesseract(tesseract_path)

    def _init_tesseract(self, custom_path: str | None):
        """Try to initialize pytesseract with the Tesseract binary.

        Attempts several strategies:
        1. Custom path (if provided)
        2. System PATH (shutil.which)
        3. Default install location (Program Files)
        """
        import shutil
        import os

        # Determine tesseract binary path
        tesseract_cmd = None

        if custom_path and os.path.isfile(custom_path):
            tesseract_cmd = custom_path
        else:
            # Check PATH
            tesseract_cmd = shutil.which("tesseract")
            if not tesseract_cmd:
                # Check common install locations
                for candidate in [
                    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                ]:
                    if os.path.isfile(candidate):
                        tesseract_cmd = candidate
                        break

        if not tesseract_cmd:
            logger.warning(
                "Tesseract OCR binary not found. "
                "Install from https://github.com/UB-Mannheim/tesseract/wiki "
                "or ensure it's on your PATH. OCR enrichment will be skipped."
            )
            self._available = False
            return

        try:
            import pytesseract
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
            # Quick validation: check version
            version = pytesseract.get_tesseract_version()
            self._pytesseract = pytesseract
            self._available = True
            logger.info(f"Tesseract OCR initialized: {version}")
        except Exception as e:
            logger.warning(f"Failed to initialize pytesseract: {e}")
            self._available = False

    @property
    def is_available(self) -> bool:
        """Check if Tesseract OCR is available."""
        return self._available

    def extract_text(self, x: int, y: int, w: int, h: int) -> dict:
        """Extract text from a screen region.

        Args:
            x, y: Top-left corner in screen coordinates.
            w, h: Width and height of the region.

        Returns:
            dict with:
                text: str — Extracted text (empty if none found)
                confidence: float — Average confidence (0-100, 0 if unavailable)
                error: str or None
        """
        if not self._available or not self._pytesseract:
            return {"text": "", "confidence": 0.0, "error": "OCR not available"}

        if w <= 0 or h <= 0 or w > 7680 or h > 4320:
            return {"text": "", "confidence": 0.0, "error": f"Invalid region: {w}x{h}"}

        try:
            from PIL import ImageGrab

            # Grab the screen region
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h))

            # Convert to grayscale for better OCR accuracy
            if img.mode != "L":
                img = img.convert("L")

            # Run OCR
            text = self._pytesseract.image_to_string(img).strip()

            # Get confidence data
            conf = self._get_confidence(img)

            return {
                "text": text,
                "confidence": conf,
                "error": None,
            }
        except Exception as e:
            return {"text": "", "confidence": 0.0, "error": str(e)}

    def extract_text_batch(
        self, regions: list[tuple[int, int, int, int]]
    ) -> list[dict]:
        """Extract text from multiple screen regions in batch.

        Args:
            regions: List of (x, y, w, h) tuples.

        Returns:
            List of OCR result dicts (same order as input).
        """
        return [self.extract_text(x, y, w, h) for x, y, w, h in regions]

    def enrich_node(self, node: dict) -> dict:
        """Enrich a single tree node with OCR text if it has no title.

        If the node has a 'pos' and 'size', captures that region and
        runs OCR. If text is found and the node has no title, sets it.

        Returns:
            The node (possibly modified in-place) with OCR enrichment info.
        """
        # Skip if already has a title
        if node.get("title"):
            node["_ocr_skipped"] = "already has title"
            return node

        # Skip non-interactive roles
        role = node.get("role", "")
        if role in _NON_INTERACTIVE_ROLES:
            node["_ocr_skipped"] = f"non-interactive role: {role}"
            return node

        # Need position and size
        pos = node.get("pos")
        size = node.get("size")
        if not pos or not size:
            node["_ocr_skipped"] = "no position data"
            return node

        # Run OCR
        result = self.extract_text(
            pos["x"] - size["w"] // 2,
            pos["y"] - size["h"] // 2,
            size["w"],
            size["h"],
        )

        if result["text"] and result["error"] is None:
            node["title"] = result["text"][:200]  # Cap at 200 chars
            node["_ocr_sourced"] = True
            node["_ocr_confidence"] = round(result["confidence"], 1)
            node["source"] = "uia+ocr"
        else:
            node["_ocr_skipped"] = result.get("error") or "no text found"

        return node

    def enrich_tree(self, tree: dict) -> dict:
        """Recursively enrich a tree with OCR text.

        Walks all nodes and runs OCR on those lacking a title.
        Limits OCR calls to avoid excessive screen captures.
        """
        ocr_count = [0]
        max_ocr = 20  # Cap total OCR calls per enrich

        def _walk(node: dict):
            if ocr_count[0] >= max_ocr:
                return
            # Enrich this node
            if not node.get("title") and node.get("pos") and node.get("size"):
                self.enrich_node(node)
                if node.get("_ocr_sourced"):
                    ocr_count[0] += 1
            # Recurse children
            for child in node.get("children", []):
                if ocr_count[0] >= max_ocr:
                    break
                _walk(child)

        _walk(tree)
        return tree

    @staticmethod
    def _get_confidence(img) -> float:
        """Get average OCR confidence from Tesseract.

        Returns average confidence as a float (0-100), or 0 if unavailable.
        """
        try:
            import pytesseract
            # Use the 'data' output to get per-character confidences
            data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
            confs = [c for c in data.get("conf", []) if c != -1]
            if confs:
                return sum(confs) / len(confs)
        except Exception:
            pass
        return 0.0


# ── SyntheticTreeBuilder ──

class SyntheticTreeBuilder:
    """Merges UIA tree + HWND tree + OCR into a unified element list.

    Strategy:
    1. UIA tree is the primary source (finest granularity)
    2. HWND tree fills in window-level containers that UIA might have missed
    3. OCR adds text content to elements with no title
    4. Each node is tagged with a 'source' field for provenance tracking
    """

    def __init__(
        self,
        hwnd_walker: HwndTreeWalker | None = None,
        ocr_engine: OcrEngine | None = None,
    ):
        self._hwnd_walker = hwnd_walker or HwndTreeWalker()
        self._ocr_engine = ocr_engine or OcrEngine()

    def build_synthetic_tree(self, uia_tree: dict) -> dict:
        """Build the best possible synthetic tree from all available sources.

        Args:
            uia_tree: The UIA accessibility tree from WindowsCollector.

        Returns:
            A merged tree with source annotations. The tree is augmented
            with HWND and OCR data where UIA is insufficient.
        """
        # If UIA tree has an error or is empty, fall back to HWND tree
        if self._should_fallback_to_hwnd(uia_tree):
            logger.info("UIA tree empty/error — falling back to HWND synthetic tree")
            merged = self._build_hwnd_fallback_tree(uia_tree)
        else:
            # UIA tree is usable — augment it
            merged = self._augment_uia_tree(uia_tree)

        # Apply OCR enrichment to elements without titles
        if self._ocr_engine.is_available:
            merged = self._ocr_engine.enrich_tree(merged)
            merged["_ocr_enriched"] = True
        else:
            merged["_ocr_enriched"] = False

        # Add synthetic tree metadata
        sources = ["uia"]
        if merged.get("_has_hwnd_fallback"):
            sources.append("hwnd")
        if merged.get("_ocr_enriched"):
            sources.append("ocr")
        merged["_synthetic_sources"] = sources

        return merged

    def _should_fallback_to_hwnd(self, uia_tree: dict) -> bool:
        """Determine if we should fall back to HWND tree.

        Fallback when:
        - UIA tree has an 'error' key
        - UIA tree has no interactive elements
        - UIA tree has very few nodes (< 5) — likely a custom control
        """
        if uia_tree.get("error"):
            return True

        # Check if there are any interactive elements
        interactive = self._count_interactive(uia_tree)
        if interactive == 0:
            return True

        # If very few nodes and the window is known to be custom
        total = self._count_nodes(uia_tree)
        if total <= 1:  # Truly degenerate: root node only, no children
            return True

        return False

    def _build_hwnd_fallback_tree(self, uia_tree: dict) -> dict:
        """Build a tree from HWND walker as fallback.

        Preserves error info from UIA but replaces content with HWND tree.
        """
        hwnd_tree = self._hwnd_walker.get_foreground_tree()

        if hwnd_tree.get("error"):
            # Both sources failed — return original UIA error with annotation
            merged = dict(uia_tree)
            merged["_has_hwnd_fallback"] = True
            merged["_uia_error"] = uia_tree.get("error", "both sources failed")
            return merged

        # Use HWND tree as the base, preserve any metadata from UIA
        merged = dict(hwnd_tree)
        merged["_has_hwnd_fallback"] = True
        merged["_uia_error"] = uia_tree.get("error", "fallback to HWND")
        merged["_platform"] = "windows"
        merged["_node_count"] = self._count_nodes(hwnd_tree)

        return merged

    def _augment_uia_tree(self, uia_tree: dict) -> dict:
        """Augment a UIA tree with HWND data.

        Current augmentation:
        1. If the root has no children and no HWND tree, it stays as-is
        2. Mark as UIA-sourced
        """
        merged = dict(uia_tree)
        merged["_has_hwnd_fallback"] = False
        merged["_uia_node_count"] = self._count_nodes(uia_tree)

        # Check if the foreground HWND root has children that UIA missed
        # by comparing the HWND tree's top-level children vs UIA children
        try:
            hwnd_tree = self._hwnd_walker.get_foreground_tree()
            if not hwnd_tree.get("error"):
                hwnd_child_count = len(hwnd_tree.get("children", []))
                uia_child_count = len(uia_tree.get("children", []))
                if hwnd_child_count > uia_child_count + 2:  # Significant gap
                    merged["_hwnd_found_extra"] = (
                        hwnd_child_count - uia_child_count
                    )
                    merged["_has_hwnd_fallback"] = True
        except Exception:
            pass

        return merged

    @staticmethod
    def _count_interactive(node: dict) -> int:
        """Count interactive elements in a tree."""
        count = 0
        role = node.get("role", "")
        if role and role not in _NON_INTERACTIVE_ROLES and role != "AXWindow":
            count += 1
        for child in node.get("children", []):
            count += SyntheticTreeBuilder._count_interactive(child)
        return count

    @staticmethod
    def _count_nodes(node: dict) -> int:
        """Count total nodes in a tree."""
        count = 1
        for child in node.get("children", []):
            count += SyntheticTreeBuilder._count_nodes(child)
        return count


# ── Convenience ──

def is_available() -> bool:
    """Check if the synthetic tree builder can run on this platform."""
    return sys.platform == "win32"


def create_default_builder() -> SyntheticTreeBuilder:
    """Create a SyntheticTreeBuilder with default components."""
    hwnd_walker = HwndTreeWalker()
    ocr_engine = OcrEngine()
    return SyntheticTreeBuilder(hwnd_walker=hwnd_walker, ocr_engine=ocr_engine)


# ── Quick test ──

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    if not is_available():
        print("❌ Synthetic tree requires Windows.")
        sys.exit(1)

    print("=== HwndTreeWalker: foreground window ===")
    walker = HwndTreeWalker()
    tree = walker.get_foreground_tree()
    if "error" in tree:
        print(f"❌ Error: {tree['error']}")
    else:
        title = tree.get("title", "?")
        role = tree.get("role", "?")
        children = len(tree.get("children", []))
        print(f"✅ Root: {role} '{title}' ({children} children)")
        print(f"   Total nodes: {SyntheticTreeBuilder._count_nodes(tree)}")

    print("\n=== HwndTreeWalker: all windows ===")
    windows = HwndTreeWalker.list_all_windows()
    print(f"   Found {len(windows)} visible top-level windows")
    for w in windows[:5]:
        t = w.get("title", "") or "(no title)"
        print(f"   - [{w['hwnd']}] {w.get('class_name', '?')} = '{t[:40]}'")

    print("\n=== OcrEngine: availability ===")
    ocr = OcrEngine()
    print(f"   OCR available: {ocr.is_available}")

    print("\n=== SyntheticTreeBuilder: full pipeline ===")
    builder = SyntheticTreeBuilder(hwnd_walker=walker, ocr_engine=ocr)

    # Simulate an empty UIA tree (like a custom control)
    empty_uia = {"error": "No accessible elements found", "role": "", "children": []}
    merged = builder.build_synthetic_tree(empty_uia)
    print(f"   Fallback tree sources: {merged.get('_synthetic_sources', [])}")
    print(f"   Root: {merged.get('role', '?')} '{merged.get('title', '?')}'")
    print(f"   Children: {len(merged.get('children', []))}")
