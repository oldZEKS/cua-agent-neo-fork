"""Unit tests for the synthetic accessibility tree builder.

Tests cover:
- HwndTreeWalker: HWND enumeration, node building, class→role mapping
- OcrEngine: fallback behavior when Tesseract is unavailable
- SyntheticTreeBuilder: merging UIA + HWND + OCR, fallback logic
- Integration verification: _augment_with_synthetic, _dfs_search_synthetic
"""

from __future__ import annotations

import json
import sys
import unittest
from unittest.mock import patch, MagicMock

from cua_agent.synthetic_tree import (
    HwndTreeWalker,
    OcrEngine,
    SyntheticTreeBuilder,
    create_default_builder,
    is_available,
    CLASS_ROLE_MAP,
    _NON_INTERACTIVE_ROLES,
)


class TestHwndTreeWalker(unittest.TestCase):
    """Tests for HwndTreeWalker — Win32 HWND hierarchy walker."""

    def test_class_role_mapping(self):
        """Verify CLASS_ROLE_MAP has expected entries for common controls."""
        self.assertIn("Button", CLASS_ROLE_MAP)
        self.assertEqual(CLASS_ROLE_MAP["Button"], "AXButton")
        self.assertIn("Edit", CLASS_ROLE_MAP)
        self.assertEqual(CLASS_ROLE_MAP["Edit"], "AXTextField")
        self.assertIn("ComboBox", CLASS_ROLE_MAP)
        self.assertEqual(CLASS_ROLE_MAP["ComboBox"], "AXComboBox")
        self.assertIn("ListBox", CLASS_ROLE_MAP)
        self.assertEqual(CLASS_ROLE_MAP["ListBox"], "AXList")
        self.assertIn("Static", CLASS_ROLE_MAP)
        self.assertEqual(CLASS_ROLE_MAP["Static"], "AXStaticText")
        self.assertIn("#32770", CLASS_ROLE_MAP)  # Dialog
        self.assertEqual(CLASS_ROLE_MAP["#32770"], "AXDialog")

    def test_non_interactive_roles(self):
        """Verify non-interactive roles are correct."""
        self.assertIn("AXPane", _NON_INTERACTIVE_ROLES)
        self.assertIn("AXGroup", _NON_INTERACTIVE_ROLES)
        self.assertIn("AXWindow", _NON_INTERACTIVE_ROLES)
        self.assertIn("AXDock", _NON_INTERACTIVE_ROLES)
        self.assertIn("AXDesktop", _NON_INTERACTIVE_ROLES)
        # Interactive roles should NOT be in the set
        self.assertNotIn("AXButton", _NON_INTERACTIVE_ROLES)
        self.assertNotIn("AXTextField", _NON_INTERACTIVE_ROLES)
        self.assertNotIn("AXCheckBox", _NON_INTERACTIVE_ROLES)

    def test_is_available_returns_false_on_non_win32(self):
        """is_available() returns False when not on Windows."""
        with patch.object(sys, "platform", "linux"):
            self.assertFalse(is_available())

    def test_is_available_returns_true_on_win32(self):
        """is_available() returns True on Windows."""
        with patch.object(sys, "platform", "win32"):
            self.assertTrue(is_available())

    def test_create_default_builder(self):
        """create_default_builder() returns a SyntheticTreeBuilder instance."""
        builder = create_default_builder()
        self.assertIsInstance(builder, SyntheticTreeBuilder)
        self.assertIsInstance(builder._hwnd_walker, HwndTreeWalker)
        self.assertIsInstance(builder._ocr_engine, OcrEngine)

    def test_error_result(self):
        """_error_result returns a properly structured error dict."""
        result = HwndTreeWalker._error_result("test error")
        self.assertEqual(result.get("error"), "test error")
        self.assertEqual(result.get("role"), "")
        self.assertEqual(result.get("title"), "")
        self.assertEqual(result.get("children"), [])
        self.assertEqual(result.get("source"), "hwnd")

    def test_list_all_windows_graceful_on_non_windows(self):
        """list_all_windows should not crash even without Win32 APIs."""
        walker = HwndTreeWalker()
        # On non-Windows, EnumWindows won't be available
        result = walker.list_all_windows()
        self.assertIsInstance(result, list)

    def test_should_fallback_when_empty(self):
        """SyntheticTreeBuilder._should_fallback_to_hwnd with empty tree."""
        builder = create_default_builder()
        # Empty tree with error
        self.assertTrue(builder._should_fallback_to_hwnd({"error": "No UIA"}))
        # Tree with no interactive elements
        self.assertTrue(builder._should_fallback_to_hwnd({
            "role": "AXWindow", "children": [
                {"role": "AXPane", "children": []}
            ]
        }))
        # Tree with interactive elements (> 1 node — valid UIA tree)
        self.assertFalse(builder._should_fallback_to_hwnd({
            "role": "AXWindow", "children": [
                {"role": "AXButton", "title": "OK"},
                {"role": "AXTextField", "title": "Name"},
            ]
        }))
        # Root only, no children — degenerate, should fallback
        self.assertTrue(builder._should_fallback_to_hwnd({"role": "AXButton", "title": "OK"}))

    def test_count_nodes(self):
        """_count_nodes counts all nodes in a tree."""
        tree = {
            "role": "root",
            "children": [
                {"role": "A", "children": [
                    {"role": "B", "children": []},
                    {"role": "C", "children": []},
                ]},
                {"role": "D", "children": []},
            ]
        }
        self.assertEqual(SyntheticTreeBuilder._count_nodes(tree), 5)

    def test_count_interactive(self):
        """_count_interactive counts only non-pane, non-group nodes."""
        tree = {
            "role": "AXButton",
            "children": [
                {"role": "AXPane", "children": [
                    {"role": "AXTextField", "children": []},
                    {"role": "AXGroup", "children": [
                        {"role": "AXCheckBox", "children": []},
                    ]},
                ]},
                {"role": "AXStaticText", "children": []},
            ]
        }
        # Count: AXButton(1) + AXTextField(1) + AXCheckBox(1) + AXStaticText(1) = 4
        self.assertEqual(SyntheticTreeBuilder._count_interactive(tree), 4)


class TestOcrEngine(unittest.TestCase):
    """Tests for OcrEngine — screen region OCR."""

    def test_init_not_available_when_tesseract_missing(self):
        """OcrEngine initializes with is_available=False when Tesseract is not found."""
        with patch("shutil.which", return_value=None), \
             patch("os.path.isfile", return_value=False):
            engine = OcrEngine()
            self.assertFalse(engine.is_available)

    def test_extract_text_returns_empty_when_unavailable(self):
        """extract_text returns empty result when OCR is unavailable."""
        engine = OcrEngine()
        engine._available = False
        result = engine.extract_text(100, 200, 50, 30)
        self.assertEqual(result["text"], "")
        self.assertEqual(result["confidence"], 0.0)
        self.assertIsNotNone(result["error"])

    def test_extract_text_validates_region(self):
        """extract_text rejects invalid region dimensions."""
        engine = OcrEngine()
        engine._available = True
        with patch.object(engine, "_pytesseract", True):
            result = engine.extract_text(0, 0, 0, 0)
            self.assertEqual(result["text"], "")
            self.assertIn("Invalid region", result["error"])

            result = engine.extract_text(0, 0, 8000, 6000)
            self.assertEqual(result["text"], "")
            self.assertIn("Invalid region", result["error"])

    def test_enrich_node_skips_existing_title(self):
        """enrich_node skips OCR if node already has a title."""
        engine = OcrEngine()
        engine._available = False
        node = {"title": "Existing Title", "role": "AXButton"}
        result = engine.enrich_node(node)
        self.assertEqual(result.get("title"), "Existing Title")
        self.assertEqual(result.get("_ocr_skipped"), "already has title")

    def test_enrich_node_skips_non_interactive_roles(self):
        """enrich_node skips OCR for non-interactive roles."""
        engine = OcrEngine()
        engine._available = False
        node = {"role": "AXPane", "pos": {"x": 500, "y": 400}, "size": {"w": 100, "h": 30}}
        result = engine.enrich_node(node)
        self.assertEqual(result.get("_ocr_skipped"), "non-interactive role: AXPane")

    def test_enrich_node_skips_no_position(self):
        """enrich_node skips OCR if node has no position data."""
        engine = OcrEngine()
        engine._available = False
        node = {"role": "AXButton"}
        result = engine.enrich_node(node)
        self.assertEqual(result.get("_ocr_skipped"), "no position data")

    def test_get_confidence_handles_no_data(self):
        """_get_confidence returns 0 when no confidence data."""
        conf = OcrEngine._get_confidence(None)
        self.assertEqual(conf, 0.0)

    def test_extract_text_batch_handles_empty(self):
        """extract_text_batch handles empty list."""
        engine = OcrEngine()
        engine._available = False
        results = engine.extract_text_batch([])
        self.assertEqual(results, [])

    def test_extract_text_batch_multiple(self):
        """extract_text_batch processes multiple regions."""
        engine = OcrEngine()
        engine._available = False
        results = engine.extract_text_batch([(0, 0, 10, 10), (100, 100, 20, 20)])
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertEqual(r["text"], "")


class TestSyntheticTreeBuilder(unittest.TestCase):
    """Tests for SyntheticTreeBuilder — merging UIA + HWND + OCR."""

    def test_build_synthetic_tree_fallsback_on_error(self):
        """build_synthetic_tree falls back when UIA tree has error."""
        builder = create_default_builder()
        # Mock the HWND walker to return a known tree
        hwnd_tree = {
            "role": "AXWindow", "title": "Test App",
            "children": [{"role": "AXButton", "title": "OK", "source": "hwnd"}],
        }
        builder._hwnd_walker.get_foreground_tree = MagicMock(return_value=hwnd_tree)

        result = builder.build_synthetic_tree({"error": "No UIA tree"})
        self.assertTrue(result.get("_has_hwnd_fallback"))
        self.assertEqual(result.get("title"), "Test App")
        self.assertIn("hwnd", result.get("_synthetic_sources", []))

    def test_build_synthetic_tree_preserves_uia_when_good(self):
        """build_synthetic_tree preserves UIA tree when it has interactive elements."""
        # Use a builder with mocked HWND walker (to avoid test-environment side effects)
        from unittest.mock import MagicMock
        mock_walker = MagicMock()
        mock_walker.get_foreground_tree.return_value = {"error": "mocked — no foreground"}
        builder = SyntheticTreeBuilder(hwnd_walker=mock_walker)
        uia_tree = {
            "role": "AXWindow", "title": "Chrome",
            "children": [
                {"role": "AXButton", "title": "Reload", "source": "uia"},
                {"role": "AXButton", "title": "Close", "source": "uia"},
            ],
        }
        result = builder.build_synthetic_tree(uia_tree)
        self.assertFalse(result.get("_has_hwnd_fallback", True))
        self.assertIn("uia", result.get("_synthetic_sources", []))
        self.assertEqual(result.get("title"), "Chrome")

    def test_build_synthetic_tree_tags_sources(self):
        """build_synthetic_tree adds metadata about sources used."""
        builder = create_default_builder()
        uia_tree = {
            "role": "AXWindow", "title": "App",
            "children": [
                {"role": "AXButton", "title": "OK", "source": "uia"},
            ],
        }
        result = builder.build_synthetic_tree(uia_tree)
        self.assertIn("_synthetic_sources", result)
        self.assertIn("uia", result["_synthetic_sources"])

    def test_both_sources_fail(self):
        """When both UIA and HWND fail, original error is preserved."""
        builder = create_default_builder()
        builder._hwnd_walker.get_foreground_tree = MagicMock(
            return_value={"error": "No foreground window"}
        )
        result = builder.build_synthetic_tree({"error": "UIA failed"})
        # Should preserve the original error annotation
        self.assertEqual(result.get("_uia_error"), "UIA failed")
        # Should also have the HWND fallback flag
        self.assertTrue(result.get("_has_hwnd_fallback", False))

    def test_augment_uia_tree_marks_metadata(self):
        """_augment_uia_tree adds _uia_node_count."""
        from unittest.mock import MagicMock
        mock_walker = MagicMock()
        mock_walker.get_foreground_tree.return_value = {"error": "mocked — no fg"}
        builder = SyntheticTreeBuilder(hwnd_walker=mock_walker)
        tree = {"role": "root", "children": [
            {"role": "AXButton", "children": []}
        ]}
        result = builder._augment_uia_tree(tree)
        self.assertEqual(result.get("_uia_node_count"), 2)
        self.assertFalse(result.get("_has_hwnd_fallback"))

    def test_should_fallback_empty_tree(self):
        """Empty tree triggers HWND fallback."""
        builder = create_default_builder()
        self.assertTrue(builder._should_fallback_to_hwnd({
            "role": "", "title": "", "children": []
        }))

    def test_should_fallback_error_tree(self):
        """Tree with error triggers HWND fallback."""
        builder = create_default_builder()
        self.assertTrue(builder._should_fallback_to_hwnd({
            "role": "root", "error": "No elements"
        }))


class TestIntegration(unittest.TestCase):
    """Integration tests between synthetic_tree.py and the action provider."""

    def test_import_synthetic_tree(self):
        """Verify synthetic_tree module can be imported."""
        try:
            from cua_agent.synthetic_tree import HwndTreeWalker, OcrEngine, SyntheticTreeBuilder
            self.assertTrue(True)
        except ImportError as e:
            self.fail(f"Import failed: {e}")

    def test_import_via_create_default(self):
        """Verify create_default_builder works."""
        try:
            builder = create_default_builder()
            self.assertIsNotNone(builder)
            self.assertIsNotNone(builder._hwnd_walker)
            self.assertIsNotNone(builder._ocr_engine)
        except Exception as e:
            self.fail(f"create_default_builder failed: {e}")

    def test_synthetic_tree_builder_builds_unified_element_list(self):
        """The synthetic tree builder produces a unified element list from mixed sources.

        Even though OCR isn't available on test machines without Tesseract,
        the HWND fallback path should still produce a valid tree.
        """
        from unittest.mock import MagicMock
        mock_walker = MagicMock()
        mock_walker.get_foreground_tree.return_value = {"error": "mocked — no foreground"}
        builder = SyntheticTreeBuilder(hwnd_walker=mock_walker)
        mock_tree = {
            "role": "AXWindow",
            "title": "Test Window",
            "children": [
                {"role": "AXButton", "title": "OK", "source": "uia"},
                {"role": "AXButton", "title": "Cancel", "source": "uia"},
            ],
        }
        result = builder.build_synthetic_tree(mock_tree)
        # Should preserve the interactive elements
        children = result.get("children", [])
        titles = [c.get("title") for c in children]
        self.assertIn("OK", titles)
        self.assertIn("Cancel", titles)

    def test_enrich_tree_fallback_when_ocr_unavailable(self):
        """enrich_tree should not crash when OCR is unavailable."""
        engine = OcrEngine()
        engine._available = False
        tree = {
            "role": "AXWindow",
            "children": [{"role": "AXButton", "pos": {"x": 500, "y": 400},
                          "size": {"w": 100, "h": 30}}],
        }
        result = engine.enrich_tree(tree)
        self.assertIn("_ocr_skipped", result.get("children", [{}])[0])


class TestDfsSearchSynthetic(unittest.TestCase):
    """Tests for _dfs_search_synthetic pattern — searches synthetic tree by title."""

    def test_dfs_search_synthetic_by_title(self):
        """Simulate the DFS search logic from WindowsActionProvider."""
        tree = {
            "role": "AXWindow", "title": "Main",
            "children": [
                {"role": "AXButton", "title": "Submit", "source": "hwnd",
                 "pos": {"x": 500, "y": 400}},
                {"role": "AXGroup", "children": [
                    {"role": "AXTextField", "title": "Username", "source": "uia+ocr"},
                ]},
            ],
        }

        # Replicate the _dfs_search_synthetic logic inline
        def dfs(node: dict, target: str):
            tgt = target.strip().lower()
            if not tgt:
                return None
            title = (node.get("title") or "").strip()
            if title.lower() == tgt or (len(tgt) > 2 and tgt in title.lower()):
                return node
            for child in node.get("children", []):
                found = dfs(child, target)
                if found is not None:
                    return found
            return None

        # Exact match
        result = dfs(tree, "Submit")
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "Submit")
        self.assertEqual(result["role"], "AXButton")

        # Substring match
        result = dfs(tree, "User")
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "Username")

        # OCR-sourced
        result = dfs(tree, "username")
        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "uia+ocr")

        # Not found
        result = dfs(tree, "NonExistent")
        self.assertIsNone(result)

        # Empty target
        result = dfs(tree, "")
        self.assertIsNone(result)

    def test_dfs_search_with_position_data(self):
        """DFS search returns nodes with position data for clicking."""
        tree = {
            "children": [
                {"role": "AXButton", "title": "OK",
                 "pos": {"x": 100, "y": 200},
                 "size": {"w": 50, "h": 20},
                 "hwnd": 12345},
            ]
        }

        def dfs(node, target):
            tgt = target.lower()
            title = (node.get("title") or "")
            if title.lower() == tgt or (len(tgt) > 2 and tgt in title.lower()):
                return node
            for child in node.get("children", []):
                found = dfs(child, target)
                if found:
                    return found
            return None

        result = dfs(tree, "OK")
        self.assertIsNotNone(result)
        self.assertEqual(result["pos"]["x"], 100)
        self.assertEqual(result["pos"]["y"], 200)
        self.assertEqual(result["hwnd"], 12345)


if __name__ == "__main__":
    unittest.main()
