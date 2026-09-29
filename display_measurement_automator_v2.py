from __future__ import annotations

import copy
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from measurement_ocr_v2 import (
    APP_VERSION,
    SUPPORTED_SUFFIXES,
    Box,
    ImageScanResult,
    MeasurementDetection,
    MeasurementTemplate,
    RapidOCREngine,
    complete_group_order,
    create_template_from_result,
    derive_group_order,
    export_excel,
    infer_identities,
    load_template,
    mark_duplicate_point_names,
    measurement_group_label,
    move_detection_within_group,
    order_result_by_groups,
    order_result_by_template,
    save_template,
    scan_images,
    validate_manual_measurement,
    validate_point_name,
)
from PIL import Image, ImageOps, ImageTk


def open_path(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class MeasurementEditDialog:
    def __init__(
        self,
        parent: tk.Misc,
        *,
        title: str,
        point_name: str = "",
        value: str = "",
        unit: str = "",
        allow_blank: bool = False,
    ) -> None:
        self.result: tuple[str, str, str] | None = None
        self.allow_blank = allow_blank
        window = self.window = tk.Toplevel(parent)
        window.title(title)
        window.transient(parent)
        window.resizable(False, False)

        body = ttk.Frame(window, padding=16)
        body.grid(sticky="nsew")
        ttk.Label(body, text="포인트명 (없으면 빈칸)").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        self.point_var = tk.StringVar(value=point_name)
        point_entry = ttk.Entry(body, textvariable=self.point_var, width=36)
        point_entry.grid(row=1, column=0, columnspan=2, pady=(4, 12), sticky="ew")

        ttk.Label(body, text="수치값 (숫자만)").grid(row=2, column=0, sticky="w")
        self.value_var = tk.StringVar(value=value)
        value_entry = ttk.Entry(body, textvariable=self.value_var, width=24)
        value_entry.grid(row=3, column=0, padx=(0, 10), pady=(4, 12), sticky="ew")

        ttk.Label(body, text="단위").grid(row=2, column=1, sticky="w")
        self.unit_var = tk.StringVar(value=unit or "nm")
        unit_combo = ttk.Combobox(
            body,
            textvariable=self.unit_var,
            values=("nm", "µm", "°"),
            state="readonly",
            width=8,
        )
        unit_combo.grid(row=3, column=1, pady=(4, 12), sticky="ew")

        note = "빈 값으로 두면 검토 필요 상태로 저장됩니다." if allow_blank else "예: 428.0 또는 56.85"
        ttk.Label(body, text=note, foreground="#666666").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )
        buttons = ttk.Frame(body)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="취소", command=window.destroy).pack(side="right")
        ttk.Button(buttons, text="적용", command=self._accept).pack(
            side="right", padx=(0, 8)
        )
        window.bind("<Return>", lambda _event: self._accept())
        window.bind("<Escape>", lambda _event: window.destroy())
        value_entry.focus_set()
        window.grab_set()
        window.wait_visibility()
        window.update_idletasks()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - window.winfo_width()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - window.winfo_height()) // 2)
        window.geometry(f"+{x}+{y}")
        parent.wait_window(window)

    def _accept(self) -> None:
        point_name = self.point_var.get().strip()
        value = self.value_var.get().strip()
        unit = self.unit_var.get().strip()
        try:
            point_name = validate_point_name(point_name)
        except ValueError as exc:
            messagebox.showerror("입력 확인", str(exc), parent=self.window)
            return
        if not value and self.allow_blank:
            self.result = (point_name, "", unit)
            self.window.destroy()
            return
        try:
            normalized_value, normalized_unit = validate_manual_measurement(value, unit)
            self.result = (point_name, normalized_value, normalized_unit)
        except ValueError as exc:
            messagebox.showerror("입력 확인", str(exc), parent=self.window)
            return
        self.window.destroy()


class ReviewDialog:
    def __init__(
        self,
        parent: tk.Misc,
        results: list[ImageScanResult],
        template: MeasurementTemplate | None,
    ) -> None:
        self.results = copy.deepcopy(results)
        self.template = copy.deepcopy(template)
        self.accepted = False
        self.current_result_index = 0
        self.current_detection_index: int | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._preview_map = (1.0, 0.0, 0.0)
        self._add_mode = False
        self.group_order = list(template.group_order) if template else []
        for result in self.results:
            self.group_order = complete_group_order(
                self.group_order or derive_group_order(result.detections),
                result.detections,
            )
        for result in self.results:
            order_result_by_groups(result, self.group_order, preserve_within_group=True)

        window = self.window = tk.Toplevel(parent)
        window.title("후보 이미지 · 측정값 검토")
        window.geometry("1280x820")
        window.minsize(980, 680)
        window.transient(parent)
        window.protocol("WM_DELETE_WINDOW", self._cancel)

        toolbar = ttk.Frame(window, padding=(10, 8))
        toolbar.pack(fill="x")
        ttk.Label(
            toolbar,
            text="값을 확인하고, 그룹 순서와 그룹 안의 값 순서를 지정하세요.",
            font=("맑은 고딕", 10, "bold"),
        ).pack(side="left")
        self.summary_var = tk.StringVar()
        ttk.Label(toolbar, textvariable=self.summary_var, foreground="#A05A00").pack(
            side="right"
        )

        paned = ttk.Panedwindow(window, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        left = ttk.Frame(paned)
        right = ttk.Frame(paned)
        paned.add(left, weight=2)
        paned.add(right, weight=3)

        image_columns = ("use", "status", "sample", "seq", "count", "file")
        self.image_tree = ttk.Treeview(
            left, columns=image_columns, show="headings", selectmode="browse"
        )
        image_headings = {
            "use": "사용",
            "status": "상태",
            "sample": "시료",
            "seq": "순번",
            "count": "검출",
            "file": "파일명",
        }
        image_widths = {"use": 48, "status": 85, "sample": 85, "seq": 52, "count": 52, "file": 220}
        for column in image_columns:
            self.image_tree.heading(column, text=image_headings[column])
            self.image_tree.column(column, width=image_widths[column], stretch=column == "file")
        image_scroll = ttk.Scrollbar(left, orient="vertical", command=self.image_tree.yview)
        self.image_tree.configure(yscrollcommand=image_scroll.set)
        self.image_tree.pack(side="left", fill="both", expand=True)
        image_scroll.pack(side="right", fill="y")
        self.image_tree.bind("<<TreeviewSelect>>", self._on_image_select)
        self.image_tree.bind("<Double-1>", lambda _event: self._toggle_image())

        preview_frame = ttk.LabelFrame(right, text="원본 미리보기 · 검출 위치")
        preview_frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(preview_frame, bg="#242424", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _event: self._draw_preview())
        self.canvas.bind("<Button-1>", self._canvas_click)

        detail_frame = ttk.LabelFrame(right, text="현재 이미지 측정값")
        detail_frame.pack(fill="x", pady=(8, 0))
        detection_columns = (
            "order",
            "use",
            "point",
            "group",
            "value",
            "unit",
            "conf",
            "status",
            "raw",
        )
        self.detection_tree = ttk.Treeview(
            detail_frame,
            columns=detection_columns,
            show="headings",
            height=8,
            selectmode="browse",
        )
        detection_headings = {
            "order": "순서",
            "use": "사용",
            "point": "포인트명",
            "group": "그룹",
            "value": "수치값",
            "unit": "단위",
            "conf": "신뢰도",
            "status": "상태",
            "raw": "OCR 원문",
        }
        detection_widths = {
            "order": 48,
            "use": 48,
            "point": 105,
            "group": 62,
            "value": 90,
            "unit": 55,
            "conf": 70,
            "status": 100,
            "raw": 180,
        }
        for column in detection_columns:
            self.detection_tree.heading(column, text=detection_headings[column])
            self.detection_tree.column(column, width=detection_widths[column], stretch=column == "raw")
        detail_scroll = ttk.Scrollbar(
            detail_frame, orient="vertical", command=self.detection_tree.yview
        )
        self.detection_tree.configure(yscrollcommand=detail_scroll.set)
        self.detection_tree.pack(side="left", fill="x", expand=True)
        detail_scroll.pack(side="right", fill="y")
        self.detection_tree.bind("<<TreeviewSelect>>", self._on_detection_select)
        self.detection_tree.bind("<Double-1>", lambda _event: self._edit_detection())

        controls = ttk.Frame(right, padding=(0, 8, 0, 0))
        controls.pack(fill="x")
        ttk.Button(controls, text="이미지 사용 전환", command=self._toggle_image).pack(side="left")
        ttk.Separator(controls, orient="vertical").pack(side="left", fill="y", padx=7)
        ttk.Button(controls, text="값 수정", command=self._edit_detection).pack(side="left")
        ttk.Button(controls, text="값 사용 전환", command=self._toggle_detection).pack(side="left", padx=(5, 0))
        ttk.Button(controls, text="누락 위치 추가", command=self._start_add).pack(side="left", padx=(5, 0))

        value_controls = ttk.Frame(right, padding=(0, 5, 0, 0))
        value_controls.pack(fill="x")
        ttk.Label(value_controls, text="같은 그룹 안:", width=14).pack(side="left")
        self.move_value_up_button = ttk.Button(
            value_controls, text="값 위로", command=lambda: self._move_detection(-1)
        )
        self.move_value_up_button.pack(side="left")
        self.move_value_down_button = ttk.Button(
            value_controls, text="값 아래로", command=lambda: self._move_detection(1)
        )
        self.move_value_down_button.pack(side="left", padx=(5, 0))

        group_controls = ttk.Frame(right, padding=(0, 5, 0, 0))
        group_controls.pack(fill="x")
        ttk.Label(group_controls, text="그룹 전체:", width=14).pack(side="left")
        ttk.Button(
            group_controls, text="그룹 위로", command=lambda: self._move_group(-1)
        ).pack(side="left")
        ttk.Button(
            group_controls, text="그룹 아래로", command=lambda: self._move_group(1)
        ).pack(side="left", padx=(5, 0))

        self.set_template_button = ttk.Button(
            right,
            text="현재 순서를 기준으로 설정",
            command=self._set_template,
        )
        self.set_template_button.pack(anchor="w", pady=(5, 3))

        self.hint_var = tk.StringVar(value="값 선택 → 위/아래 이동 → [현재 순서를 기준으로 설정]")
        ttk.Label(right, textvariable=self.hint_var, foreground="#555555", wraplength=480).pack(
            fill="x", pady=(0, 2)
        )

        bottom = ttk.Frame(window, padding=(10, 4, 10, 10))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="취소", command=self._cancel).pack(side="right")
        ttk.Button(bottom, text="검토 완료", command=self._accept).pack(
            side="right", padx=(0, 8)
        )

        self._refresh_image_tree()
        if self.results:
            self.image_tree.selection_set("0")
            self.image_tree.focus("0")
            self._show_result(0)
        self._update_summary()
        window.grab_set()
        parent.wait_window(window)

    def _current_result(self) -> ImageScanResult | None:
        if not self.results:
            return None
        return self.results[self.current_result_index]

    def _current_detection(self) -> MeasurementDetection | None:
        result = self._current_result()
        if result is None or self.current_detection_index is None:
            return None
        if not (0 <= self.current_detection_index < len(result.detections)):
            return None
        return result.detections[self.current_detection_index]

    def _refresh_image_tree(self) -> None:
        selected_iids = self.image_tree.selection()
        for item in self.image_tree.get_children():
            self.image_tree.delete(item)
        for index, result in enumerate(self.results):
            self.image_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    "예" if result.selected else "아니오",
                    result.status,
                    result.sample_id,
                    result.image_index,
                    len(result.detections),
                    result.path.name,
                ),
            )
        for iid in selected_iids:
            if self.image_tree.exists(iid):
                self.image_tree.selection_set(iid)

    def _refresh_detection_tree(self) -> None:
        for item in self.detection_tree.get_children():
            self.detection_tree.delete(item)
        result = self._current_result()
        if result is None:
            return
        for index, detection in enumerate(result.detections):
            self.detection_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    detection.order,
                    "예" if detection.included else "아니오",
                    detection.point_name,
                    measurement_group_label(detection.group_key),
                    detection.value_text,
                    detection.unit,
                    f"{detection.confidence:.2f}",
                    detection.status,
                    detection.raw_text,
                ),
            )
        if self.current_detection_index is not None and self.detection_tree.exists(str(self.current_detection_index)):
            self.detection_tree.selection_set(str(self.current_detection_index))

    def _on_image_select(self, _event: object = None) -> None:
        selection = self.image_tree.selection()
        if not selection:
            return
        self._show_result(int(selection[0]))

    def _show_result(self, index: int) -> None:
        self.current_result_index = index
        self.current_detection_index = None
        self._refresh_detection_tree()
        self._draw_preview()

    def _on_detection_select(self, _event: object = None) -> None:
        selection = self.detection_tree.selection()
        self.current_detection_index = int(selection[0]) if selection else None
        self._draw_preview()

    def _draw_preview(self) -> None:
        self.canvas.delete("all")
        result = self._current_result()
        if result is None or result.error:
            self.canvas.create_text(
                20,
                20,
                anchor="nw",
                fill="white",
                text=result.error if result else "이미지 없음",
            )
            return
        try:
            with Image.open(result.path) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
        except Exception as exc:
            self.canvas.create_text(20, 20, anchor="nw", fill="white", text=str(exc))
            return
        canvas_w = max(100, self.canvas.winfo_width())
        canvas_h = max(100, self.canvas.winfo_height())
        scale = min(canvas_w / image.width, canvas_h / image.height)
        draw_w = max(1, round(image.width * scale))
        draw_h = max(1, round(image.height * scale))
        preview = image.resize((draw_w, draw_h), Image.Resampling.LANCZOS)
        self._photo = ImageTk.PhotoImage(preview)
        offset_x = (canvas_w - draw_w) / 2
        offset_y = (canvas_h - draw_h) / 2
        self._preview_map = (scale, offset_x, offset_y)
        self.canvas.create_image(offset_x, offset_y, image=self._photo, anchor="nw")
        for index, detection in enumerate(result.detections):
            x1 = offset_x + detection.box.x * scale
            y1 = offset_y + detection.box.y * scale
            x2 = offset_x + detection.box.x2 * scale
            y2 = offset_y + detection.box.y2 * scale
            selected = index == self.current_detection_index
            color = "#2D8CFF" if selected else "#9B9B9B" if not detection.included else "#FF9D00" if detection.status != "정상" else "#27C26A"
            width = 3 if selected else 2
            self.canvas.create_rectangle(x1, y1, x2, y2, outline=color, width=width)
            self.canvas.create_text(
                x1 + 3,
                max(offset_y + 8, y1 - 7),
                text=(
                    f"{detection.order} {detection.point_name}"
                    if detection.point_name
                    else str(detection.order)
                ),
                anchor="sw",
                fill=color,
                font=("Arial", 10, "bold"),
            )

    def _toggle_image(self) -> None:
        result = self._current_result()
        if result is None:
            return
        result.selected = not result.selected
        self._refresh_image_tree()
        self.image_tree.selection_set(str(self.current_result_index))
        self._update_summary()

    def _toggle_detection(self) -> None:
        detection = self._current_detection()
        if detection is None:
            messagebox.showinfo("선택 필요", "측정값 한 행을 선택하세요.", parent=self.window)
            return
        detection.included = not detection.included
        self._apply_group_order(detection)
        self._refresh_detection_tree()
        self._refresh_image_tree()
        self._draw_preview()
        self._update_summary()

    def _edit_detection(self) -> None:
        detection = self._current_detection()
        if detection is None:
            messagebox.showinfo("선택 필요", "수정할 측정값을 선택하세요.", parent=self.window)
            return
        dialog = MeasurementEditDialog(
            self.window,
            title="측정값 수정",
            point_name=detection.point_name,
            value=detection.value_text,
            unit=detection.unit or "nm",
            allow_blank=True,
        )
        if dialog.result is None:
            return
        point_name, value, unit = dialog.result
        detection.point_name = point_name
        detection.value_text = value
        detection.unit = unit if value else ""
        detection.raw_text = detection.raw_text or "사용자 입력"
        detection.confidence = 1.0 if value else 0.0
        detection.source_mode = "manual_review"
        detection.review_required = not bool(value)
        detection.note = "사용자 확인/수정" if value else "사용자가 빈 값으로 유지"
        detection.included = True
        mark_duplicate_point_names(self._current_result())
        self._apply_group_order(detection)
        self._refresh_detection_tree()
        self._refresh_image_tree()
        self._draw_preview()
        self._update_summary()

    def _move_detection(self, direction: int) -> None:
        result = self._current_result()
        detection = self._current_detection()
        if result is None or detection is None:
            self.hint_var.set("이동할 측정값 한 행을 먼저 선택하세요.")
            return
        if not detection.included:
            self.hint_var.set("제외된 값입니다. [값 사용 전환] 후 순서를 바꿀 수 있습니다.")
            return
        if not move_detection_within_group(result, detection, direction):
            self.hint_var.set("이 그룹의 처음 또는 마지막 값입니다. 다른 그룹으로는 이동하지 않습니다.")
            return
        self.current_detection_index = result.detections.index(detection)
        self._refresh_detection_tree()
        self.detection_tree.see(str(self.current_detection_index))
        self._draw_preview()
        self._update_summary()
        self.hint_var.set("순서를 바꿨습니다. 다른 이미지·Excel에 적용하려면 [현재 순서를 기준으로 설정]")

    def _move_group(self, direction: int) -> None:
        result = self._current_result()
        detection = self._current_detection()
        if result is None or detection is None:
            return
        self.group_order = complete_group_order(self.group_order, result.detections)
        try:
            index = self.group_order.index(detection.group_key)
        except ValueError:
            return
        target = index + direction
        if not (0 <= target < len(self.group_order)):
            return
        self.group_order[index], self.group_order[target] = (
            self.group_order[target],
            self.group_order[index],
        )
        for item in self.results:
            order_result_by_groups(item, self.group_order, preserve_within_group=True)
        self.current_detection_index = result.detections.index(detection)
        self._refresh_detection_tree()
        self.detection_tree.selection_set(str(self.current_detection_index))
        self._draw_preview()
        self._update_summary()
        self.hint_var.set("그룹을 옮겼습니다. Excel에 적용하려면 [현재 순서를 기준으로 설정]")

    def _apply_group_order(
        self, selected_detection: MeasurementDetection | None = None
    ) -> None:
        for item in self.results:
            self.group_order = complete_group_order(self.group_order, item.detections)
        for item in self.results:
            order_result_by_groups(item, self.group_order, preserve_within_group=True)
        result = self._current_result()
        if result is not None and selected_detection in result.detections:
            self.current_detection_index = result.detections.index(selected_detection)

    def _renumber(self) -> None:
        result = self._current_result()
        if result is None:
            return
        for order, detection in enumerate(result.detections, 1):
            detection.order = order

    def _start_add(self) -> None:
        self._add_mode = True
        self.hint_var.set("추가할 측정값 위치의 중심을 미리보기에서 클릭하세요.")

    def _canvas_click(self, event: tk.Event) -> None:
        result = self._current_result()
        if result is None:
            return
        scale, offset_x, offset_y = self._preview_map
        image_x = (event.x - offset_x) / max(scale, 1e-9)
        image_y = (event.y - offset_y) / max(scale, 1e-9)
        if not (0 <= image_x < result.image_width and 0 <= image_y < result.image_height):
            return
        if self._add_mode:
            self._add_mode = False
            self.hint_var.set("상자를 선택하면 해당 위치가 파란색으로 표시됩니다.")
            dialog = MeasurementEditDialog(
                self.window,
                title="누락 측정값 추가",
                unit="nm",
                allow_blank=True,
            )
            if dialog.result is None:
                return
            point_name, value, unit = dialog.result
            box_w = max(40, round(result.image_width * 0.18))
            box_h = max(24, round(result.image_height * 0.08))
            box = Box(
                round(image_x - box_w / 2),
                round(image_y - box_h / 2),
                box_w,
                box_h,
            ).clipped(result.image_width, result.image_height)
            detection = MeasurementDetection(
                detection_id=f"M{len(result.detections) + 1}",
                box=box,
                image_width=result.image_width,
                image_height=result.image_height,
                order=len(result.detections) + 1,
                value_text=value,
                unit=unit if value else "",
                point_name=point_name,
                raw_text="사용자 추가",
                confidence=1.0 if value else 0.0,
                source_mode="manual_add",
                included=True,
                review_required=not bool(value),
                note="사용자가 누락 위치를 추가",
            )
            result.detections.append(detection)
            result.selected = True
            mark_duplicate_point_names(result)
            self._apply_group_order(detection)
            self._refresh_detection_tree()
            self.detection_tree.selection_set(str(self.current_detection_index))
            self._refresh_image_tree()
            self._draw_preview()
            self._update_summary()
            return

        containing = [
            (index, detection)
            for index, detection in enumerate(result.detections)
            if detection.box.x <= image_x <= detection.box.x2
            and detection.box.y <= image_y <= detection.box.y2
        ]
        if not containing:
            return
        index, _ = min(
            containing,
            key=lambda item: (item[1].box.cx - image_x) ** 2
            + (item[1].box.cy - image_y) ** 2,
        )
        self.current_detection_index = index
        self.detection_tree.selection_set(str(index))
        self.detection_tree.see(str(index))
        self._draw_preview()

    def _set_template(self) -> None:
        result = self._current_result()
        if result is None:
            return
        try:
            self.template = create_template_from_result(result, self.group_order)
            for other in self.results:
                order_result_by_template(other, self.template)
        except ValueError as exc:
            messagebox.showerror("기준 순서", str(exc), parent=self.window)
            return
        self._refresh_image_tree()
        self._show_result(self.current_result_index)
        messagebox.showinfo(
            "기준 순서 설정",
            f"{result.path.name}의 그룹 순서와 그룹 안의 값 순서를 적용했습니다.\n"
            "다른 이미지는 포인트명 또는 위치로 대응시킵니다.\n"
            "Excel 열에도 이 순서가 적용됩니다. 새 값은 해당 그룹 뒤에 추가됩니다.",
            parent=self.window,
        )
        self.hint_var.set("기준 순서를 적용했습니다. [검토 완료] 후 [기준 순서 저장]으로 다시 사용할 수 있습니다.")

    def _update_summary(self) -> None:
        selected = [result for result in self.results if result.selected]
        values = [d for result in selected for d in result.detections if d.included]
        unresolved = sum(d.status != "정상" for d in values)
        group_text = " → ".join(
            measurement_group_label(key) for key in self.group_order
        )
        self.summary_var.set(
            f"선택 이미지 {len(selected)}장 · 사용 값 {len(values)}개 · "
            f"확인 필요 {unresolved}개 · 그룹 {group_text or '없음'}"
        )

    def _accept(self) -> None:
        if not any(result.selected for result in self.results):
            messagebox.showerror("검토 확인", "사용할 이미지를 한 장 이상 선택하세요.", parent=self.window)
            return
        self.accepted = True
        self.window.destroy()

    def _cancel(self) -> None:
        self.accepted = False
        self.window.destroy()


class MeasurementAutomatorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.paths: list[Path] = []
        self.results: list[ImageScanResult] = []
        self.template: MeasurementTemplate | None = None
        self.events: queue.Queue[tuple[object, ...]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None

        root.title(f"디스플레이 측정값 자동 정리 v{APP_VERSION}")
        root.geometry("980x680")
        root.minsize(820, 580)
        self._build_ui()

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, padding=14)
        header.pack(fill="x")
        ttk.Label(
            header,
            text="FIB · SEM 측정값 자동 검출 → 검토 → Excel",
            font=("맑은 고딕", 16, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            header,
            text="고정 ROI 없이 이미지 전체의 측정선과 수치 위치를 찾습니다. 모든 처리는 PC 내부에서 수행됩니다.",
            foreground="#555555",
        ).pack(anchor="w", pady=(4, 0))

        actions = ttk.Frame(self.root, padding=(14, 0, 14, 8))
        actions.pack(fill="x")
        ttk.Button(actions, text="이미지 파일 추가", command=self._add_files).pack(side="left")
        ttk.Button(actions, text="폴더 추가", command=self._add_folder).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="목록 비우기", command=self._clear).pack(side="left", padx=(6, 0))
        ttk.Separator(actions, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(actions, text="기준 순서 불러오기", command=self._load_template).pack(side="left")
        self.save_template_button = ttk.Button(
            actions, text="기준 순서 저장", command=self._save_template, state="disabled"
        )
        self.save_template_button.pack(side="left", padx=(6, 0))
        self.deep_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            actions,
            text="더 작은 글자 정밀 검사",
            variable=self.deep_var,
        ).pack(side="right")

        list_frame = ttk.LabelFrame(self.root, text="검사 대상 이미지", padding=8)
        list_frame.pack(fill="both", expand=True, padx=14)
        columns = ("sample", "seq", "file", "path")
        self.path_tree = ttk.Treeview(list_frame, columns=columns, show="headings")
        for column, heading, width in [
            ("sample", "시료", 100),
            ("seq", "이미지 순번", 90),
            ("file", "파일명", 230),
            ("path", "경로", 430),
        ]:
            self.path_tree.heading(column, text=heading)
            self.path_tree.column(column, width=width, stretch=column in {"file", "path"})
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.path_tree.yview)
        self.path_tree.configure(yscrollcommand=scroll.set)
        self.path_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        progress_frame = ttk.Frame(self.root, padding=(14, 10))
        progress_frame.pack(fill="x")
        self.status_var = tk.StringVar(value="이미지를 추가한 뒤 '전체 검사'를 누르세요.")
        ttk.Label(progress_frame, textvariable=self.status_var).pack(anchor="w")
        self.progress = ttk.Progressbar(progress_frame, mode="determinate")
        self.progress.pack(fill="x", pady=(5, 0))

        bottom = ttk.Frame(self.root, padding=(14, 0, 14, 14))
        bottom.pack(fill="x")
        self.detailed_excel_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bottom, text="상세 Excel", variable=self.detailed_excel_var).pack(side="left", padx=(0, 8))
        self.scan_button = ttk.Button(bottom, text="전체 검사", command=self._start_scan)
        self.scan_button.pack(side="left")
        self.cancel_button = ttk.Button(bottom, text="검사 취소", command=self._cancel_scan, state="disabled")
        self.cancel_button.pack(side="left", padx=(6, 0))
        self.review_button = ttk.Button(bottom, text="후보·값 검토", command=self._review, state="disabled")
        self.review_button.pack(side="right")
        self.export_button = ttk.Button(bottom, text="Excel 저장", command=self._export, state="disabled")
        self.export_button.pack(side="right", padx=(0, 6))

    def _add_paths(self, paths: list[Path]) -> None:
        known = {os.path.normcase(str(path.resolve())) for path in self.paths}
        for path in paths:
            resolved = path.resolve()
            key = os.path.normcase(str(resolved))
            if resolved.suffix.lower() in SUPPORTED_SUFFIXES and key not in known:
                self.paths.append(resolved)
                known.add(key)
        self._refresh_paths()

    def _add_files(self) -> None:
        selected = filedialog.askopenfilenames(
            parent=self.root,
            title="분석 이미지 선택",
            filetypes=[("지원 이미지", "*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.gif *.webp"), ("모든 파일", "*.*")],
        )
        self._add_paths([Path(path) for path in selected])

    def _add_folder(self) -> None:
        selected = filedialog.askdirectory(parent=self.root, title="이미지 폴더 선택")
        if not selected:
            return
        folder = Path(selected)
        paths = [path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES]
        self._add_paths(paths)

    def _refresh_paths(self) -> None:
        identities = infer_identities(self.paths)
        self.paths = [identity.path for identity in identities]
        for item in self.path_tree.get_children():
            self.path_tree.delete(item)
        for index, identity in enumerate(identities):
            sample = identity.sample_id + ("*" if identity.inferred else "")
            self.path_tree.insert(
                "", "end", iid=str(index), values=(sample, identity.image_index, identity.path.name, str(identity.path.parent))
            )
        self.status_var.set(f"검사 대상 {len(self.paths)}장" if self.paths else "이미지를 추가하세요.")

    def _clear(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.paths.clear()
        self.results.clear()
        self._refresh_paths()
        self.review_button.configure(state="disabled")
        self.export_button.configure(state="disabled")

    def _start_scan(self) -> None:
        if not self.paths:
            messagebox.showinfo("검사 대상", "이미지 파일이나 폴더를 먼저 추가하세요.", parent=self.root)
            return
        if self.worker and self.worker.is_alive():
            return
        self.cancel_event.clear()
        self.progress.configure(maximum=len(self.paths), value=0)
        self.scan_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.review_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
        # Tk variables must be read on the UI thread, before the worker starts.
        deep_verify = bool(self.deep_var.get())

        def progress(index: int, total: int, path: Path) -> None:
            self.events.put(("progress", index, total, path.name))

        def run() -> None:
            try:
                results = scan_images(
                    self.paths,
                    deep_verify=deep_verify,
                    progress=progress,
                    cancelled=self.cancel_event.is_set,
                )
                self.events.put(("done", results))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()
        self.root.after(100, self._poll_events)

    def _poll_events(self) -> None:
        active = True
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == "progress":
                _, index, total, name = event
                self.progress.configure(value=index)
                self.status_var.set(f"{index}/{total} 검사 중: {name}")
            elif kind == "done":
                self.results = event[1]  # type: ignore[assignment]
                if self.template is not None:
                    for result in self.results:
                        order_result_by_template(result, self.template)
                failed = sum(bool(result.error) for result in self.results)
                candidates = sum(result.selected for result in self.results)
                values = sum(len(result.detections) for result in self.results)
                self.status_var.set(
                    f"검사 완료 · 후보 이미지 {candidates}장 · 위치 {values}개 · 실패 {failed}장"
                )
                self.scan_button.configure(state="normal")
                self.cancel_button.configure(state="disabled")
                self.review_button.configure(state="normal")
                self.export_button.configure(state="normal")
                active = False
            elif kind == "error":
                messagebox.showerror("검사 실패", str(event[1]), parent=self.root)
                self.status_var.set("검사가 중단되었습니다.")
                self.scan_button.configure(state="normal")
                self.cancel_button.configure(state="disabled")
                active = False
        if active and self.worker and self.worker.is_alive():
            self.root.after(100, self._poll_events)

    def _cancel_scan(self) -> None:
        self.cancel_event.set()
        self.status_var.set("현재 이미지 처리가 끝나면 취소됩니다…")

    def _review(self) -> None:
        if not self.results:
            return
        dialog = ReviewDialog(self.root, self.results, self.template)
        if not dialog.accepted:
            return
        self.results = dialog.results
        self.template = dialog.template
        if self.template:
            self.save_template_button.configure(state="normal")
        unresolved = sum(
            detection.status != "정상"
            for result in self.results
            if result.selected
            for detection in result.included_detections
        )
        self.status_var.set(f"검토 반영 완료 · 남은 확인 필요 {unresolved}개")

    def _load_template(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="기준 순서 템플릿 불러오기",
            filetypes=[("JSON 템플릿", "*.json")],
        )
        if not selected:
            return
        try:
            self.template = load_template(selected)
            for result in self.results:
                order_result_by_template(result, self.template)
        except Exception as exc:
            messagebox.showerror("템플릿 오류", str(exc), parent=self.root)
            return
        self.save_template_button.configure(state="normal")
        self.status_var.set(f"기준 순서 불러옴: {Path(selected).name}")

    def _save_template(self) -> None:
        if self.template is None:
            return
        selected = filedialog.asksaveasfilename(
            parent=self.root,
            title="기준 순서 템플릿 저장",
            defaultextension=".json",
            filetypes=[("JSON 템플릿", "*.json")],
            initialfile="measurement_order_template.json",
        )
        if not selected:
            return
        try:
            save_template(self.template, selected)
        except Exception as exc:
            messagebox.showerror("저장 실패", str(exc), parent=self.root)
            return
        self.status_var.set(f"기준 순서 저장 완료: {Path(selected).name}")

    def _export(self) -> None:
        if not self.results:
            return
        unresolved = sum(
            detection.status != "정상"
            for result in self.results
            if result.selected
            for detection in result.included_detections
        )
        if unresolved:
            proceed = messagebox.askyesno(
                "확인 필요 값 존재",
                f"아직 확인 필요 값이 {unresolved}개 있습니다.\n"
                "Excel에는 빈 셀 또는 노란색 셀로 표시됩니다. 그대로 저장할까요?",
                parent=self.root,
            )
            if not proceed:
                self._review()
                return
        selected = filedialog.asksaveasfilename(
            parent=self.root,
            title="측정값 Excel 저장",
            defaultextension=".xlsx",
            filetypes=[("Excel 통합문서", "*.xlsx")],
            initialfile="display_measurement_results.xlsx",
        )
        if not selected:
            return
        try:
            summary = export_excel(
                self.results,
                selected,
                template=self.template,
                overwrite=True,
                compact=not self.detailed_excel_var.get(),
            )
        except Exception as exc:
            messagebox.showerror("Excel 저장 실패", str(exc), parent=self.root)
            return
        self.status_var.set(
            f"Excel 저장 완료 · 이미지 {summary.selected_images}장 · 측정값 {summary.measurement_count}개"
        )
        if messagebox.askyesno(
            "저장 완료",
            f"Excel 파일을 저장했습니다.\n\n{summary.output_path}\n\n지금 폴더를 열까요?",
            parent=self.root,
        ):
            open_path(summary.output_path.parent)


def runtime_smoke_test() -> bool:
    """Exercise local OCR, custom order, reference round-trip and Excel export."""

    import cv2
    import numpy as np

    image = np.full((120, 420, 3), 70, dtype=np.uint8)
    cv2.putText(
        image,
        "2.35 um",
        (35, 75),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.6,
        (20, 255, 20),
        3,
        cv2.LINE_AA,
    )
    engine = RapidOCREngine()
    attempts = engine.recognize_region(
        image, Box(0, 0, image.shape[1], image.shape[0]), "internal_horizontal"
    )
    if not any(
        (attempt.value_text, attempt.unit) == ("2.35", "µm")
        for attempt in attempts
    ):
        return False
    import tempfile
    from PIL import ImageDraw, ImageFont
    from measurement_ocr_v2 import ImageIdentity, scan_image, verify_xlsx

    image = Image.new("RGB", (640, 400), (150, 150, 150))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=24)
    draw.text((0, 40), "15.2 deg", font=font, fill=(245, 245, 245))
    text = "28.4 deg"
    width = draw.textbbox((0, 0), text, font=font)[2]
    draw.text((640 - width, 130), text, font=font, fill=(30, 240, 240))
    draw.text((245, 265), "167.5 nm", font=ImageFont.load_default(size=16), fill=(178, 178, 178))
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "self_test.png"
        image.save(path)
        result = scan_image(ImageIdentity(path, "self-test", 1), engine)
        values = {(d.value_text, d.unit) for d in result.included_detections}
        if result.error or values != {("15.2", "°"), ("28.4", "°"), ("167.5", "nm")}:
            return False
        order_result_by_groups(result, ["angle", "length"])
        angle = next(d for d in result.detections if d.value_text == "28.4")
        if not move_detection_within_group(result, angle, -1):
            return False
        template = create_template_from_result(result, ["angle", "length"])
        template_path = Path(directory) / "order.json"
        save_template(template, template_path)
        template = load_template(template_path)
        restored = copy.deepcopy(result)
        order_result_by_groups(restored, ["length", "angle"])
        order_result_by_template(restored, template)
        if [d.value_text for d in restored.included_detections] != ["28.4", "15.2", "167.5"]:
            return False
        from measurement_ocr_v2 import assign_export_columns
        columns = assign_export_columns([restored], template)
        if [d.column_key for d in restored.included_detections] != [c.key for c in columns]:
            return False
        output = Path(directory) / "self_test.xlsx"
        export_excel([restored], output, template=template)
        verify_xlsx(output)
    return True


def main() -> int:
    if "--smoke-test" in sys.argv:
        return 0 if runtime_smoke_test() else 1

    root = tk.Tk()
    try:
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except tk.TclError:
        pass
    MeasurementAutomatorApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
