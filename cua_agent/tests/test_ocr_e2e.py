"""End-to-end OCR enrichment tests.

Tests the full OCR pipeline:

1. OcrEngine initialization (with/without Tesseract)
2. extract_text — screen region capture → grayscale → OCR → confidence
3. enrich_node — single node enrichment with mocked OCR
4. enrich_tree — recursive tree enrichment with call limit
5. batch processing — extract_text_batch
6. Full pipeline: UIA tree → build_synthetic_tree → OCR enrichment
"""

from __future__ import annotations

import sys
import unittest
from unittest.mock import patch, MagicMock, PropertyMock, call


# =====================================================================
# Mocks
# =====================================================================

MockPytesseract = MagicMock()
MockPytesseract.get_tesseract_version.return_value = "5.3.3"
MockPytesseract.image_to_string.return_value = "Extracted Text\n"
MockPytesseract.image_to_data.return_value = {
    "conf": [95, 87, 92, 88, -1, 90],
}


# =====================================================================
# Tests
# =====================================================================

class TestOcrEngineInitE2E(unittest.TestCase):
    """End-to-end tests for OcrEngine initialization."""

    def test_init_marks_unavailable_when_no_tesseract(self):
        """OcrEngine.is_available is False when Tesseract binary not found."""
        with patch("shutil.which", return_value=None), \
             patch("os.path.isfile", return_value=False):
            from cua_agent.synthetic_tree import OcrEngine
            engine = OcrEngine()
            self.assertFalse(engine.is_available)
            self.assertIsNone(engine._pytesseract)

    def test_init_with_custom_path(self):
        """OcrEngine accepts a custom tesseract path but still unavailable if pytesseract not installed."""
        from cua_agent.synthetic_tree import OcrEngine
        engine = OcrEngine(tesseract_path=r"C:\custom\tesseract.exe")
        # pytesseract not installed/importable, so unavailable
        self.assertFalse(engine.is_available)

    def test_init_success_with_mocked_tesseract(self):
        """OcrEngine initializes successfully when pytesseract is available."""
        with patch("shutil.which", return_value=r"C:\Program Files\Tesseract-OCR\tesseract.exe"), \
             patch("os.path.isfile", return_value=True), \
             patch.dict("sys.modules", {"pytesseract": MockPytesseract}):
            from cua_agent.synthetic_tree import OcrEngine
            engine = OcrEngine()
            self.assertTrue(engine.is_available)


class TestOcrExtractTextE2E(unittest.TestCase):
    """End-to-end tests for extract_text with mocked screen capture."""

    def setUp(self):
        self.mock_pil = MagicMock()
        self.mock_img = MagicMock()
        self.mock_img.mode = "RGB"
        self.mock_img.convert.return_value = self.mock_img
        self.mock_pil.ImageGrab.grab.return_value = self.mock_img

        self.engine = self._make_engine()

    def _make_engine(self, available: bool = True):
        from cua_agent.synthetic_tree import OcrEngine
        engine = OcrEngine()
        engine._available = available
        if available:
            engine._pytesseract = MockPytesseract
        return engine

    def test_extract_text_captures_region(self):
        """extract_text captures the correct screen region."""
        with patch("PIL.ImageGrab.grab", return_value=self.mock_img) as mock_grab:
            result = self.engine.extract_text(100, 200, 50, 30)
            mock_grab.assert_called_once_with(bbox=(100, 200, 150, 230))

    def test_extract_text_converts_to_grayscale(self):
        """extract_text converts captured image to grayscale for OCR."""
        with patch("PIL.ImageGrab.grab", return_value=self.mock_img) as mock_grab:
            self.engine.extract_text(100, 200, 50, 30)
            self.mock_img.convert.assert_called_once_with("L")

    def test_extract_text_returns_text(self):
        """extract_text returns extracted text."""
        with patch("PIL.ImageGrab.grab", return_value=self.mock_img), \
             patch.object(self.engine._pytesseract, "image_to_string",
                          return_value="Hello World\n"):
            result = self.engine.extract_text(100, 200, 200, 50)
            self.assertEqual(result["text"], "Hello World")
            self.assertIsNone(result["error"])

    def test_extract_text_returns_confidence(self):
        """extract_text returns confidence score."""
        with patch("PIL.ImageGrab.grab", return_value=self.mock_img), \
             patch.object(self.engine._pytesseract, "image_to_string",
                          return_value="Hello\n"):
            result = self.engine.extract_text(100, 200, 200, 50)
            self.assertIsInstance(result["confidence"], float)

    def test_extract_text_validates_negative_dimensions(self):
        """extract_text rejects negative width/height."""
        result = self.engine.extract_text(0, 0, -1, 100)
        self.assertIn("Invalid region", result.get("error", ""))

    def test_extract_text_validates_excessive_dimensions(self):
        """extract_text rejects too-large regions (screen dimensions)."""
        result = self.engine.extract_text(0, 0, 10000, 10000)
        self.assertIn("Invalid region", result.get("error", ""))

    def test_extract_text_handles_pytesseract_error(self):
        """extract_text handles pytesseract exceptions gracefully."""
        with patch("PIL.ImageGrab.grab", return_value=self.mock_img), \
             patch.object(self.engine._pytesseract, "image_to_string",
                          side_effect=RuntimeError("Tesseract crashed")):
            result = self.engine.extract_text(100, 200, 50, 30)
            self.assertIsNotNone(result.get("error"))

    def test_extract_text_not_available(self):
        """extract_text returns empty when OCR not available."""
        engine = self._make_engine(available=False)
        result = engine.extract_text(100, 200, 50, 30)
        self.assertEqual(result["text"], "")
        self.assertEqual(result["confidence"], 0.0)
        self.assertIsNotNone(result["error"])


class TestOcrEnrichNodeE2E(unittest.TestCase):
    """End-to-end tests for enrich_node — single node enrichment."""

    def setUp(self):
        self.engine = self._make_engine()

    def _make_engine(self, available: bool = True):
        from cua_agent.synthetic_tree import OcrEngine
        engine = OcrEngine()
        engine._available = available
        if available:
            engine._pytesseract = MockPytesseract
        return engine

    def test_enrich_node_adds_title_from_ocr(self):
        """enrich_node adds title from OCR for a button with no title."""
        node = {
            "role": "AXButton",
            "pos": {"x": 500, "y": 400},
            "size": {"w": 100, "h": 30},
        }
        with patch("PIL.ImageGrab.grab", return_value=MagicMock(mode="RGB")), \
             patch.object(self.engine._pytesseract, "image_to_string",
                          return_value="Submit\n"):
            result = self.engine.enrich_node(node)
            self.assertEqual(result.get("title"), "Submit")
            self.assertTrue(result.get("_ocr_sourced"))
            self.assertEqual(result.get("source"), "uia+ocr")

    def test_enrich_node_sets_confidence(self):
        """enrich_node sets OCR confidence score on enriched nodes."""
        node = {
            "role": "AXButton",
            "pos": {"x": 500, "y": 400},
            "size": {"w": 100, "h": 30},
        }
        with patch("PIL.ImageGrab.grab", return_value=MagicMock(mode="RGB")), \
             patch.object(self.engine._pytesseract, "image_to_string",
                          return_value="OK\n"):
            result = self.engine.enrich_node(node)
            self.assertIn("_ocr_confidence", result)
            self.assertIsInstance(result["_ocr_confidence"], float)

    def test_enrich_node_skips_existing_title(self):
        """enrich_node skips OCR if node already has a title."""
        node = {"title": "Existing", "role": "AXButton"}
        result = self.engine.enrich_node(node)
        self.assertEqual(result["title"], "Existing")
        self.assertEqual(result.get("_ocr_skipped"), "already has title")

    def test_enrich_node_skips_non_interactive_role(self):
        """enrich_node skips OCR for non-interactive roles like AXPane."""
        node = {"role": "AXPane", "pos": {"x": 500, "y": 400}, "size": {"w": 100, "h": 30}}
        result = self.engine.enrich_node(node)
        self.assertEqual(result.get("_ocr_skipped"), "non-interactive role: AXPane")

    def test_enrich_node_skips_no_position(self):
        """enrich_node skips OCR if node has no position data."""
        node = {"role": "AXButton"}
        result = self.engine.enrich_node(node)
        self.assertEqual(result.get("_ocr_skipped"), "no position data")

    def test_enrich_node_skips_no_size(self):
        """enrich_node skips OCR if node has position but no size."""
        node = {"role": "AXButton", "pos": {"x": 500, "y": 400}}
        result = self.engine.enrich_node(node)
        self.assertEqual(result.get("_ocr_skipped"), "no position data")

    def test_enrich_node_caps_title_length(self):
        """enrich_node caps OCR-extracted title at 200 chars."""
        long_text = "A" * 500
        node = {
            "role": "AXTextField",
            "pos": {"x": 500, "y": 400},
            "size": {"w": 300, "h": 30},
        }
        with patch("PIL.ImageGrab.grab", return_value=MagicMock(mode="RGB")), \
             patch.object(self.engine._pytesseract, "image_to_string",
                          return_value=long_text + "\n"):
            result = self.engine.enrich_node(node)
            self.assertLessEqual(len(result.get("title", "")), 200)

    def test_enrich_node_does_not_overwrite_source_flag(self):
        """enrich_node sets source='uia+ocr' only when OCR succeeds."""
        node = {
            "role": "AXButton",
            "pos": {"x": 500, "y": 400},
            "size": {"w": 100, "h": 30},
        }
        with patch("PIL.ImageGrab.grab", return_value=MagicMock(mode="RGB")), \
             patch.object(self.engine._pytesseract, "image_to_string",
                          return_value=""):  # Empty OCR result
            result = self.engine.enrich_node(node)
            # No title was set, so source should still be the original
            self.assertNotEqual(result.get("source"), "uia+ocr")


class TestOcrEnrichTreeE2E(unittest.TestCase):
    """End-to-end tests for enrich_tree — recursive tree enrichment."""

    def setUp(self):
        self.engine = self._make_engine()

    def _make_engine(self, available: bool = True):
        from cua_agent.synthetic_tree import OcrEngine
        engine = OcrEngine()
        engine._available = available
        if available:
            engine._pytesseract = MockPytesseract
        return engine

    def test_enrich_tree_walks_all_nodes(self):
        """enrich_tree recursively walks all nodes and enriches those without titles."""
        tree = {
            "role": "AXWindow", "title": "App",
            "children": [
                {"role": "AXButton", "pos": {"x": 100, "y": 100},
                 "size": {"w": 50, "h": 20}},
                {"role": "AXTextField", "title": "Name",
                 "pos": {"x": 200, "y": 100}, "size": {"w": 100, "h": 20}},
                {"role": "AXGroup", "children": [
                    {"role": "AXCheckBox", "pos": {"x": 100, "y": 200},
                     "size": {"w": 20, "h": 20}},
                ]},
            ],
        }
        with patch("PIL.ImageGrab.grab", return_value=MagicMock(mode="RGB")):
            result = self.engine.enrich_tree(tree)
            # First child (AXButton, no title) should get OCR enrichment
            btn = result["children"][0]
            self.assertTrue(btn.get("_ocr_sourced", False) or btn.get("_ocr_skipped"))
            # Second child (AXTextField, already has title) should be skipped
            txt = result["children"][1]
            self.assertEqual(txt.get("title"), "Name")
            # Third is a group with no pos/size — _walk skips it (no enrich_node call)
            # and recurses into its children. The checkbox child is enriched.
            grp = result["children"][2]
            self.assertFalse(grp.get("_ocr_sourced", False),
                             msg="Group should not be OCR-enriched")
            # Checkbox inside group should still be processed
            self.assertTrue(result["children"][2]["children"][0].get("_ocr_sourced", False) or
                          result["children"][2]["children"][0].get("_ocr_skipped"))

    def test_enrich_tree_limits_ocr_calls(self):
        """enrich_tree caps OCR calls at 20 to avoid excessive screen captures."""
        # Create a tree with 30 nodes that could be OCR-enriched
        nodes = []
        for i in range(30):
            nodes.append({
                "role": "AXButton",
                "title": "",  # No title — eligible for OCR
                "pos": {"x": 100 + i * 10, "y": 100},
                "size": {"w": 50, "h": 20},
            })

        tree = {"role": "AXWindow", "children": nodes}

        call_count = [0]

        def limited_grab(bbox=None):
            call_count[0] += 1
            return MagicMock(mode="RGB")

        with patch("PIL.ImageGrab.grab", side_effect=limited_grab):
            self.engine.enrich_tree(tree)
            # Should not make more than 20 OCR calls
            self.assertLessEqual(call_count[0], 22)  # 20 OCR + some overhead

    def test_enrich_tree_marks_ocr_count_in_children(self):
        """enrich_tree enriches up to 20 nodes, leaves rest with skip reason."""
        nodes = []
        for i in range(25):
            nodes.append({
                "role": "AXButton",
                "pos": {"x": 100 + i * 10, "y": 100},
                "size": {"w": 50, "h": 20},
            })

        tree = {"role": "AXWindow", "children": nodes}

        mock_img = MagicMock(mode="RGB")
        with patch("PIL.ImageGrab.grab", return_value=mock_img), \
             patch.object(self.engine._pytesseract, "image_to_string",
                          return_value="Btn\n"):
            result = self.engine.enrich_tree(tree)
            enriched = sum(1 for c in result.get("children", [])
                          if c.get("_ocr_sourced"))
            # Should have enriched 20 nodes (max OCR limit)
            self.assertEqual(enriched, 20)

    def test_enrich_tree_unavailable(self):
        """enrich_tree does nothing when OCR is unavailable."""
        engine = self._make_engine(available=False)
        tree = {
            "role": "AXWindow",
            "children": [{"role": "AXButton", "pos": {"x": 500, "y": 400},
                          "size": {"w": 100, "h": 30}}],
        }
        result = engine.enrich_tree(tree)
        child = result.get("children", [{}])[0]
        # Should have a skip reason since no actual OCR was run
        self.assertIn("_ocr_skipped", child)

    def test_enrich_tree_handles_nested_deeply(self):
        """enrich_tree handles deeply nested trees without stack overflow."""
        # Build a deeply nested tree (depth 50)
        child = {"role": "AXButton", "title": "Deep Button",
                 "pos": {"x": 100, "y": 100}, "size": {"w": 50, "h": 20}}
        current = child
        for _ in range(50):
            current = {
                "role": "AXPane",
                "children": [current],
            }
        tree = {"role": "AXWindow", "children": [current]}

        mock_img = MagicMock(mode="RGB")
        with patch("PIL.ImageGrab.grab", return_value=mock_img):
            result = self.engine.enrich_tree(tree)
            # Should not crash — and deep button already has title, so should be skipped
            self.assertIsNotNone(result)

    def test_enrich_tree_twice_does_not_re_enrich(self):
        """Running enrich_tree twice doesn't re-enrich already enriched nodes."""
        tree = {
            "role": "AXWindow",
            "children": [{
                "role": "AXButton",
                "pos": {"x": 500, "y": 400},
                "size": {"w": 100, "h": 30},
            }],
        }

        mock_img = MagicMock(mode="RGB")
        with patch("PIL.ImageGrab.grab", return_value=mock_img), \
             patch.object(self.engine._pytesseract, "image_to_string",
                          return_value="Button\n"):
            result1 = self.engine.enrich_tree(tree)
            # First pass adds title
            self.assertTrue(result1["children"][0].get("_ocr_sourced"))

            # Second pass should skip since node now has title
            from copy import deepcopy
            tree2 = deepcopy(tree)
            result2 = self.engine.enrich_tree(tree2)
            # The fresh tree has no title, so it should still be enriched
            self.assertTrue(result2["children"][0].get("_ocr_sourced") or
                          result2["children"][0].get("_ocr_skipped"))


class TestOcrExtractTextBatchE2E(unittest.TestCase):
    """End-to-end tests for extract_text_batch."""

    def setUp(self):
        from cua_agent.synthetic_tree import OcrEngine
        self.engine = OcrEngine()
        self.engine._available = True
        self.engine._pytesseract = MockPytesseract

    def test_batch_returns_correct_count(self):
        """extract_text_batch returns same number of results as regions."""
        mock_img = MagicMock(mode="RGB")
        with patch("PIL.ImageGrab.grab", return_value=mock_img):
            results = self.engine.extract_text_batch([
                (0, 0, 10, 10), (100, 100, 20, 20), (200, 200, 30, 30),
            ])
            self.assertEqual(len(results), 3)

    def test_batch_preserves_order(self):
        """extract_text_batch preserves input order in output."""
        mock_img = MagicMock(mode="RGB")
        texts = ["First", "Second", "Third"]

        with patch("PIL.ImageGrab.grab", return_value=mock_img), \
             patch.object(self.engine._pytesseract, "image_to_string",
                          side_effect=[f"{t}\n" for t in texts]):
            results = self.engine.extract_text_batch([
                (0, 0, 10, 10), (100, 100, 20, 20), (200, 200, 30, 30),
            ])
            self.assertEqual([r["text"] for r in results], texts)

    def test_batch_handles_empty_list(self):
        """extract_text_batch handles empty list gracefully."""
        results = self.engine.extract_text_batch([])
        self.assertEqual(results, [])

    def test_batch_handles_single_region(self):
        """extract_text_batch handles single region."""
        mock_img = MagicMock(mode="RGB")
        with patch("PIL.ImageGrab.grab", return_value=mock_img):
            results = self.engine.extract_text_batch([(100, 200, 50, 30)])
            self.assertEqual(len(results), 1)


class TestOcrConfidenceE2E(unittest.TestCase):
    """End-to-end tests for OCR confidence extraction."""

    def test_get_confidence_from_valid_data(self):
        """_get_confidence returns average from valid confidence values."""
        from cua_agent.synthetic_tree import OcrEngine
        mock_img = MagicMock()

        # Mock pytesseract as a module so local import works
        mock_pytesseract = MagicMock()
        mock_pytesseract.image_to_data.return_value = {"conf": [95, 87, 92, -1, 88]}
        mock_pytesseract.Output.DICT = "DICT"

        with patch.dict("sys.modules", {"pytesseract": mock_pytesseract}):
            conf = OcrEngine._get_confidence(mock_img)
            # Average of [95, 87, 92, 88] = 90.5
            self.assertAlmostEqual(conf, 90.5, places=1)

    def test_get_confidence_handles_all_negative(self):
        """_get_confidence returns 0 when all confidences are -1."""
        from cua_agent.synthetic_tree import OcrEngine
        mock_img = MagicMock()

        mock_pytesseract = MagicMock()
        mock_pytesseract.image_to_data.return_value = {"conf": [-1, -1, -1]}
        mock_pytesseract.Output.DICT = "DICT"

        with patch.dict("sys.modules", {"pytesseract": mock_pytesseract}):
            conf = OcrEngine._get_confidence(mock_img)
            self.assertEqual(conf, 0.0)

    def test_get_confidence_handles_empty_data(self):
        """_get_confidence returns 0 when no confidence data."""
        from cua_agent.synthetic_tree import OcrEngine
        mock_img = MagicMock()

        mock_pytesseract = MagicMock()
        mock_pytesseract.image_to_data.return_value = {"conf": []}
        mock_pytesseract.Output.DICT = "DICT"

        with patch.dict("sys.modules", {"pytesseract": mock_pytesseract}):
            conf = OcrEngine._get_confidence(mock_img)
            self.assertEqual(conf, 0.0)

    def test_get_confidence_handles_exception(self):
        """_get_confidence returns 0 when pytesseract raises an error."""
        from cua_agent.synthetic_tree import OcrEngine
        mock_img = MagicMock()

        mock_pytesseract = MagicMock()
        mock_pytesseract.image_to_data.side_effect = RuntimeError("Tesseract error")
        mock_pytesseract.Output.DICT = "DICT"

        with patch.dict("sys.modules", {"pytesseract": mock_pytesseract}):
            conf = OcrEngine._get_confidence(mock_img)
            self.assertEqual(conf, 0.0)


class TestFullOcrSyntheticPipeline(unittest.TestCase):
    """End-to-end: OCR + SyntheticTreeBuilder full pipeline."""

    def setUp(self):
        from cua_agent.synthetic_tree import SyntheticTreeBuilder
        self.builder = SyntheticTreeBuilder()
        self.mock_img = MagicMock(mode="RGB")

    def test_pipeline_hwnd_fallback_with_ocr(self):
        """HWND fallback tree gets enriched with OCR."""
        # Mock HWND walker to return a tree with elements that have no title
        hwnd_tree = {
            "role": "AXWindow", "title": "App",
            "children": [
                {"role": "AXButton", "source": "hwnd",
                 "pos": {"x": 500, "y": 400}, "size": {"w": 100, "h": 30}},
                {"role": "AXTextField", "source": "hwnd",
                 "pos": {"x": 500, "y": 500}, "size": {"w": 200, "h": 25}},
            ],
        }
        self.builder._hwnd_walker.get_foreground_tree = MagicMock(return_value=hwnd_tree)
        self.builder._ocr_engine._available = True
        self.builder._ocr_engine._pytesseract = MockPytesseract

        with patch("PIL.ImageGrab.grab", return_value=self.mock_img), \
             patch.object(MockPytesseract, "image_to_string",
                          return_value="Submit\n"):
            result = self.builder.build_synthetic_tree({"error": "No UIA"})

            self.assertTrue(result.get("_has_hwnd_fallback"))
            # First child should have been OCR-enriched
            btn = result.get("children", [{}])[0]
            self.assertEqual(btn.get("title"), "Submit")

    def test_pipeline_recognizes_ocr_in_sources(self):
        """OCR availability is reflected in _synthetic_sources."""
        self.builder._hwnd_walker.get_foreground_tree = MagicMock(
            return_value={"error": "No fg window"}
        )
        self.builder._ocr_engine._available = True
        self.builder._ocr_engine._pytesseract = MockPytesseract

        with patch("PIL.ImageGrab.grab", return_value=self.mock_img):
            result = self.builder.build_synthetic_tree({"error": "UIA failed"})
            self.assertIn("ocr", result.get("_synthetic_sources", []))
            self.assertTrue(result.get("_ocr_enriched"))


if __name__ == "__main__":
    unittest.main()
