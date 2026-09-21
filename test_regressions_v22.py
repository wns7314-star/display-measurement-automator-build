from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
from PIL import Image, ImageDraw

from benchmark_regressions import make_cases, test_font
from measurement_ocr_v2 import (
    BatchCancelled, Box, ImageIdentity, ImageScanResult, MeasurementDetection,
    OCRAttempt, RapidOCREngine, _merge_full_image_attempts, _same_text_box,
    export_excel, is_scale_bar_candidate, scan_image, search_tiles, verify_xlsx,
)


NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def workbook_values(path):
    with zipfile.ZipFile(path) as z:
        strings = ["".join(x.itertext()) for x in ET.fromstring(z.read("xl/sharedStrings.xml"))]
        root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        cells = {}
        for cell in root.findall(".//s:c", NS):
            v = cell.find("s:v", NS)
            if v is not None:
                cells[cell.attrib["r"]] = strings[int(v.text)] if cell.get("t") == "s" else float(v.text)
        names = [x.get("name") for x in ET.fromstring(z.read("xl/workbook.xml")).findall(".//s:sheet", NS)]
        return cells, names, root


class OCRRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = RapidOCREngine()
        cls.temp = tempfile.TemporaryDirectory()
        cls.cases = {p.stem: (p, expected) for p, expected in make_cases(cls.temp.name)}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def check_case(self, name, deep=False):
        path, expected = self.cases[name]
        result = scan_image(ImageIdentity(path, "1", 1), self.engine, deep_verify=deep)
        self.assertFalse(result.error, result.error)
        found = [(d.value_text, d.unit) for d in result.included_detections if d.value_text]
        self.assertCountEqual(found, expected, [(d.raw_text, d.note) for d in result.detections])
        for d in result.detections:
            self.assertTrue(0 <= d.box.x < d.box.x2 <= result.image_width)
            self.assertTrue(0 <= d.box.y < d.box.y2 <= result.image_height)
        return result

    def test_left_right_edges_alongside_length(self):
        self.check_case("edges_with_center_anchor")

    def test_small_low_contrast_monochrome(self):
        self.check_case("small_faint_gray")

    def test_faint_colored_labels(self):
        result = self.check_case("faint_color")
        self.assertEqual({d.point_name for d in result.included_detections}, {"Top-W", "A1"})

    def test_high_resolution_small_labels(self):
        self.check_case("large_canvas_small_text")

    def test_bottom_angles_not_removed_as_scale(self):
        result = self.check_case("bottom_angles_and_scale")
        self.assertTrue(any(not d.included and d.value_text == "100" and d.unit == "nm" for d in result.detections))

    def test_deep_mode_uses_same_original_coordinates_and_no_duplicates(self):
        self.check_case("edges_with_center_anchor", deep=True)

    def test_clockwise_and_counterclockwise_edge_labels(self):
        self.check_case("vertical_edges")

    def test_blank_image_does_not_invent_measurements(self):
        path = Path(self.temp.name) / "blank.png"
        Image.new("RGB", (700, 480), (150, 150, 150)).save(path)
        result = scan_image(ImageIdentity(path, "1", 1), self.engine)
        self.assertFalse(result.error)
        self.assertFalse(result.selected)
        self.assertEqual(result.detections, [])

    def test_cancellation_propagates_during_image_scan(self):
        path, _ = self.cases["large_canvas_small_text"]
        with self.assertRaises(BatchCancelled):
            scan_image(ImageIdentity(path, "1", 1), self.engine, cancelled=lambda: True)


class MergeAndCoverageTests(unittest.TestCase):
    def test_every_pixel_including_outer_edges_is_covered(self):
        for width, height in [(1, 1), (641, 639), (2200, 1300), (700, 2500)]:
            covered = np.zeros((height, width), dtype=bool)
            for b in search_tiles(width, height):
                covered[b.y:b.y2, b.x:b.x2] = True
            self.assertTrue(covered.all())

    def test_invalid_tile_overlap_fails_instead_of_looping(self):
        with self.assertRaises(ValueError):
            search_tiles(100, 100, side=100, overlap=100)

    def test_identical_neighbour_values_remain_two_measurements(self):
        result = ImageScanResult(Path("x.png"), "1", 1, 4000, 2000)
        _merge_full_image_attempts(result, [
            (Box(10, 50, 75, 20), OCRAttempt("10nm", .98, "raw", "10", "nm")),
            (Box(105, 50, 75, 20), OCRAttempt("10nm", .98, "raw", "10", "nm")),
        ])
        self.assertEqual(len(result.detections), 2)

    def test_overlapping_passes_do_not_duplicate_and_conflicts_require_review(self):
        result = ImageScanResult(Path("x.png"), "1", 1, 1000, 600)
        items = [
            (Box(10, 50, 80, 20), OCRAttempt("12.5°", .98, "raw", "12.5", "°")),
            (Box(9, 49, 82, 22), OCRAttempt("12.5°", .99, "contrast", "12.5", "°")),
        ]
        _merge_full_image_attempts(result, items)
        self.assertEqual(len(result.detections), 1)
        self.assertFalse(result.detections[0].review_required)
        _merge_full_image_attempts(result, [(items[0][0], OCRAttempt("72.5°", .99, "chroma", "72.5", "°"))])
        self.assertEqual(len(result.detections), 1)
        self.assertTrue(result.detections[0].review_required)
        self.assertEqual(result.detections[0].value_text, "12.5")

    def test_bottom_length_without_bar_is_not_discarded(self):
        rgb = np.full((500, 900, 3), 150, np.uint8)
        self.assertFalse(is_scale_bar_candidate(rgb, Box(5, 455, 85, 20), OCRAttempt("25nm", .99, "raw", "25", "nm")))


class SimpleExcelTests(unittest.TestCase):
    def results(self):
        results = []
        for i, value in enumerate(("10.00", "12.00"), 1):
            d = MeasurementDetection(f"d{i}", Box(100, 100, 80, 20), 1000, 600,
                order=1, value_text=value, unit="nm", point_name="H1", confidence=.99)
            results.append(ImageScanResult(Path(f"{i}-01.png"), str(i), 1, 1000, 600, detections=[d]))
        return results

    def test_default_two_sheets_short_table_numeric_values_average(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "simple.xlsx"
            export_excel(self.results(), p)
            verify_xlsx(p)
            values, names, root = workbook_values(p)
        self.assertEqual(names, ["측정 데이터", "전체 검출 목록"])
        self.assertEqual([values[x] for x in ["A1", "B1", "C1"]], ["시료번호", "파일명", "H1 (nm)"])
        self.assertEqual(values["C2"], 10)
        self.assertEqual(values["C3"], 12)
        self.assertEqual(values["C4"], 11)
        self.assertEqual(root.find(".//s:c[@r='C4']/s:f", NS).text, 'IFERROR(AVERAGE(C2:C3),"")')
        self.assertEqual(root.find("s:dimension", NS).get("ref"), "A1:C4")

    def test_detailed_layout_remains_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "detail.xlsx"
            export_excel(self.results(), p, compact=False)
            verify_xlsx(p)
            values, names, root = workbook_values(p)
        self.assertEqual(len(names), 4)
        self.assertEqual(values["C1"], "데이터 이미지")
        self.assertEqual(values["D4"], 11)

    def test_unconfirmed_numbers_do_not_get_averaged(self):
        results = self.results()
        results[1].detections[0].review_required = True
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "review.xlsx"
            export_excel(results, p)
            values, names, root = workbook_values(p)
        self.assertEqual(values["C4"], "값 확인 필요")
        self.assertIsNone(root.find(".//s:c[@r='C4']/s:f", NS))

    def test_zero_and_missing_value_are_different(self):
        results = self.results()
        results[0].detections[0].value_text = "0.00"
        results[1].detections.clear()
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "zero.xlsx"
            export_excel(results, p)
            values, _, _ = workbook_values(p)
        self.assertEqual(values["C2"], 0)
        self.assertNotIn("C3", values)
        self.assertEqual(values["C4"], 0)

    def test_failed_export_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "keep.xlsx"
            p.write_bytes(b"original")
            with self.assertRaises(ValueError):
                export_excel([], p, overwrite=True)
            self.assertEqual(p.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main(verbosity=2)
