from __future__ import annotations

import json
import time
from pathlib import Path

import cv2
import numpy as np
from measurement_ocr_v2 import Box, RapidOCREngine
from PIL import Image


def benchmark_image() -> tuple[np.ndarray, Box, tuple[str, str], str]:
    """Use the public attachment when present, otherwise a reproducible image."""

    root = Path(__file__).resolve().parent.parent
    sample = root / "upload" / "IMG_9907.jpeg"
    if sample.is_file():
        with Image.open(sample) as source:
            rgb = np.asarray(source.convert("RGB"))
        return rgb, Box(219, 139, 114, 42), ("1.82", "µm"), "첨부 실제 이미지"

    rgb = np.full((120, 420, 3), 70, dtype=np.uint8)
    cv2.putText(
        rgb,
        "2.35 um",
        (35, 75),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.6,
        (20, 255, 20),
        3,
        cv2.LINE_AA,
    )
    return rgb, Box(0, 0, 420, 120), ("2.35", "µm"), "내장 합성 이미지"


def main() -> int:
    rgb, region, expected, source_name = benchmark_image()
    engine_started = time.perf_counter()
    engine = RapidOCREngine()
    engine_seconds = time.perf_counter() - engine_started
    started = time.perf_counter()
    successes = 0
    for _ in range(120):
        attempts = engine.recognize_region(rgb, region, "internal_horizontal")
        if any((attempt.value_text, attempt.unit) == expected for attempt in attempts):
            successes += 1
    elapsed = time.perf_counter() - started
    print(
        json.dumps(
            {
                "source": source_name,
                "points": 120,
                "successful_points": successes,
                "engine_initialization_seconds": round(engine_seconds, 3),
                "recognition_seconds": round(elapsed, 3),
                "seconds_per_point": round(elapsed / 120, 4),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if successes == 120 else 1


if __name__ == "__main__":
    raise SystemExit(main())
