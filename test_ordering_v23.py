"""Order-editing regressions: data, saved reference, Excel, and real Tk buttons."""
from __future__ import annotations

import copy
import itertools
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

import display_measurement_automator_v2 as gui
from measurement_ocr_v2 import (
    Box, ImageScanResult, MeasurementDetection, assign_export_columns,
    create_template_from_result, derive_group_order, export_excel, load_template,
    move_detection_within_group, order_result_by_groups, order_result_by_template,
    save_template, verify_xlsx,
)
from test_regressions_v22 import NS, workbook_values


def sample(number=1, *, named=True):
    result = ImageScanResult(Path(f"{number}-01.png"), str(number), 1, 1000, 800)
    # Two angles followed by heights 3, 4, 5 (nm and micrometres share one group).
    for index, (name, value, unit) in enumerate([
        ("A1", "15.2", "°"), ("A2", "28.4", "°"),
        ("H3", "30", "nm"), ("Top-W", "40", "µm"), ("Gate_1", "50", "nm"),
    ], 1):
        result.detections.append(MeasurementDetection(
            detection_id=f"{number}-{index}", box=Box(100, 70 * index, 90, 25),
            image_width=1000, image_height=800, order=index,
            value_text=value if number == 1 else str(float(value) + 2), unit=unit,
            point_name=name if named else "", raw_text=f"{name}={value}{unit}",
            confidence=0.99,
        ))
    return result


def names(result):
    return [d.point_name for d in result.included_detections]


def reorder(result):
    assert move_detection_within_group(result, result.detections[4], -1)
    return result


EXPECTED = ["A1", "A2", "H3", "Gate_1", "Top-W"]


class WithinGroupTests(unittest.TestCase):
    def test_move_up_down_preserves_values_boxes_and_identity(self):
        result = sample()
        selected = result.detections[4]
        before = {id(d): (d.detection_id, d.box.as_tuple(), d.value_text, d.unit) for d in result.detections}
        self.assertTrue(move_detection_within_group(result, selected, -1))
        self.assertEqual(names(result), EXPECTED)
        self.assertIs(result.detections[3], selected)
        self.assertEqual([d.order for d in result.detections], [1, 2, 3, 4, 5])
        self.assertEqual(before, {id(d): (d.detection_id, d.box.as_tuple(), d.value_text, d.unit) for d in result.detections})
        self.assertTrue(move_detection_within_group(result, selected, 1))
        self.assertEqual(names(result), names(sample()))

    def test_boundaries_excluded_foreign_invalid_direction_and_singleton(self):
        result = sample()
        for selected, direction in [(result.detections[0], -1), (result.detections[1], 1),
                                    (result.detections[2], -1), (result.detections[4], 1),
                                    (result.detections[3], 0), (copy.deepcopy(result.detections[3]), -1)]:
            before = copy.deepcopy(result)
            self.assertFalse(move_detection_within_group(result, selected, direction))
            self.assertEqual(result, before)
        result.detections[3].included = False
        self.assertFalse(move_detection_within_group(result, result.detections[3], -1))
        result.detections = [result.detections[0]]
        self.assertFalse(move_detection_within_group(result, result.detections[0], 1))

    def test_move_group_preserves_each_groups_internal_order(self):
        result = reorder(sample())
        order_result_by_groups(result, ["length", "angle"], preserve_within_group=True)
        self.assertEqual(names(result), ["H3", "Gate_1", "Top-W", "A1", "A2"])
        self.assertEqual(derive_group_order(result.detections), ["length", "angle"])
        order_result_by_groups(result, ["angle", "length"], preserve_within_group=True)
        self.assertEqual(names(result), EXPECTED)

    def test_template_json_reapply_uses_names_even_if_positions_change(self):
        reference = create_template_from_result(reorder(sample()))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "기준 순서 !.json"
            save_template(reference, path)
            loaded = load_template(path)
        self.assertEqual(reference.to_dict(), loaded.to_dict())
        other = sample(2)
        for i, d in enumerate(other.detections):
            d.box = Box(700, 650 - 70 * i, 90, 25)
        order_result_by_template(other, loaded)
        self.assertEqual(names(other), EXPECTED)
        self.assertEqual([d.value_text for d in other.detections], ["17.2", "30.4", "32.0", "52.0", "42.0"])

    def test_unnamed_values_keep_spatial_identity_not_old_row_number(self):
        reference = create_template_from_result(reorder(sample(named=False)))
        other = sample(2, named=False)
        other.detections.reverse()
        for d in other.detections:
            d.box.x += 3
        order_result_by_template(other, reference)
        self.assertEqual([d.value_text for d in other.detections], ["17.2", "30.4", "32.0", "52.0", "42.0"])
        columns = assign_export_columns([other], reference)
        self.assertEqual([d.column_key for d in other.detections], [c.key for c in columns])

    def test_missing_point_stays_empty_and_new_named_point_appends_to_group(self):
        reference = create_template_from_result(reorder(sample()))
        other = sample(2)
        # A new, explicitly different point at the missing point's old location.
        other.detections[3].point_name = "NEW-H"
        order_result_by_template(other, reference)
        self.assertEqual(names(other), ["A1", "A2", "H3", "Gate_1", "NEW-H"])
        columns = assign_export_columns([other], reference)
        self.assertEqual([c.point_name for c in columns], EXPECTED + ["NEW-H"])
        self.assertNotIn("T5", [d.column_key for d in other.detections])
        self.assertTrue(other.detections[-1].column_key.startswith("E"))

    def test_same_name_in_wrong_unit_group_cannot_cross_group_boundary(self):
        reference = create_template_from_result(reorder(sample()))
        other = sample(2)
        other.detections[4].unit = "°"
        order_result_by_template(other, reference)
        columns = assign_export_columns([other], reference)
        changed = next(d for d in other.detections if d.point_name == "Gate_1")
        self.assertTrue(changed.column_key.startswith("E"))
        self.assertEqual(next(c for c in columns if c.key == changed.column_key).group_key, "angle")
        self.assertNotIn("T4", [d.column_key for d in other.detections])
        self.assertEqual(len(other.detections), 5)

    def test_no_template_export_keeps_first_images_current_order(self):
        result = reorder(sample())
        columns = assign_export_columns([result], None)
        self.assertEqual([c.point_name for c in columns], EXPECTED)

    def test_all_height_permutations_export_values_and_average_in_correct_columns(self):
        from xlsxwriter.utility import xl_col_to_name
        for heights, groups, compact in itertools.product(
            itertools.permutations(["H3", "Top-W", "Gate_1"]),
            (["angle", "length"], ["length", "angle"]), (True, False),
        ):
            with self.subTest(heights=heights, groups=groups, compact=compact):
                first, second = sample(), sample(2)
                target = ["A1", "A2"] + list(heights)
                by_name = {d.point_name: d for d in first.detections}
                first.detections = [by_name[name] for name in target]
                for i, d in enumerate(first.detections, 1):
                    d.order = i
                order_result_by_groups(first, groups, preserve_within_group=True)
                target = names(first)
                template = create_template_from_result(first, groups)
                # Export must obey the reference even when the second image is unsorted.
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "output.xlsx"
                    export_excel([first, second], path, template=template, compact=compact)
                    verify_xlsx(path)
                    cells, sheets, root = workbook_values(path)
                first_values = {d.point_name: float(d.value_text) for d in first.detections}
                second_values = {d.point_name: float(d.value_text) for d in second.detections}
                for i, name in enumerate(target, 2 if compact else 3):
                    col = xl_col_to_name(i)
                    self.assertEqual(cells[col + "1"], f"{name} ({by_name[name].unit})")
                    self.assertEqual(cells[col + "2"], first_values[name])
                    self.assertEqual(cells[col + "3"], second_values[name])
                    self.assertAlmostEqual(cells[col + "4"], (first_values[name] + second_values[name]) / 2)
                    formula = root.find(f'.//s:c[@r="{col}4"]/s:f', NS)
                    self.assertIn(f"AVERAGE({col}2:{col}3)", formula.text)

    def test_excluded_values_not_saved_or_exported_and_other_values_not_lost(self):
        result = reorder(sample())
        result.detections[0].included = False
        order_result_by_groups(result, ["angle", "length"], preserve_within_group=True)
        reference = create_template_from_result(result)
        self.assertEqual([p.point_name for p in reference.points], EXPECTED[1:])
        self.assertEqual(len(assign_export_columns([result], reference)), 4)
        self.assertEqual(len(result.detections), 5)


class ReviewWindowTests(unittest.TestCase):
    """Actual Tk widgets and callbacks; mandatory on Windows, skipped headless."""
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            if sys.platform == "win32":
                raise
            self.skipTest(f"No desktop display for Tk: {exc}")
        self.root.geometry("500x200+0+0")
        self.addCleanup(self.root.destroy)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.results = [sample(), sample(2)]
        for result in self.results:
            result.path = Path(self.temp.name) / result.path.name
            Image.new("RGB", (1000, 800), "#777777").save(result.path)
        self.wait_patch = patch.object(tk.Misc, "wait_window", return_value=None)
        self.wait_patch.start()
        self.addCleanup(self.wait_patch.stop)
        self.info_patch = patch.object(gui.messagebox, "showinfo", return_value="ok")
        self.info_patch.start()
        self.addCleanup(self.info_patch.stop)
        self.dialog = gui.ReviewDialog(self.root, self.results, None)
        self.root.update()

    def select(self, name):
        index = next(i for i, d in enumerate(self.dialog._current_result().detections) if d.point_name == name)
        self.dialog.detection_tree.selection_set(str(index))
        self.root.update()
        self.assertEqual(self.dialog._current_detection().point_name, name)

    def test_real_buttons_save_reference_apply_reopen_and_restore(self):
        self.select("Gate_1")
        self.dialog.move_value_up_button.invoke()
        self.root.update()
        self.assertEqual(names(self.dialog.results[0]), EXPECTED)
        self.assertEqual(self.dialog._current_detection().point_name, "Gate_1")
        self.assertEqual(names(self.dialog.results[1]), names(sample()))
        self.dialog.set_template_button.invoke()
        self.root.update()
        self.assertEqual(names(self.dialog.results[1]), EXPECTED)
        results, template = self.dialog.results, self.dialog.template
        self.dialog._accept()
        self.assertTrue(self.dialog.accepted)
        self.dialog = gui.ReviewDialog(self.root, results, template)
        self.root.update()
        self.assertEqual(names(self.dialog.results[0]), EXPECTED)
        self.select("Gate_1")
        self.dialog.move_value_down_button.invoke()
        self.root.update()
        self.assertEqual(names(self.dialog.results[0]), names(sample()))

    def test_edit_include_and_add_preserve_existing_internal_order(self):
        self.select("Gate_1")
        self.dialog.move_value_up_button.invoke()
        with patch.object(gui, "MeasurementEditDialog", return_value=SimpleNamespace(result=("Gate_1", "55", "nm"))):
            self.dialog._edit_detection()
        self.assertEqual(names(self.dialog.results[0]), EXPECTED)
        self.select("H3")
        self.dialog._toggle_detection()
        self.assertEqual(names(self.dialog.results[0]), ["A1", "A2", "Gate_1", "Top-W"])
        self.select("H3")
        self.dialog._toggle_detection()
        self.assertLess(names(self.dialog.results[0]).index("Gate_1"), names(self.dialog.results[0]).index("Top-W"))
        self.dialog._add_mode = True
        self.dialog._preview_map = (1, 0, 0)
        with patch.object(gui, "MeasurementEditDialog", return_value=SimpleNamespace(result=("New-H", "77", "nm"))):
            self.dialog._canvas_click(SimpleNamespace(x=10, y=10))
        self.assertEqual(names(self.dialog.results[0])[-1], "New-H")
        self.assertLess(names(self.dialog.results[0]).index("Gate_1"), names(self.dialog.results[0]).index("Top-W"))

    def test_group_button_does_not_undo_value_move(self):
        self.select("Gate_1")
        self.dialog.move_value_up_button.invoke()
        self.dialog._move_group(-1)
        self.assertEqual(names(self.dialog.results[0]), ["H3", "Gate_1", "Top-W", "A1", "A2"])
        self.assertEqual(self.dialog._current_detection().point_name, "Gate_1")

    def test_cancel_discards_value_order_and_reference_changes(self):
        before = copy.deepcopy(self.results)
        self.select("Gate_1")
        self.dialog.move_value_up_button.invoke()
        self.dialog.set_template_button.invoke()
        self.dialog._cancel()
        self.assertFalse(self.dialog.accepted)
        self.assertEqual(self.results, before)

    def test_buttons_at_boundaries_and_without_selection_are_safe(self):
        self.dialog.current_detection_index = None
        self.dialog.move_value_up_button.invoke()
        self.select("H3")
        self.dialog.move_value_up_button.invoke()
        self.assertEqual(names(self.dialog.results[0]), names(sample()))
        self.dialog._toggle_detection()
        self.select("H3")
        before = copy.deepcopy(self.dialog.results[0])
        self.dialog.move_value_up_button.invoke()
        self.assertEqual(self.dialog.results[0], before)

    def test_new_scan_applies_previously_loaded_reference(self):
        self.dialog.window.destroy()
        app = gui.MeasurementAutomatorApp(self.root)
        app.template = create_template_from_result(reorder(sample()))
        app.events.put(("done", [sample(2)]))
        app._poll_events()
        self.assertEqual(names(app.results[0]), EXPECTED)

    def test_new_buttons_visible_at_minimum_window_size(self):
        self.dialog.window.geometry("980x680+0+0")
        self.root.update()
        window = self.dialog.window
        for widget in (self.dialog.move_value_up_button, self.dialog.move_value_down_button, self.dialog.set_template_button):
            self.assertTrue(widget.winfo_ismapped())
            x, y = widget.winfo_rootx() - window.winfo_rootx(), widget.winfo_rooty() - window.winfo_rooty()
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + widget.winfo_width(), window.winfo_width())
            self.assertLessEqual(y + widget.winfo_height(), window.winfo_height())


if __name__ == "__main__":
    unittest.main(verbosity=2)
