from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
import unicodedata
import zipfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree

# ONNX Runtime enables telemetry in some official builds.  This variable must be
# set before onnxruntime is imported by RapidOCR.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

import cv2
import numpy as np
from PIL import Image, ImageOps

APP_VERSION = "2.1.0"
SUPPORTED_SUFFIXES = {
    ".bmp",
    ".dib",
    ".gif",
    ".jfif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}
MEASUREMENT_RE = re.compile(
    r"(?<![\d.])(?P<value>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>n\s*m|[uµμ]\s*m|°|d\s*e\s*g)(?![a-z])",
    re.IGNORECASE,
)
POINT_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
POINT_BEFORE_VALUE_RE = re.compile(
    r"(?P<point>[A-Za-z][A-Za-z0-9_-]{0,63})"
    r"(?:(?:\s*[=:]\s*)|(?:\s*\(\s*)|\s+)$"
)
NATURAL_PART_RE = re.compile(r"(\d+)")
LEADING_SAMPLE_RE = re.compile(
    r"^((?:sample|specimen|시료|샘플|no)?\s*\d+)", re.IGNORECASE
)


class MeasurementError(RuntimeError):
    pass


class OCRUnavailableError(MeasurementError):
    pass


class BatchCancelled(MeasurementError):
    pass


@dataclass
class Box:
    x: int
    y: int
    w: int
    h: int
    area: int = 0
    label: int = 0

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.x2, self.y2

    def clipped(self, width: int, height: int) -> Box:
        x1 = max(0, min(width, self.x))
        y1 = max(0, min(height, self.y))
        x2 = max(x1, min(width, self.x2))
        y2 = max(y1, min(height, self.y2))
        return Box(x1, y1, x2 - x1, y2 - y1, self.area, self.label)


@dataclass
class ParsedMeasurement:
    point_name: str
    value_text: str
    unit: str
    item_index: int = 0


@dataclass
class OCRAttempt:
    text: str
    confidence: float
    mode: str
    value_text: str = ""
    unit: str = ""
    point_name: str = ""
    item_index: int = 0

    @property
    def valid(self) -> bool:
        return bool(self.value_text and self.unit)


@dataclass
class MeasurementDetection:
    detection_id: str
    box: Box
    image_width: int
    image_height: int
    order: int = 0
    value_text: str = ""
    unit: str = ""
    point_name: str = ""
    raw_text: str = ""
    confidence: float = 0.0
    source_mode: str = ""
    orientation: str = "horizontal"
    included: bool = True
    review_required: bool = False
    note: str = ""
    column_key: str = ""
    alternatives: list[OCRAttempt] = field(default_factory=list)

    @property
    def numeric_value(self) -> float | None:
        if not self.value_text:
            return None
        try:
            return float(Decimal(self.value_text))
        except (InvalidOperation, ValueError):
            return None

    @property
    def center_normalized(self) -> tuple[float, float]:
        if self.image_width <= 0 or self.image_height <= 0:
            return 0.0, 0.0
        return self.box.cx / self.image_width, self.box.cy / self.image_height

    @property
    def group_key(self) -> str:
        return measurement_group(self.unit)

    @property
    def status(self) -> str:
        if not self.included:
            return "제외"
        if self.numeric_value is None or not self.unit:
            return "값 확인 필요"
        if self.review_required:
            return "확인 필요"
        return "정상"


@dataclass
class ImageIdentity:
    path: Path
    sample_id: str
    image_index: int
    inferred: bool = False


@dataclass
class ImageScanResult:
    path: Path
    sample_id: str
    image_index: int
    image_width: int
    image_height: int
    detections: list[MeasurementDetection] = field(default_factory=list)
    selected: bool = True
    elapsed_seconds: float = 0.0
    color_threshold: int | None = None
    used_full_fallback: bool = False
    identity_inferred: bool = False
    error: str = ""

    @property
    def included_detections(self) -> list[MeasurementDetection]:
        return sorted(
            (item for item in self.detections if item.included),
            key=lambda item: (item.order, item.box.y, item.box.x),
        )

    @property
    def status(self) -> str:
        if self.error:
            return "실패"
        included = self.included_detections
        if not included:
            return "측정값 없음"
        if any(item.status != "정상" for item in included):
            return "확인 필요"
        return "정상"


@dataclass
class TemplatePoint:
    slot_id: str
    order: int
    x_norm: float
    y_norm: float
    expected_unit: str = ""
    label: str = ""
    point_name: str = ""
    group_key: str = ""


@dataclass
class MeasurementTemplate:
    reference_name: str
    reference_width: int
    reference_height: int
    points: list[TemplatePoint]
    group_order: list[str] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    schema_version: int = 3

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "reference_name": self.reference_name,
            "reference_width": self.reference_width,
            "reference_height": self.reference_height,
            "group_order": self.group_order,
            "created_at": self.created_at,
            "points": [
                {
                    "slot_id": point.slot_id,
                    "order": point.order,
                    "x_norm": point.x_norm,
                    "y_norm": point.y_norm,
                    "expected_unit": point.expected_unit,
                    "label": point.label,
                    "point_name": point.point_name,
                    "group_key": point.group_key,
                }
                for point in self.points
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> MeasurementTemplate:
        points_raw = data.get("points")
        if not isinstance(points_raw, list):
            raise ValueError("템플릿의 points 항목이 올바르지 않습니다.")
        points: list[TemplatePoint] = []
        for index, item in enumerate(points_raw, 1):
            if not isinstance(item, dict):
                raise ValueError("템플릿 측정위치 형식이 올바르지 않습니다.")
            x_norm = float(item.get("x_norm", -1))
            y_norm = float(item.get("y_norm", -1))
            if not (0 <= x_norm <= 1 and 0 <= y_norm <= 1):
                raise ValueError("템플릿 상대좌표가 0~1 범위를 벗어났습니다.")
            points.append(
                TemplatePoint(
                    str(item.get("slot_id") or f"T{index}"),
                    int(item.get("order") or index),
                    x_norm,
                    y_norm,
                    normalize_unit(str(item.get("expected_unit") or "")),
                    str(item.get("label") or f"측정값 {index}"),
                    normalize_point_name(str(item.get("point_name") or "")),
                    str(item.get("group_key") or "")
                    or measurement_group(str(item.get("expected_unit") or "")),
                )
            )
        points.sort(key=lambda point: point.order)
        raw_group_order = data.get("group_order") or []
        if not isinstance(raw_group_order, list):
            raw_group_order = []
        group_order = [
            str(item)
            for item in raw_group_order
            if isinstance(item, str) and item
        ]
        for point in points:
            if point.group_key and point.group_key not in group_order:
                group_order.append(point.group_key)
        return cls(
            reference_name=str(data.get("reference_name") or ""),
            reference_width=int(data.get("reference_width") or 0),
            reference_height=int(data.get("reference_height") or 0),
            points=points,
            group_order=group_order,
            created_at=str(data.get("created_at") or ""),
            schema_version=int(data.get("schema_version") or 2),
        )


@dataclass
class ExportColumn:
    key: str
    display_order: int
    x_norm: float
    y_norm: float
    label: str
    expected_unit: str = ""
    point_name: str = ""
    group_key: str = ""
    points: list[tuple[float, float]] = field(default_factory=list)

    @property
    def spread(self) -> float:
        if not self.points:
            return 0.0
        return max(math.dist((self.x_norm, self.y_norm), point) for point in self.points)


@dataclass
class ExportSummary:
    output_path: Path
    selected_images: int
    measurement_count: int
    review_count: int
    column_count: int
    sha256: str


def normalize_unit(unit: str) -> str:
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", unit)).lower()
    if compact == "nm":
        return "nm"
    if compact in {"um", "μm", "µm"}:
        return "µm"
    if compact in {"°", "deg"}:
        return "°"
    return ""


def measurement_group(unit: str) -> str:
    normalized = normalize_unit(unit)
    if normalized in {"nm", "µm"}:
        return "length"
    if normalized == "°":
        return "angle"
    return "unknown"


def measurement_group_label(group_key: str) -> str:
    return {"length": "높이", "angle": "각도", "unknown": "기타"}.get(
        group_key, "기타"
    )


def normalize_point_name(point_name: str) -> str:
    normalized = unicodedata.normalize("NFKC", point_name).strip()
    return normalized if POINT_NAME_RE.fullmatch(normalized) else ""


def point_name_key(point_name: str) -> str:
    return normalize_point_name(point_name).casefold()


def derive_group_order(
    detections: Sequence[MeasurementDetection],
) -> list[str]:
    output: list[str] = []
    for detection in sorted(detections, key=lambda item: (item.box.y, item.box.x)):
        if not detection.included:
            continue
        key = detection.group_key
        if key not in output:
            output.append(key)
    return output


def complete_group_order(
    group_order: Sequence[str], detections: Sequence[MeasurementDetection]
) -> list[str]:
    output: list[str] = []
    for key in group_order:
        if key and key not in output:
            output.append(key)
    for key in derive_group_order(detections):
        if key not in output:
            output.append(key)
    return output


def order_result_by_groups(
    result: ImageScanResult, group_order: Sequence[str]
) -> list[str]:
    resolved = complete_group_order(group_order, result.detections)
    rank = {key: index for index, key in enumerate(resolved)}
    included = sorted(
        (item for item in result.detections if item.included),
        key=lambda item: (
            rank.get(item.group_key, len(rank)),
            item.box.y,
            item.box.x,
            point_name_key(item.point_name),
        ),
    )
    excluded = sorted(
        (item for item in result.detections if not item.included),
        key=lambda item: (item.box.y, item.box.x),
    )
    result.detections[:] = included + excluded
    for index, detection in enumerate(result.detections, 1):
        detection.order = index
    return resolved


def _append_note(detection: MeasurementDetection, note: str) -> None:
    notes = [item.strip() for item in detection.note.split(";") if item.strip()]
    if note not in notes:
        notes.append(note)
    detection.note = "; ".join(notes)


def mark_duplicate_point_names(result: ImageScanResult) -> None:
    duplicate_note = "동일 이미지 내 포인트명 중복"
    for detection in result.detections:
        had_duplicate_warning = duplicate_note in detection.note
        notes = [
            item.strip()
            for item in detection.note.split(";")
            if item.strip() and item.strip() != duplicate_note
        ]
        detection.note = "; ".join(notes)
        if had_duplicate_warning:
            other_review_reason = (
                detection.numeric_value is None
                or not detection.unit
                or detection.confidence < 0.75
                or any(
                    marker in detection.note
                    for marker in ("불일치", "확정하지 못함", "하단 스케일바")
                )
            )
            detection.review_required = other_review_reason

    by_name: dict[str, list[MeasurementDetection]] = {}
    for detection in result.detections:
        key = point_name_key(detection.point_name)
        if detection.included and key:
            by_name.setdefault(key, []).append(detection)
    for detections in by_name.values():
        if len(detections) < 2:
            continue
        for detection in detections:
            detection.review_required = True
            _append_note(detection, duplicate_note)


def canonical_decimal(value: str) -> str:
    normalized = value.replace(",", ".")
    decimal_value = Decimal(normalized)
    if not decimal_value.is_finite() or decimal_value < 0:
        raise ValueError("측정값은 0 이상의 유한 숫자여야 합니다.")
    # Decimal keeps the number of displayed fractional digits.  Measurement
    # precision is meaningful, so 428.0 and 1.2300 must not become 428/1.23.
    return format(decimal_value, "f")


def decimal_places(value: str) -> int:
    normalized = value.replace(",", ".")
    return len(normalized.rsplit(".", 1)[1]) if "." in normalized else 0


def parse_measurement_items(text: str) -> list[ParsedMeasurement]:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.translate(str.maketrans({"，": ".", "．": ".", "·": "."}))
    parsed: list[ParsedMeasurement] = []
    previous_end = 0
    for match in MEASUREMENT_RE.finditer(normalized):
        prefix = normalized[previous_end : match.start()]
        point_match = POINT_BEFORE_VALUE_RE.search(prefix)
        point_name = (
            normalize_point_name(point_match.group("point")) if point_match else ""
        )
        # A bare digit group separated by whitespace usually means OCR lost a
        # decimal point ("2 35 µm").  A valid alphanumeric point such as H1 is
        # allowed because it is explicitly captured above.
        if not point_name and re.search(r"\d\s+$", prefix):
            previous_end = match.end()
            continue
        try:
            value = canonical_decimal(match.group("value"))
        except (InvalidOperation, ValueError):
            previous_end = match.end()
            continue
        unit = normalize_unit(match.group("unit"))
        if unit:
            parsed.append(
                ParsedMeasurement(point_name, value, unit, item_index=len(parsed))
            )
        previous_end = match.end()
    return parsed


def parse_measurement_text(text: str) -> tuple[str, str] | None:
    parsed = parse_measurement_items(text)
    if not parsed:
        return None
    return parsed[0].value_text, parsed[0].unit


def validate_point_name(point_name: str) -> str:
    if not point_name.strip():
        return ""
    normalized = normalize_point_name(point_name)
    if not normalized:
        raise ValueError(
            "포인트명은 영문으로 시작하고 영문·숫자·하이픈·언더바만 사용하세요."
        )
    return normalized


def validate_manual_measurement(value: str, unit: str) -> tuple[str, str]:
    try:
        normalized_value = canonical_decimal(value.strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("수치값은 0 이상의 숫자로 입력하세요.") from exc
    normalized_unit = normalize_unit(unit)
    if not normalized_unit:
        raise ValueError("단위는 nm, µm(um) 또는 °(deg)만 사용할 수 있습니다.")
    return normalized_value, normalized_unit


def _natural_sort_key(text: str) -> tuple[tuple[int, object], ...]:
    normalized = unicodedata.normalize("NFKC", text).strip().casefold()
    key: list[tuple[int, object]] = []
    for part in NATURAL_PART_RE.split(normalized):
        if not part:
            continue
        key.append((0, int(part)) if part.isdigit() else (1, part))
    key.append((2, normalized))
    return tuple(key)


def split_sample_and_sequence(filename_or_stem: str) -> tuple[str, str]:
    stem = unicodedata.normalize("NFKC", Path(filename_or_stem).stem).strip()
    if not stem:
        raise ValueError("빈 파일명")
    segments: list[tuple[int, int]] = []
    start: int | None = None
    for index, character in enumerate(stem):
        if character.isalnum():
            start = index if start is None else start
        elif start is not None:
            segments.append((start, index))
            start = None
    if start is not None:
        segments.append((start, len(stem)))
    if len(segments) >= 2:
        first_start, first_end = segments[0]
        first = stem[first_start:first_end]
        if any(character.isdigit() for character in first):
            return first, stem[segments[1][0] :]
        if len(segments) >= 3:
            second_start, second_end = segments[1]
            second = stem[second_start:second_end]
            if any(character.isdigit() for character in second):
                return stem[:second_end], stem[segments[2][0] :]
    match = LEADING_SAMPLE_RE.match(stem)
    if match:
        return match.group(1), stem[match.end() :]
    raise ValueError("파일명에서 시료와 이미지 순번을 구분할 수 없습니다.")


def infer_identities(paths: Iterable[str | os.PathLike[str]]) -> list[ImageIdentity]:
    unique: dict[str, Path] = {}
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if path.suffix.lower() in SUPPORTED_SUFFIXES:
            unique.setdefault(os.path.normcase(str(path)), path)
    ordered = sorted(unique.values(), key=lambda path: _natural_sort_key(path.name))
    identities: list[ImageIdentity] = []
    for fallback_index, path in enumerate(ordered, 1):
        try:
            sample_id, remainder = split_sample_and_sequence(path.name)
            matches = re.findall(r"\d+", remainder)
            if not matches:
                raise ValueError("이미지 순번 없음")
            identities.append(ImageIdentity(path, sample_id, int(matches[0]), False))
        except ValueError:
            identities.append(
                ImageIdentity(path, path.parent.name or "미분류", fallback_index, True)
            )
    identities.sort(
        key=lambda item: (
            _natural_sort_key(item.sample_id),
            item.image_index,
            _natural_sort_key(item.path.name),
        )
    )
    return identities


def supported_image_paths(paths: Iterable[str | os.PathLike[str]]) -> list[Path]:
    return [item.path for item in infer_identities(paths)]


def file_sha256(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _components(mask: np.ndarray, min_area: int = 3) -> list[Box]:
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    result: list[Box] = []
    for label in range(1, count):
        x, y, w, h, area = map(int, stats[label])
        if area >= min_area:
            result.append(Box(x, y, w, h, area, label))
    return result


def _color_layers(
    rgb: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    rgb16 = rgb.astype(np.int16)
    chroma = rgb16.max(axis=2) - rgb16.min(axis=2)
    noise = float(np.percentile(chroma, 97))
    threshold = int(min(24, max(8, round(noise + 5))))
    base = np.where(chroma >= threshold, 255, 0).astype(np.uint8)
    height, width = base.shape
    run = max(25, round(min(height, width) * 0.055))
    horizontal = cv2.morphologyEx(
        base, cv2.MORPH_OPEN, np.ones((1, run), np.uint8)
    )
    vertical = cv2.morphologyEx(
        base, cv2.MORPH_OPEN, np.ones((run, 1), np.uint8)
    )
    lines = cv2.bitwise_or(horizontal, vertical)
    widened = cv2.dilate(lines, np.ones((3, 3), np.uint8))
    text = cv2.bitwise_and(base, cv2.bitwise_not(widened))
    return base, lines, text, threshold


def _interval_gap(a0: float, a1: float, b0: float, b1: float) -> float:
    if a1 < b0:
        return b0 - a1
    if b1 < a0:
        return a0 - b1
    return 0.0


def detect_colored_measurement_regions(
    rgb: np.ndarray,
) -> tuple[list[tuple[Box, str]], int]:
    height, width = rgb.shape[:2]
    base, lines, _, threshold = _color_layers(rgb)
    base_components = _components(base, 12)
    anchors: list[Box] = []
    minimum_length = max(42, int(min(height, width) * 0.075))
    for box in base_components:
        line_pixels = int(np.count_nonzero(lines[box.y : box.y2, box.x : box.x2]))
        if max(box.w, box.h) >= minimum_length and line_pixels >= 20:
            anchors.append(box)
    anchor_labels = {box.label for box in anchors}
    words = [
        box
        for box in base_components
        if box.label not in anchor_labels
        and box.area >= 20
        and box.w >= 6
        and box.h >= 4
        and box.w <= width * 0.35
        and box.h <= height * 0.35
    ]

    possible_pairs: list[tuple[float, int, int]] = []
    for anchor_index, anchor in enumerate(anchors):
        vertical_line = anchor.h > anchor.w * 1.35
        for word_index, word in enumerate(words):
            if vertical_line:
                along = _interval_gap(anchor.y, anchor.y2, word.y, word.y2)
                across = _interval_gap(anchor.x, anchor.x2, word.x, word.x2)
                limit = max(80.0, width * 0.13)
                center_distance = abs(word.cy - anchor.cy)
            else:
                along = _interval_gap(anchor.x, anchor.x2, word.x, word.x2)
                across = _interval_gap(anchor.y, anchor.y2, word.y, word.y2)
                limit = max(60.0, height * 0.13)
                center_distance = abs(word.cx - anchor.cx)
            if along > max(12.0, max(anchor.w, anchor.h) * 0.12) or across > limit:
                continue
            size_bonus = min(word.area, 500) * 0.015
            overlap_bonus = 20.0 if across == 0 else 0.0
            score = across + center_distance * 0.25 - size_bonus - overlap_bonus
            possible_pairs.append((score, anchor_index, word_index))

    assignments: dict[int, Box] = {}
    used_words: set[int] = set()
    for _, anchor_index, word_index in sorted(possible_pairs):
        if anchor_index in assignments or word_index in used_words:
            continue
        assignments[anchor_index] = words[word_index]
        used_words.add(word_index)

    regions: list[tuple[Box, str]] = []
    for anchor_index, anchor in enumerate(anchors):
        word = assignments.get(anchor_index)
        if word is not None:
            pad_x, pad_y = 12, 7
            region = Box(
                max(0, word.x - pad_x),
                max(0, word.y - pad_y),
                word.w + pad_x * 2,
                word.h + pad_y * 2,
                word.area,
            ).clipped(width, height)
            orientation = (
                "external_vertical"
                if word.h > word.w * 1.35
                else "external_horizontal"
            )
        else:
            pad = 7
            region = Box(
                anchor.x - pad,
                anchor.y - pad,
                anchor.w + pad * 2,
                anchor.h + pad * 2,
                anchor.area,
            ).clipped(width, height)
            orientation = (
                "internal_vertical"
                if anchor.h > anchor.w * 1.35
                else "internal_horizontal"
            )
        regions.append((region, orientation))

    deduped: list[tuple[Box, str]] = []
    for region, orientation in sorted(regions, key=lambda item: (item[0].y, item[0].x)):
        duplicate = False
        for previous, _ in deduped:
            inter_w = max(0, min(region.x2, previous.x2) - max(region.x, previous.x))
            inter_h = max(0, min(region.y2, previous.y2) - max(region.y, previous.y))
            overlap = inter_w * inter_h
            smaller = min(region.w * region.h, previous.w * previous.h)
            if smaller and overlap / smaller >= 0.65:
                duplicate = True
                break
        if not duplicate:
            deduped.append((region, orientation))
    return deduped, threshold


def _chroma_projection(crop: np.ndarray, low: int = 4) -> np.ndarray:
    array = crop.astype(np.float32)
    score = array.max(axis=2) - array.min(axis=2)
    positive = score[score > low]
    high = float(np.percentile(positive, 98)) if positive.size else float(low + 8)
    high = max(high, low + 8)
    gray = 255 - np.clip((score - low) * 255 / (high - low), 0, 255)
    return cv2.cvtColor(gray.astype(np.uint8), cv2.COLOR_GRAY2BGR)


class RapidOCREngine:
    """In-process local OCR.  No image or OCR text is sent over the network."""

    def __init__(self) -> None:
        os.environ["ORT_DISABLE_TELEMETRY"] = "1"
        try:
            import rapidocr
            from rapidocr import RapidOCR
        except Exception as exc:  # pragma: no cover - environment dependent
            raise OCRUnavailableError(
                "RapidOCR가 설치되지 않았습니다. START_HERE.py를 실행해 설치하세요."
            ) from exc

        model_root = Path(rapidocr.__file__).resolve().parent / "models"
        model_paths = {
            "Det.model_path": model_root / "PP-OCRv6_det_small.onnx",
            "Cls.model_path": model_root / "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
            "Rec.model_path": model_root / "PP-OCRv6_rec_small.onnx",
        }
        missing = [path.name for path in model_paths.values() if not path.is_file()]
        if missing:
            raise OCRUnavailableError(
                "로컬 OCR 모델이 누락되었습니다. 네트워크 자동 다운로드는 실행하지 "
                f"않습니다: {', '.join(missing)}"
            )
        params: dict[str, object] = {
            "Global.log_level": "error",
            "Global.model_root_dir": str(model_root),
        }
        params.update({key: str(value) for key, value in model_paths.items()})
        try:
            self._engine = RapidOCR(params=params)
        except Exception as exc:  # pragma: no cover - environment dependent
            raise OCRUnavailableError(f"RapidOCR 초기화 실패: {exc}") from exc

    def recognize_region(
        self, rgb: np.ndarray, region: Box, orientation: str
    ) -> list[OCRAttempt]:
        crop = rgb[region.y : region.y2, region.x : region.x2]
        if crop.size == 0:
            return []
        variants: list[tuple[str, np.ndarray]] = [
            ("raw", crop),
            ("chroma", _chroma_projection(crop)),
        ]
        if "vertical" in orientation or region.h > region.w * 1.15:
            variants.extend(
                [
                    ("raw_rot90", cv2.rotate(crop, cv2.ROTATE_90_COUNTERCLOCKWISE)),
                    (
                        "chroma_rot90",
                        cv2.rotate(
                            _chroma_projection(crop), cv2.ROTATE_90_COUNTERCLOCKWISE
                        ),
                    ),
                ]
            )
        if orientation == "internal_vertical":
            band_half = max(14, min(20, region.h // 7))
            center = region.h // 2
            y0, y1 = max(0, center - band_half), min(region.h, center + band_half)
            mid = crop[y0:y1, :]
            if mid.size:
                variants.extend(
                    [("mid_raw", mid), ("mid_chroma", _chroma_projection(mid))]
                )
        attempts: list[OCRAttempt] = []
        for mode, variant in variants:
            try:
                output = self._engine(
                    variant, use_det=False, use_cls=False, use_rec=True
                )
                text = output.txts[0] if output.txts else ""
                confidence = float(output.scores[0]) if output.scores else 0.0
            except Exception as exc:
                attempts.append(OCRAttempt(f"OCR 오류: {exc}", 0.0, mode))
                continue
            parsed_items = parse_measurement_items(text)
            if not parsed_items:
                attempts.append(OCRAttempt(text, confidence, mode))
                continue
            for parsed in parsed_items:
                attempts.append(
                    OCRAttempt(
                        text=text,
                        confidence=confidence,
                        mode=mode,
                        value_text=parsed.value_text,
                        unit=parsed.unit,
                        point_name=parsed.point_name,
                        item_index=parsed.item_index,
                    )
                )
        return attempts

    def detect_full_image(
        self, rgb: np.ndarray
    ) -> list[tuple[Box, OCRAttempt]]:
        try:
            output = self._engine(rgb, use_det=True, use_cls=True, use_rec=True)
        except Exception as exc:
            raise MeasurementError(f"전체 이미지 OCR 실패: {exc}") from exc
        if output.txts is None or output.boxes is None:
            return []
        scores = output.scores or tuple(0.0 for _ in output.txts)
        detections: list[tuple[Box, OCRAttempt]] = []
        for points, text, confidence in zip(output.boxes, output.txts, scores):
            parsed_items = parse_measurement_items(str(text))
            if not parsed_items:
                continue
            array = np.asarray(points, dtype=float)
            x1, y1 = np.floor(array.min(axis=0)).astype(int)
            x2, y2 = np.ceil(array.max(axis=0)).astype(int)
            box = Box(int(x1), int(y1), max(1, int(x2 - x1)), max(1, int(y2 - y1)))
            for parsed in parsed_items:
                detections.append(
                    (
                        box,
                        OCRAttempt(
                            text=str(text),
                            confidence=float(confidence),
                            mode="full_image",
                            value_text=parsed.value_text,
                            unit=parsed.unit,
                            point_name=parsed.point_name,
                            item_index=parsed.item_index,
                        ),
                    )
                )
        return detections


def _build_detection(
    detection_id: str,
    region: Box,
    orientation: str,
    image_width: int,
    image_height: int,
    attempts: list[OCRAttempt],
) -> MeasurementDetection:
    valid = sorted(
        (attempt for attempt in attempts if attempt.valid),
        key=lambda attempt: attempt.confidence,
        reverse=True,
    )
    if valid:
        chosen = valid[0]
        unique_values = {(attempt.value_text, attempt.unit) for attempt in valid}
        unique_point_names = {
            point_name_key(attempt.point_name)
            for attempt in valid
            if attempt.point_name
        }
        review = (
            chosen.confidence < 0.75
            or len(unique_values) > 1
            or len(unique_point_names) > 1
        )
        notes: list[str] = []
        if chosen.confidence < 0.75:
            notes.append("낮은 OCR 신뢰도")
        if len(unique_values) > 1:
            notes.append("전처리별 판독값 불일치")
        if len(unique_point_names) > 1:
            notes.append("전처리별 포인트명 불일치")
        return MeasurementDetection(
            detection_id=detection_id,
            box=region,
            image_width=image_width,
            image_height=image_height,
            value_text=chosen.value_text,
            unit=chosen.unit,
            point_name=chosen.point_name,
            raw_text=chosen.text,
            confidence=chosen.confidence,
            source_mode=f"color_anchor:{chosen.mode}",
            orientation=orientation,
            review_required=review,
            note="; ".join(notes),
            alternatives=attempts,
        )
    best = max(attempts, key=lambda attempt: attempt.confidence, default=None)
    return MeasurementDetection(
        detection_id=detection_id,
        box=region,
        image_width=image_width,
        image_height=image_height,
        raw_text=best.text if best else "",
        confidence=best.confidence if best else 0.0,
        source_mode=f"color_anchor:{best.mode if best else 'none'}",
        orientation=orientation,
        review_required=True,
        note="측정선 위치는 찾았지만 수치+단위를 확정하지 못함",
        alternatives=attempts,
    )


def _build_region_detections(
    detection_id: str,
    region: Box,
    orientation: str,
    image_width: int,
    image_height: int,
    attempts: list[OCRAttempt],
) -> list[MeasurementDetection]:
    item_indices = sorted({attempt.item_index for attempt in attempts if attempt.valid})
    if not item_indices:
        item_indices = [0]
    detections: list[MeasurementDetection] = []
    for output_index, item_index in enumerate(item_indices, 1):
        grouped = [
            attempt
            for attempt in attempts
            if attempt.item_index == item_index
            and (attempt.valid or item_index == item_indices[0])
        ]
        suffix = "" if len(item_indices) == 1 else f"_{output_index}"
        detections.append(
            _build_detection(
                detection_id + suffix,
                region,
                orientation,
                image_width,
                image_height,
                grouped,
            )
        )
    return detections


def _centers_close(first: Box, second: Box, width: int, height: int) -> bool:
    dx = (first.cx - second.cx) / max(1, width)
    dy = (first.cy - second.cy) / max(1, height)
    return math.hypot(dx, dy) <= 0.09


def _merge_full_image_attempts(
    result: ImageScanResult,
    full_items: list[tuple[Box, OCRAttempt]],
) -> None:
    for box, attempt in full_items:
        existing = next(
            (
                detection
                for detection in result.detections
                if _centers_close(detection.box, box, result.image_width, result.image_height)
                and (
                    detection.numeric_value is None
                    or (
                        detection.point_name
                        and attempt.point_name
                        and point_name_key(detection.point_name)
                        == point_name_key(attempt.point_name)
                    )
                    or (
                        detection.value_text == attempt.value_text
                        and detection.unit == attempt.unit
                        and not (detection.point_name and attempt.point_name)
                    )
                )
            ),
            None,
        )
        if existing is not None:
            existing.alternatives.append(attempt)
            if existing.numeric_value is None:
                existing.value_text = attempt.value_text
                existing.unit = attempt.unit
                existing.point_name = attempt.point_name
                existing.raw_text = attempt.text
                existing.confidence = attempt.confidence
                existing.source_mode = "full_image"
                existing.review_required = attempt.confidence < 0.75
                existing.note = "전체 이미지 OCR로 보완" + (
                    "; 낮은 OCR 신뢰도" if attempt.confidence < 0.75 else ""
                )
            elif not existing.point_name and attempt.point_name:
                existing.point_name = attempt.point_name
            elif (existing.value_text, existing.unit) != (
                attempt.value_text,
                attempt.unit,
            ):
                existing.review_required = True
                existing.note = (
                    existing.note + "; " if existing.note else ""
                ) + f"전체 OCR와 불일치({attempt.value_text} {attempt.unit})"
            continue

        footer = box.cy >= result.image_height * 0.87
        item = MeasurementDetection(
            detection_id=f"F{len(result.detections) + 1}",
            box=box.clipped(result.image_width, result.image_height),
            image_width=result.image_width,
            image_height=result.image_height,
            value_text=attempt.value_text,
            unit=attempt.unit,
            point_name=attempt.point_name,
            raw_text=attempt.text,
            confidence=attempt.confidence,
            source_mode="full_image",
            orientation="horizontal",
            included=not footer,
            review_required=footer or attempt.confidence < 0.75,
            note=(
                "하단 스케일바/장비정보 후보 — 기본 제외"
                if footer
                else "색상 측정선이 없어 전체 이미지 OCR로 검출"
            ),
            alternatives=[attempt],
        )
        result.detections.append(item)


def scan_image(
    identity: ImageIdentity,
    engine: RapidOCREngine,
    *,
    deep_verify: bool = False,
) -> ImageScanResult:
    started = time.perf_counter()
    try:
        with Image.open(identity.path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            rgb = np.asarray(image)
        height, width = rgb.shape[:2]
        result = ImageScanResult(
            path=identity.path,
            sample_id=identity.sample_id,
            image_index=identity.image_index,
            image_width=width,
            image_height=height,
            identity_inferred=identity.inferred,
        )
        regions, threshold = detect_colored_measurement_regions(rgb)
        result.color_threshold = threshold
        for index, (region, orientation) in enumerate(regions, 1):
            attempts = engine.recognize_region(rgb, region, orientation)
            items = _build_region_detections(
                f"C{index}",
                region,
                orientation,
                width,
                height,
                attempts,
            )
            for item in items:
                if item.box.cy >= height * 0.88:
                    item.included = False
                    item.review_required = True
                    item.note = (
                        item.note + "; " if item.note else ""
                    ) + "하단 스케일바/장비정보 후보 — 기본 제외"
                result.detections.append(item)

        parsed_ratio = (
            sum(item.numeric_value is not None for item in result.detections)
            / len(result.detections)
            if result.detections
            else 0.0
        )
        if deep_verify or not regions or parsed_ratio < 0.35:
            full_items = engine.detect_full_image(rgb)
            result.used_full_fallback = True
            _merge_full_image_attempts(result, full_items)

        mark_duplicate_point_names(result)
        order_result_by_groups(result, derive_group_order(result.detections))
        result.selected = any(item.included for item in result.detections)
        result.elapsed_seconds = time.perf_counter() - started
        return result
    except Exception as exc:
        return ImageScanResult(
            path=identity.path,
            sample_id=identity.sample_id,
            image_index=identity.image_index,
            image_width=0,
            image_height=0,
            selected=False,
            elapsed_seconds=time.perf_counter() - started,
            identity_inferred=identity.inferred,
            error=str(exc),
        )


def scan_images(
    paths: Iterable[str | os.PathLike[str]],
    *,
    deep_verify: bool = False,
    progress: Callable[[int, int, Path], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[ImageScanResult]:
    identities = infer_identities(paths)
    if not identities:
        raise ValueError("검사할 지원 이미지가 없습니다.")
    engine = RapidOCREngine()
    results: list[ImageScanResult] = []
    total = len(identities)
    for index, identity in enumerate(identities, 1):
        if cancelled and cancelled():
            raise BatchCancelled("사용자가 검사를 취소했습니다.")
        if progress:
            progress(index, total, identity.path)
        results.append(scan_image(identity, engine, deep_verify=deep_verify))
    return results


def create_template_from_result(
    result: ImageScanResult, group_order: Sequence[str] | None = None
) -> MeasurementTemplate:
    resolved_group_order = complete_group_order(
        group_order or derive_group_order(result.detections), result.detections
    )
    rank = {key: index for index, key in enumerate(resolved_group_order)}
    detections = sorted(
        result.included_detections,
        key=lambda item: (
            rank.get(item.group_key, len(rank)),
            item.box.y,
            item.box.x,
        ),
    )
    if not detections:
        raise ValueError("기준 순서로 저장할 측정위치가 없습니다.")
    counters: dict[str, int] = {}
    points: list[TemplatePoint] = []
    for index, detection in enumerate(detections, 1):
        counters[detection.group_key] = counters.get(detection.group_key, 0) + 1
        fallback_label = (
            f"{measurement_group_label(detection.group_key)} "
            f"{counters[detection.group_key]}"
        )
        points.append(
            TemplatePoint(
                slot_id=f"T{index}",
                order=index,
                x_norm=detection.center_normalized[0],
                y_norm=detection.center_normalized[1],
                expected_unit=detection.unit,
                label=detection.point_name or fallback_label,
                point_name=detection.point_name,
                group_key=detection.group_key,
            )
        )
    return MeasurementTemplate(
        reference_name=result.path.name,
        reference_width=result.image_width,
        reference_height=result.image_height,
        points=points,
        group_order=resolved_group_order,
    )


def order_result_by_template(
    result: ImageScanResult, template: MeasurementTemplate
) -> None:
    """Put matched detections in the user's sample order and append new positions."""

    included = [item for item in result.detections if item.included]
    excluded = [item for item in result.detections if not item.included]
    assignments, unmatched = _match_to_template(included, template.points)
    ordered: list[MeasurementDetection] = []
    resolved_group_order = complete_group_order(template.group_order, included)
    point_groups = {
        point.slot_id: point.group_key or measurement_group(point.expected_unit)
        for point in template.points
    }
    for group_key in resolved_group_order:
        for point in sorted(template.points, key=lambda item: item.order):
            if point_groups[point.slot_id] != group_key:
                continue
            matched = assignments.get(point.slot_id)
            if matched is not None:
                ordered.append(matched)
        ordered.extend(
            sorted(
                (item for item in unmatched if item.group_key == group_key),
                key=lambda item: (item.box.y, item.box.x),
            )
        )
    ordered.extend(sorted(excluded, key=lambda item: (item.box.y, item.box.x)))
    result.detections[:] = ordered
    for index, detection in enumerate(result.detections, 1):
        detection.order = index


def save_template(template: MeasurementTemplate, path: str | os.PathLike[str]) -> Path:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(template.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, destination)
    return destination


def load_template(path: str | os.PathLike[str]) -> MeasurementTemplate:
    source = Path(path).expanduser().resolve()
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("템플릿 최상위 형식이 올바르지 않습니다.")
    return MeasurementTemplate.from_dict(data)


def _match_to_template(
    detections: list[MeasurementDetection],
    points: list[TemplatePoint],
    *,
    maximum_distance: float = 0.25,
) -> tuple[dict[str, MeasurementDetection], list[MeasurementDetection]]:
    assignments: dict[str, MeasurementDetection] = {}
    used_detections: set[int] = set()
    used_points: set[int] = set()

    # A unique OCR point name is stronger evidence than image position.
    for point_index, point in enumerate(points):
        key = point_name_key(point.point_name)
        if not key:
            continue
        candidates = [
            (
                math.dist(
                    (point.x_norm, point.y_norm), detection.center_normalized
                ),
                detection_index,
            )
            for detection_index, detection in enumerate(detections)
            if detection_index not in used_detections
            and point_name_key(detection.point_name) == key
        ]
        if not candidates:
            continue
        _, detection_index = min(candidates)
        detection = detections[detection_index]
        detection.column_key = point.slot_id
        assignments[point.slot_id] = detection
        used_points.add(point_index)
        used_detections.add(detection_index)

    pairs: list[tuple[float, int, int]] = []
    for point_index, point in enumerate(points):
        if point_index in used_points:
            continue
        point_group = point.group_key or measurement_group(point.expected_unit)
        for detection_index, detection in enumerate(detections):
            if detection_index in used_detections:
                continue
            if point_group != "unknown" and detection.group_key != point_group:
                continue
            distance = math.dist(
                (point.x_norm, point.y_norm), detection.center_normalized
            )
            if distance <= maximum_distance:
                pairs.append((distance, point_index, detection_index))
    for _, point_index, detection_index in sorted(pairs):
        if point_index in used_points or detection_index in used_detections:
            continue
        point = points[point_index]
        detection = detections[detection_index]
        detection.column_key = point.slot_id
        assignments[point.slot_id] = detection
        used_points.add(point_index)
        used_detections.add(detection_index)
    unmatched = [
        detection
        for index, detection in enumerate(detections)
        if index not in used_detections
    ]
    return assignments, unmatched


def assign_export_columns(
    results: Sequence[ImageScanResult],
    template: MeasurementTemplate | None,
) -> list[ExportColumn]:
    selected = [result for result in results if result.selected and not result.error]
    columns: list[ExportColumn] = []
    template_points = (
        sorted(template.points, key=lambda point: point.order) if template else []
    )
    for point in template_points:
        columns.append(
            ExportColumn(
                key=point.slot_id,
                display_order=point.order,
                x_norm=point.x_norm,
                y_norm=point.y_norm,
                label=point.label or f"측정값 {point.order}",
                expected_unit=point.expected_unit,
                point_name=point.point_name,
                group_key=point.group_key
                or measurement_group(point.expected_unit),
                points=[(point.x_norm, point.y_norm)],
            )
        )

    unmatched_by_result: list[tuple[ImageScanResult, list[MeasurementDetection]]] = []
    for result in selected:
        detections = result.included_detections
        for detection in detections:
            detection.column_key = ""
        _, unmatched = _match_to_template(detections, template_points)
        unmatched_by_result.append((result, unmatched))

    extra_columns: list[ExportColumn] = []
    for _, detections in unmatched_by_result:
        used_keys: set[str] = set()
        for detection in sorted(
            detections, key=lambda item: (item.group_key, item.box.y, item.box.x)
        ):
            point = detection.center_normalized
            name_key = point_name_key(detection.point_name)
            if name_key:
                candidates = [
                    (math.dist((column.x_norm, column.y_norm), point), column)
                    for column in extra_columns
                    if column.key not in used_keys
                    and point_name_key(column.point_name) == name_key
                ]
            else:
                candidates = [
                    (math.dist((column.x_norm, column.y_norm), point), column)
                    for column in extra_columns
                    if column.key not in used_keys
                    and not column.point_name
                    and column.group_key == detection.group_key
                ]
            candidates.sort(key=lambda item: item[0])
            if candidates and (name_key or candidates[0][0] <= 0.14):
                column = candidates[0][1]
                count = len(column.points)
                column.x_norm = (column.x_norm * count + point[0]) / (count + 1)
                column.y_norm = (column.y_norm * count + point[1]) / (count + 1)
                column.points.append(point)
            else:
                column = ExportColumn(
                    key=f"E{len(extra_columns) + 1}",
                    display_order=0,
                    x_norm=point[0],
                    y_norm=point[1],
                    label=detection.point_name,
                    expected_unit=detection.unit,
                    point_name=detection.point_name,
                    group_key=detection.group_key,
                    points=[point],
                )
                extra_columns.append(column)
            detection.column_key = column.key
            used_keys.add(column.key)

    columns.extend(extra_columns)
    if template and template.group_order:
        group_order = list(template.group_order)
    elif selected:
        group_order = derive_group_order(selected[0].included_detections)
    else:
        group_order = []
    for column in columns:
        if not column.group_key:
            column.group_key = measurement_group(column.expected_unit)
        if column.group_key not in group_order:
            group_order.append(column.group_key)
    rank = {key: index for index, key in enumerate(group_order)}
    columns.sort(
        key=lambda column: (
            rank.get(column.group_key, len(rank)),
            column.y_norm,
            column.x_norm,
            point_name_key(column.point_name),
        )
    )
    counters: dict[str, int] = {}
    for index, column in enumerate(columns, 1):
        column.display_order = index
        counters[column.group_key] = counters.get(column.group_key, 0) + 1
        column.label = column.point_name or (
            f"{measurement_group_label(column.group_key)} "
            f"{counters[column.group_key]}"
        )
    return columns


def _column_units(
    selected: Sequence[ImageScanResult], columns: Sequence[ExportColumn]
) -> dict[str, tuple[str, bool]]:
    output: dict[str, tuple[str, bool]] = {}
    for column in columns:
        units = {
            detection.unit
            for result in selected
            for detection in result.included_detections
            if detection.column_key == column.key and detection.numeric_value is not None
        }
        if len(units) == 1:
            output[column.key] = (next(iter(units)), True)
        elif not units and column.expected_unit:
            output[column.key] = (column.expected_unit, True)
        elif not units:
            output[column.key] = ("단위 미확인", False)
        else:
            output[column.key] = ("단위 확인 필요", False)
    return output


def _write_workbook(
    path: Path,
    results: Sequence[ImageScanResult],
    template: MeasurementTemplate | None,
) -> tuple[int, int, int]:
    try:
        import xlsxwriter
        from xlsxwriter.utility import xl_col_to_name
    except Exception as exc:  # pragma: no cover - environment dependent
        raise MeasurementError("XlsxWriter가 설치되지 않았습니다.") from exc

    selected = [result for result in results if result.selected and not result.error]
    if not selected:
        raise ValueError("Excel에 저장할 선택 이미지가 없습니다.")
    columns = assign_export_columns(selected, template)
    units = _column_units(selected, columns)
    review_count = sum(
        detection.status != "정상"
        for result in selected
        for detection in result.included_detections
    )
    measurement_count = sum(len(result.included_detections) for result in selected)

    with xlsxwriter.Workbook(str(path)) as workbook:
        workbook.set_properties(
            {
                "title": "디스플레이 구조분석 측정값",
                "subject": "FIB/SEM 이미지 측정값 자동 검출 결과",
                "author": "디스플레이 측정값 자동 정리 v2.1",
                "comments": "확인 필요 항목은 원본 이미지와 대조한 뒤 사용하세요.",
            }
        )
        header = workbook.add_format(
            {
                "font_name": "맑은 고딕",
                "bold": True,
                "font_color": "#FFFFFF",
                "bg_color": "#1F4E78",
                "border": 1,
                "align": "center",
                "valign": "vcenter",
            }
        )
        normal = workbook.add_format(
            {"font_name": "맑은 고딕", "border": 1, "valign": "vcenter"}
        )
        center = workbook.add_format(
            {
                "font_name": "맑은 고딕",
                "border": 1,
                "align": "center",
                "valign": "vcenter",
            }
        )
        warning = workbook.add_format(
            {
                "font_name": "맑은 고딕",
                "border": 1,
                "bg_color": "#FFF2CC",
                "font_color": "#9C6500",
            }
        )
        average = workbook.add_format(
            {
                "font_name": "맑은 고딕",
                "bold": True,
                "border": 1,
                "bg_color": "#D9EAF7",
                "num_format": "0.#########",
            }
        )
        numeric_formats: dict[tuple[int, bool], object] = {}

        def measurement_number_format(value_text: str, needs_review: bool = False):
            places = min(15, decimal_places(value_text))
            key = (places, needs_review)
            if key not in numeric_formats:
                properties: dict[str, object] = {
                    "font_name": "맑은 고딕",
                    "border": 1,
                    "align": "right",
                    "num_format": "0" + ("." + "0" * places if places else ""),
                }
                if needs_review:
                    properties.update(
                        {"bg_color": "#FFF2CC", "font_color": "#9C6500"}
                    )
                numeric_formats[key] = workbook.add_format(properties)
            return numeric_formats[key]

        sheet = workbook.add_worksheet("측정 데이터")
        fixed = ["시료번호", "이미지 순번", "데이터 이미지"]
        measurement_headers = [
            f"{column.label} ({units[column.key][0]})"
            for column in columns
        ]
        tail = ["판독상태", "검토 필요 수", "소요시간(초)", "원본 경로"]
        headings = fixed + measurement_headers + tail
        for col, heading in enumerate(headings):
            sheet.write_string(0, col, heading, header)

        for row, result in enumerate(selected, 1):
            sheet.write_string(row, 0, result.sample_id, center)
            sheet.write_number(row, 1, result.image_index, center)
            sheet.write_string(row, 2, result.path.name, normal)
            by_key = {
                detection.column_key: detection
                for detection in result.included_detections
                if detection.column_key
            }
            for offset, column in enumerate(columns):
                detection = by_key.get(column.key)
                col = 3 + offset
                if detection and detection.numeric_value is not None:
                    fmt = measurement_number_format(
                        detection.value_text, detection.review_required
                    )
                    sheet.write_number(row, col, detection.numeric_value, fmt)
                else:
                    sheet.write_blank(
                        row,
                        col,
                        None,
                        warning if detection else measurement_number_format("0"),
                    )
            tail_start = 3 + len(columns)
            result_review = sum(
                detection.status != "정상" for detection in result.included_detections
            )
            status_fmt = warning if result.status != "정상" else center
            sheet.write_string(row, tail_start, result.status, status_fmt)
            sheet.write_number(row, tail_start + 1, result_review, center)
            sheet.write_number(row, tail_start + 2, result.elapsed_seconds, center)
            sheet.write_string(row, tail_start + 3, str(result.path), normal)

        average_row = len(selected) + 1
        sheet.write_string(average_row, 0, "평균", average)
        sheet.write_string(average_row, 1, "", average)
        sheet.write_string(average_row, 2, "숫자 셀 기준", average)
        for offset, column in enumerate(columns):
            col = 3 + offset
            unit_consistent = units[column.key][1]
            stable_position = bool(column.point_name) or column.spread <= 0.18
            if unit_consistent and stable_position:
                col_name = xl_col_to_name(col)
                values = [
                    detection.numeric_value
                    for result in selected
                    for detection in result.included_detections
                    if detection.column_key == column.key
                    and detection.numeric_value is not None
                ]
                if values:
                    cached = sum(values) / len(values)
                    formula = f'=IFERROR(AVERAGE({col_name}2:{col_name}{len(selected) + 1}),"")'
                    sheet.write_formula(average_row, col, formula, average, cached)
                else:
                    sheet.write_string(average_row, col, "값 없음", warning)
            else:
                reason = "단위 확인 필요" if not unit_consistent else "위치 확인 필요"
                sheet.write_string(average_row, col, reason, warning)
        sheet.set_row(0, 26)
        sheet.set_row(average_row, 24)
        sheet.set_column(0, 0, 16)
        sheet.set_column(1, 1, 12)
        sheet.set_column(2, 2, 34)
        if columns:
            sheet.set_column(3, 2 + len(columns), 17)
        tail_start = 3 + len(columns)
        sheet.set_column(tail_start, tail_start + 2, 15)
        sheet.set_column(tail_start + 3, tail_start + 3, 60)
        sheet.freeze_panes(1, 3)
        sheet.autofilter(0, 0, len(selected), len(headings) - 1)
        sheet.hide_gridlines(2)
        sheet.set_landscape()
        sheet.fit_to_pages(1, 0)

        detail = workbook.add_worksheet("전체 검출 목록")
        detail_headers = [
            "시료번호",
            "이미지 순번",
            "파일명",
            "이미지 선택",
            "측정 순서",
            "사용 여부",
            "포인트명",
            "측정 그룹",
            "수치값",
            "단위",
            "상태",
            "신뢰도",
            "OCR 원문",
            "검출 방식",
            "비고",
            "x",
            "y",
            "width",
            "height",
            "열 키",
        ]
        for col, heading in enumerate(detail_headers):
            detail.write_string(0, col, heading, header)
        row = 1
        for result in results:
            for detection in sorted(result.detections, key=lambda item: item.order):
                values: list[object] = [
                    result.sample_id,
                    result.image_index,
                    result.path.name,
                    "예" if result.selected else "아니오",
                    detection.order,
                    "예" if detection.included else "아니오",
                    detection.point_name,
                    measurement_group_label(detection.group_key),
                    detection.numeric_value if detection.numeric_value is not None else "",
                    detection.unit,
                    detection.status,
                    detection.confidence,
                    detection.raw_text,
                    detection.source_mode,
                    detection.note,
                    detection.box.x,
                    detection.box.y,
                    detection.box.w,
                    detection.box.h,
                    detection.column_key,
                ]
                for col, value in enumerate(values):
                    fmt = warning if detection.status != "정상" else normal
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        if col == 8 and detection.value_text:
                            fmt = measurement_number_format(
                                detection.value_text, detection.status != "정상"
                            )
                        detail.write_number(row, col, value, fmt)
                    elif value == "":
                        detail.write_blank(row, col, None, fmt)
                    else:
                        detail.write_string(row, col, str(value), fmt)
                row += 1
        detail_widths = [
            15,
            11,
            30,
            11,
            11,
            11,
            18,
            12,
            14,
            10,
            15,
            11,
            30,
            22,
            45,
            9,
            9,
            9,
            9,
            12,
        ]
        for col, width in enumerate(detail_widths):
            detail.set_column(col, col, width)
        detail.freeze_panes(1, 0)
        detail.autofilter(0, 0, max(1, row - 1), len(detail_headers) - 1)
        detail.hide_gridlines(2)

        review = workbook.add_worksheet("검토 필요")
        review_headers = [
            "시료번호",
            "이미지 순번",
            "파일명",
            "측정 순서",
            "포인트명",
            "측정 그룹",
            "현재 수치",
            "단위",
            "상태",
            "사유",
            "OCR 원문",
        ]
        for col, heading in enumerate(review_headers):
            review.write_string(0, col, heading, header)
        review_row = 1
        for result in results:
            for detection in result.detections:
                if detection.status == "정상":
                    continue
                row_values = [
                    result.sample_id,
                    result.image_index,
                    result.path.name,
                    detection.order,
                    detection.point_name,
                    measurement_group_label(detection.group_key),
                    detection.value_text,
                    detection.unit,
                    detection.status,
                    detection.note,
                    detection.raw_text,
                ]
                for col, value in enumerate(row_values):
                    if isinstance(value, int):
                        review.write_number(review_row, col, value, warning)
                    elif value == "":
                        review.write_blank(review_row, col, None, warning)
                    else:
                        review.write_string(review_row, col, str(value), warning)
                review_row += 1
        for col, width in enumerate([15, 11, 30, 11, 18, 12, 14, 10, 15, 48, 32]):
            review.set_column(col, col, width)
        review.freeze_panes(1, 0)
        review.hide_gridlines(2)

        info = workbook.add_worksheet("설정 정보")
        info.write_string(0, 0, "항목", header)
        info.write_string(0, 1, "내용", header)
        group_order = (
            list(template.group_order)
            if template and template.group_order
            else list(dict.fromkeys(column.group_key for column in columns))
        )
        metadata = [
            ("프로그램 버전", APP_VERSION),
            ("OCR", "RapidOCR 3.9.2 / PP-OCRv6 / ONNX Runtime CPU"),
            ("실행 방식", "로컬 처리; 이미지·OCR 결과 외부 전송 없음"),
            ("선택 이미지 수", str(len(selected))),
            ("측정값 수", str(measurement_count)),
            ("검토 필요 수", str(review_count)),
            ("열 수", str(len(columns))),
            ("기준 이미지", template.reference_name if template else "미설정"),
            (
                "측정 그룹 순서",
                " → ".join(measurement_group_label(key) for key in group_order),
            ),
            ("생성시각(UTC)", datetime.now(timezone.utc).isoformat(timespec="seconds")),
        ]
        for row_index, (name, value) in enumerate(metadata, 1):
            info.write_string(row_index, 0, name, normal)
            info.write_string(row_index, 1, value, normal)
        info.set_column(0, 0, 24)
        info.set_column(1, 1, 70)
        info.hide_gridlines(2)

    return measurement_count, review_count, len(columns)


def verify_xlsx(path: str | os.PathLike[str]) -> None:
    source = Path(path)
    try:
        with zipfile.ZipFile(source, "r") as archive:
            required = {
                "[Content_Types].xml",
                "xl/workbook.xml",
                "xl/worksheets/sheet1.xml",
                "xl/worksheets/sheet2.xml",
                "xl/worksheets/sheet3.xml",
                "xl/worksheets/sheet4.xml",
            }
            missing = required - set(archive.namelist())
            if missing:
                raise MeasurementError(f"XLSX 필수 파일 누락: {sorted(missing)}")
            bad_member = archive.testzip()
            if bad_member:
                raise MeasurementError(f"XLSX CRC 오류: {bad_member}")
            for name in archive.namelist():
                if name.endswith((".xml", ".rels")):
                    ElementTree.fromstring(archive.read(name))
            workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
            tag = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet"
            names = [node.attrib.get("name", "") for node in workbook.iter(tag)]
            expected = ["측정 데이터", "전체 검출 목록", "검토 필요", "설정 정보"]
            if names != expected:
                raise MeasurementError(f"시트 검증 실패: {names}")
    except zipfile.BadZipFile as exc:
        raise MeasurementError("올바른 XLSX 파일이 아닙니다.") from exc
    except ElementTree.ParseError as exc:
        raise MeasurementError(f"XLSX XML 오류: {exc}") from exc


def export_excel(
    results: Sequence[ImageScanResult],
    output_path: str | os.PathLike[str],
    *,
    template: MeasurementTemplate | None = None,
    overwrite: bool = False,
) -> ExportSummary:
    destination = Path(output_path).expanduser().resolve()
    if destination.suffix.lower() != ".xlsx":
        destination = destination.with_suffix(".xlsx")
    if destination.exists() and not overwrite:
        raise FileExistsError(f"이미 존재하는 파일입니다: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=destination.stem + "_", suffix=".tmp.xlsx", dir=destination.parent
    )
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        measurement_count, review_count, column_count = _write_workbook(
            temporary, results, template
        )
        verify_xlsx(temporary)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    selected_images = sum(result.selected and not result.error for result in results)
    return ExportSummary(
        output_path=destination,
        selected_images=selected_images,
        measurement_count=measurement_count,
        review_count=review_count,
        column_count=column_count,
        sha256=file_sha256(destination),
    )
