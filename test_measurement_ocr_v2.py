from __future__ import annotations

import json
import queue
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

import numpy as np
from measurement_ocr_v2 import (
    Box,
    ImageIdentity,
    ImageScanResult,
    MeasurementDetection,
    RapidOCREngine,
    assign_export_columns,
    create_template_from_result,
    export_excel,
    load_template,
    mark_duplicate_point_names,
    measurement_group,
    order_result_by_groups,
    order_result_by_template,
    parse_measurement_items,
    parse_measurement_text,
    save_template,
    scan_image,
    validate_manual_measurement,
    validate_point_name,
    verify_xlsx,
)
from PIL import Image, ImageDraw, ImageFont

import display_measurement_automator_v2 as gui

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_1 = ROOT / "upload" / "IMG_9907.jpeg"
SAMPLE_2 = ROOT / "upload" / "IMG_9908.jpeg"


def detection(
    detection_id: str,
    x: int,
    y: int,
    value: str,
    unit: str,
    *,
    width: int = 1000,
    height: int = 800,
    included: bool = True,
    point_name: str = "",
) -> MeasurementDetection:
    return MeasurementDetection(
        detection_id=detection_id,
        box=Box(x, y, 80, 30),
        image_width=width,
        image_height=height,
        value_text=value,
        unit=unit,
        point_name=point_name,
        raw_text=f"{value} {unit}" if value else "",
        confidence=0.99 if value else 0.0,
        source_mode="test",
        included=included,
        review_required=not bool(value),
    )


class ParserTests(unittest.TestCase):
    def test_measurement_units(self) -> None:
        self.assertEqual(parse_measurement_text("428.0 nm"), ("428.0", "nm"))
        self.assertEqual(parse_measurement_text("56.85 μm (cs)"), ("56.85", "µm"))
        self.assertEqual(parse_measurement_text("2.52um"), ("2.52", "µm"))
        self.assertEqual(parse_measurement_text("A1=15.2deg"), ("15.2", "°"))
        self.assertEqual(parse_measurement_text("A2:28.4°"), ("28.4", "°"))

    def test_point_names_and_multiple_mixed_formats(self) -> None:
        items = parse_measurement_items(
            "Top-W=167.5nm Gate_1:573.1um H1 284.2nm A1(15.2deg)"
        )
        self.assertEqual(
            [(item.point_name, item.value_text, item.unit) for item in items],
            [
                ("Top-W", "167.5", "nm"),
                ("Gate_1", "573.1", "µm"),
                ("H1", "284.2", "nm"),
                ("A1", "15.2", "°"),
            ],
        )

    def test_value_first_does_not_attach_following_point_name(self) -> None:
        items = parse_measurement_items("167.5nm H1")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].point_name, "")

    def test_ambiguous_and_non_measurement_text_is_rejected(self) -> None:
        self.assertIsNone(parse_measurement_text("2 35 μm"))
        self.assertIsNone(parse_measurement_text("104mm"))
        self.assertIsNone(parse_measurement_text("MAG 450000x"))

    def test_manual_validation(self) -> None:
        self.assertEqual(
            validate_manual_measurement("001.2300", "um"), ("1.2300", "µm")
        )
        self.assertEqual(validate_manual_measurement("15.20", "deg"), ("15.20", "°"))
        self.assertEqual(validate_point_name("Top-W_1"), "Top-W_1")
        with self.assertRaises(ValueError):
            validate_manual_measurement("12x", "nm")
        with self.assertRaises(ValueError):
            validate_point_name("Top Width")


class RealImageOCRTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not SAMPLE_1.is_file() or not SAMPLE_2.is_file():
            raise unittest.SkipTest("공개 샘플 이미지가 없음")
        cls.engine = RapidOCREngine()

    def test_first_sample_all_four_values(self) -> None:
        started = time.perf_counter()
        result = scan_image(ImageIdentity(SAMPLE_1, "sample", 1), self.engine)
        elapsed = time.perf_counter() - started
        values = {(item.value_text, item.unit) for item in result.detections}
        self.assertEqual(len(result.detections), 4)
        self.assertEqual(
            values,
            {("1.82", "µm"), ("2.35", "µm"), ("6.09", "µm"), ("2.52", "µm")},
        )
        self.assertLess(elapsed, 15.0)

    def test_second_sample_finds_six_positions_and_clear_values(self) -> None:
        started = time.perf_counter()
        result = scan_image(ImageIdentity(SAMPLE_2, "sample", 2), self.engine)
        elapsed = time.perf_counter() - started
        clear_values = {
            (item.value_text, item.unit)
            for item in result.detections
            if item.numeric_value is not None
        }
        self.assertEqual(len(result.detections), 6)
        self.assertTrue({("105", "nm"), ("111", "nm"), ("104", "nm")} <= clear_values)
        self.assertEqual(sum(item.numeric_value is None for item in result.detections), 3)
        self.assertLess(elapsed, 15.0)

    def test_deep_verify_excludes_footer_scale_bar(self) -> None:
        result = scan_image(
            ImageIdentity(SAMPLE_2, "sample", 2), self.engine, deep_verify=True
        )
        included = {(item.value_text, item.unit) for item in result.detections if item.included}
        excluded = {(item.value_text, item.unit) for item in result.detections if not item.included}
        self.assertNotIn(("100", "nm"), included)
        self.assertIn(("100", "nm"), excluded)
        self.assertTrue(any("스케일바" in item.note for item in result.detections if not item.included))

    def test_seven_overlay_colors(self) -> None:
        font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        if not font_path.is_file():
            self.skipTest("시험용 글꼴 없음")
        font = ImageFont.truetype(str(font_path), 24)
        colors = [
            (255, 30, 30),
            (30, 255, 30),
            (30, 80, 255),
            (255, 230, 20),
            (20, 240, 240),
            (255, 30, 255),
            (255, 140, 20),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for index, color in enumerate(colors, 1):
                image = Image.new("RGB", (520, 260), (100, 100, 100))
                draw = ImageDraw.Draw(image)
                draw.line((80, 150, 430, 150), fill=color, width=2)
                draw.line((80, 135, 80, 165), fill=color, width=2)
                draw.line((430, 135, 430, 165), fill=color, width=2)
                draw.text((175, 112), "123.45 nm", font=font, fill=color)
                path = Path(directory) / f"1-{index:02d}.png"
                image.save(path)
                result = scan_image(ImageIdentity(path, "1", index), self.engine)
                self.assertEqual(len(result.detections), 1)
                self.assertEqual(
                    (result.detections[0].value_text, result.detections[0].unit),
                    ("123.45", "nm"),
                )

    def test_grayscale_overlay_uses_full_image_fallback(self) -> None:
        font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        if not font_path.is_file():
            self.skipTest("시험용 글꼴 없음")
        font = ImageFont.truetype(str(font_path), 30)
        with tempfile.TemporaryDirectory() as directory:
            image = Image.new("RGB", (600, 300), (200, 200, 200))
            draw = ImageDraw.Draw(image)
            draw.line((100, 180, 500, 180), fill=(20, 20, 20), width=3)
            draw.line((100, 160, 100, 200), fill=(20, 20, 20), width=3)
            draw.line((500, 160, 500, 200), fill=(20, 20, 20), width=3)
            draw.text((210, 130), "456.7 nm", font=font, fill=(10, 10, 10))
            path = Path(directory) / "1-01.png"
            image.save(path)
            result = scan_image(ImageIdentity(path, "1", 1), self.engine)
        self.assertTrue(result.used_full_fallback)
        self.assertEqual(
            [(item.value_text, item.unit) for item in result.included_detections],
            [("456.7", "nm")],
        )

    def test_labeled_length_and_angle_recognition(self) -> None:
        font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        if not font_path.is_file():
            self.skipTest("시험용 글꼴 없음")
        font = ImageFont.truetype(str(font_path), 36)
        cases = [
            ("H1=167.5 nm", "H1", "167.5", "nm"),
            ("Top-W=167.5 nm", "Top-W", "167.5", "nm"),
            ("Gate_1:573.1 um", "Gate_1", "573.1", "µm"),
            ("A1=15.2 deg", "A1", "15.2", "°"),
        ]
        for text, point_name, value, unit in cases:
            image = Image.new("RGB", (600, 100), (60, 60, 60))
            ImageDraw.Draw(image).text((15, 20), text, font=font, fill=(20, 255, 20))
            attempts = self.engine.recognize_region(
                np.asarray(image), Box(0, 0, 600, 100), "horizontal"
            )
            self.assertTrue(
                any(
                    (item.point_name, item.value_text, item.unit)
                    == (point_name, value, unit)
                    for item in attempts
                ),
                text,
            )

    def test_full_scan_labeled_values_are_separated(self) -> None:
        font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        if not font_path.is_file():
            self.skipTest("시험용 글꼴 없음")
        font = ImageFont.truetype(str(font_path), 24)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "1-01.png"
            image = Image.new("RGB", (800, 500), (100, 100, 100))
            draw = ImageDraw.Draw(image)
            for y, text in [(150, "Top-W=167.5 nm"), (350, "A1=15.2 deg")]:
                draw.line((80, y, 430, y), fill=(30, 255, 30), width=2)
                draw.line((80, y - 15, 80, y + 15), fill=(30, 255, 30), width=2)
                draw.line((430, y - 15, 430, y + 15), fill=(30, 255, 30), width=2)
                draw.text((175, y - 38), text, font=font, fill=(30, 255, 30))
            image.save(path)
            result = scan_image(ImageIdentity(path, "1", 1), self.engine)
        self.assertEqual(
            [
                (item.point_name, item.value_text, item.unit)
                for item in result.included_detections
            ],
            [("Top-W", "167.5", "nm"), ("A1", "15.2", "°")],
        )


class OrderingAndExportTests(unittest.TestCase):
    def build_results(self) -> list[ImageScanResult]:
        first = ImageScanResult(Path("1-01.png"), "1", 1, 1000, 800)
        first.detections = [
            detection("a", 100, 100, "10", "nm"),
            detection("b", 500, 100, "20", "nm"),
        ]
        for index, item in enumerate(first.detections, 1):
            item.order = index

        second = ImageScanResult(Path("2-01.png"), "2", 1, 1000, 800)
        second.detections = [
            detection("d", 505, 102, "22", "nm"),
            detection("c", 105, 98, "12", "nm"),
            detection("extra", 300, 500, "7.5", "µm"),
        ]
        for index, item in enumerate(second.detections, 1):
            item.order = index
        return [first, second]

    def test_template_order_and_extra_position(self) -> None:
        results = self.build_results()
        template = create_template_from_result(results[0])
        order_result_by_template(results[1], template)
        self.assertEqual([item.value_text for item in results[1].detections], ["12", "22", "7.5"])
        columns = assign_export_columns(results, template)
        self.assertEqual(len(columns), 3)
        self.assertEqual(results[1].detections[0].column_key, "T1")
        self.assertEqual(results[1].detections[1].column_key, "T2")
        self.assertTrue(results[1].detections[2].column_key.startswith("E"))

    def test_group_order_and_point_name_matching(self) -> None:
        sample = ImageScanResult(Path("sample.png"), "1", 1, 1000, 800)
        sample.detections = [
            detection("h1", 100, 60, "167.5", "nm", point_name="H1"),
            detection("a1", 100, 140, "15.2", "°", point_name="A1"),
            detection("h2", 100, 220, "2.35", "µm", point_name="Top-W"),
            detection("a2", 100, 300, "28.4", "°", point_name="A2"),
        ]
        order_result_by_groups(sample, ["angle", "length"])
        self.assertEqual(
            [item.point_name for item in sample.included_detections],
            ["A1", "A2", "H1", "Top-W"],
        )
        template = create_template_from_result(sample, ["angle", "length"])
        self.assertEqual(template.group_order, ["angle", "length"])

        moved = ImageScanResult(Path("moved.png"), "2", 1, 1000, 800)
        moved.detections = [
            detection("mh2", 800, 700, "2.50", "µm", point_name="Top-W"),
            detection("ma2", 850, 650, "30.0", "°", point_name="A2"),
            detection("mh1", 900, 600, "170.0", "nm", point_name="H1"),
            detection("ma1", 950, 550, "16.0", "°", point_name="A1"),
        ]
        order_result_by_template(moved, template)
        self.assertEqual(
            [item.point_name for item in moved.included_detections],
            ["A1", "A2", "H1", "Top-W"],
        )
        columns = assign_export_columns([sample, moved], template)
        self.assertEqual(
            [(column.label, column.group_key) for column in columns],
            [
                ("A1", "angle"),
                ("A2", "angle"),
                ("H1", "length"),
                ("Top-W", "length"),
            ],
        )

    def test_duplicate_point_name_is_review_required(self) -> None:
        result = ImageScanResult(Path("duplicate.png"), "1", 1, 1000, 800)
        result.detections = [
            detection("a", 100, 100, "10", "nm", point_name="H1"),
            detection("b", 300, 100, "11", "nm", point_name="h1"),
        ]
        mark_duplicate_point_names(result)
        self.assertTrue(all(item.review_required for item in result.detections))
        self.assertTrue(all("포인트명 중복" in item.note for item in result.detections))

        result.detections[1].point_name = "H2"
        mark_duplicate_point_names(result)
        self.assertTrue(all(not item.review_required for item in result.detections))
        self.assertTrue(all("포인트명 중복" not in item.note for item in result.detections))

    def test_nm_and_um_share_length_group(self) -> None:
        self.assertEqual(measurement_group("nm"), "length")
        self.assertEqual(measurement_group("µm"), "length")
        self.assertEqual(measurement_group("°"), "angle")

    def test_template_json_round_trip(self) -> None:
        template = create_template_from_result(self.build_results()[0])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "template.json"
            save_template(template, path)
            loaded = load_template(path)
            self.assertEqual(loaded.reference_name, template.reference_name)
            self.assertEqual(len(loaded.points), 2)
            self.assertEqual(loaded.group_order, ["length"])
            json.loads(path.read_text(encoding="utf-8"))

    def test_old_template_is_migrated_to_length_group(self) -> None:
        old_template = {
            "schema_version": 2,
            "reference_name": "old.png",
            "reference_width": 1000,
            "reference_height": 800,
            "group_order": "invalid-old-value",
            "points": [
                {
                    "slot_id": "T1",
                    "order": 1,
                    "x_norm": 0.2,
                    "y_norm": 0.3,
                    "expected_unit": "nm",
                    "label": "측정값 1",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old-template.json"
            path.write_text(json.dumps(old_template), encoding="utf-8")
            loaded = load_template(path)
        self.assertEqual(loaded.group_order, ["length"])
        self.assertEqual(loaded.points[0].group_key, "length")

    def test_excel_has_numeric_table_average_and_detail_sheets(self) -> None:
        results = self.build_results()
        results[0].detections[0].value_text = "10.00"
        results[0].detections[0].raw_text = "10.00 nm"
        template = create_template_from_result(results[0])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.xlsx"
            summary = export_excel(results, path, template=template)
            verify_xlsx(path)
            self.assertEqual(summary.selected_images, 2)
            self.assertEqual(summary.measurement_count, 5)
            self.assertEqual(summary.column_count, 3)
            with zipfile.ZipFile(path, "r") as archive:
                sheet_xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
                strings_xml = archive.read("xl/sharedStrings.xml").decode("utf-8")
                styles_xml = archive.read("xl/styles.xml").decode("utf-8")
            self.assertIn("AVERAGE(", sheet_xml)
            self.assertIn("높이 1 (nm)", strings_xml)
            self.assertIn("포인트명", strings_xml)
            self.assertIn("측정 그룹", strings_xml)
            self.assertIn('formatCode="0.00"', styles_xml)
            self.assertIn("전체 검출 목록", archive_name_text(path))

    def test_excel_headers_keep_groups_contiguous(self) -> None:
        result = ImageScanResult(Path("grouped.png"), "1", 1, 1000, 800)
        result.detections = [
            detection("h1", 100, 50, "167.5", "nm", point_name="H1"),
            detection("a1", 100, 120, "15.2", "°", point_name="A1"),
            detection("h2", 100, 200, "2.35", "µm", point_name="Top-W"),
            detection("a2", 100, 280, "28.4", "°", point_name="A2"),
        ]
        template = create_template_from_result(result, ["angle", "length"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grouped.xlsx"
            export_excel([result], path, template=template)
            with zipfile.ZipFile(path, "r") as archive:
                strings_xml = archive.read("xl/sharedStrings.xml").decode("utf-8")
        positions = [
            strings_xml.index(header)
            for header in ["A1 (°)", "A2 (°)", "H1 (nm)", "Top-W (µm)"]
        ]
        self.assertEqual(positions, sorted(positions))

    def test_same_named_point_with_mixed_units_blocks_average(self) -> None:
        first = ImageScanResult(Path("1-01.png"), "1", 1, 1000, 800)
        first.detections = [
            detection("h1", 100, 100, "500", "nm", point_name="H1")
        ]
        second = ImageScanResult(Path("2-01.png"), "2", 1, 1000, 800)
        second.detections = [
            detection("h1", 700, 600, "0.5", "µm", point_name="H1")
        ]
        template = create_template_from_result(first)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mixed-units.xlsx"
            export_excel([first, second], path, template=template)
            with zipfile.ZipFile(path, "r") as archive:
                sheet_xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
                strings_xml = archive.read("xl/sharedStrings.xml").decode("utf-8")
        self.assertIn("H1 (단위 확인 필요)", strings_xml)
        self.assertIn("단위 확인 필요", strings_xml)
        self.assertNotIn("AVERAGE(D2:D3)", sheet_xml)


class GUIWorkflowTests(unittest.TestCase):
    def test_runtime_smoke_test_loads_local_ocr_models(self) -> None:
        self.assertTrue(gui.runtime_smoke_test())

    def test_scan_button_passes_deep_verify_to_worker(self) -> None:
        class Widget:
            def configure(self, **_kwargs: object) -> None:
                return None

        class Variable:
            def get(self) -> bool:
                return True

        class Root:
            def after(self, *_args: object) -> None:
                return None

        app = object.__new__(gui.MeasurementAutomatorApp)
        app.paths = [Path("sample.png")]
        app.results = []
        app.worker = None
        app.cancel_event = threading.Event()
        app.events = queue.Queue()
        app.progress = Widget()
        app.scan_button = Widget()
        app.cancel_button = Widget()
        app.review_button = Widget()
        app.export_button = Widget()
        app.deep_var = Variable()
        app.root = Root()

        received: dict[str, bool] = {}
        original = gui.scan_images

        def fake_scan_images(
            _paths: object,
            *,
            deep_verify: bool,
            progress: object,
            cancelled: object,
        ) -> list[ImageScanResult]:
            received["deep_verify"] = deep_verify
            return []

        gui.scan_images = fake_scan_images
        try:
            app._start_scan()
            self.assertIsNotNone(app.worker)
            app.worker.join(timeout=2.0)
        finally:
            gui.scan_images = original

        self.assertFalse(app.worker.is_alive())
        self.assertEqual(received, {"deep_verify": True})
        self.assertEqual(app.events.get_nowait()[0], "done")


def archive_name_text(path: Path) -> str:
    with zipfile.ZipFile(path, "r") as archive:
        return archive.read("xl/workbook.xml").decode("utf-8")


if __name__ == "__main__":
    unittest.main(verbosity=2)
