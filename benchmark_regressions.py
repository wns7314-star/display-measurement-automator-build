"""Deterministic synthetic OCR fixtures, no confidential images or network calls."""
from pathlib import Path
import json
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

from measurement_ocr_v2 import ImageIdentity, RapidOCREngine, scan_image


def test_font(size):
    for name in ("DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    raise RuntimeError("No scalable test font")


def make_cases(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    cases = []
    def save(name, image, values):
        path = folder / (name + ".png")
        image.save(path)
        cases.append((path, values))

    image = Image.new("RGB", (1000, 600), (85, 85, 85))
    draw = ImageDraw.Draw(image)
    green = (20, 240, 20)
    draw.line((350, 240, 650, 240), fill=green, width=2)
    draw.line((350, 230, 350, 250), fill=green, width=2)
    draw.line((650, 230, 650, 250), fill=green, width=2)
    draw.text((410, 207), "123.4 nm", font=test_font(22), fill=green)
    draw.text((0, 100), "32.5°", font=test_font(20), fill=(235, 235, 235))
    label = "67.8°"
    draw.text((1000 - draw.textbbox((0, 0), label, font=test_font(20))[2], 360), label, font=test_font(20), fill=(20, 210, 210))
    save("edges_with_center_anchor", image, [("123.4", "nm"), ("32.5", "°"), ("67.8", "°")])

    image = Image.new("RGB", (1000, 600), (150, 150, 150))
    draw = ImageDraw.Draw(image)
    for xy, text in [((5, 70), "12.5 deg"), ((860, 270), "284.2 nm"), ((430, 460), "157.2 nm")]:
        draw.text(xy, text, font=test_font(12), fill=(174, 174, 174))
    save("small_faint_gray", image, [("12.5", "°"), ("284.2", "nm"), ("157.2", "nm")])

    image = Image.new("RGB", (1000, 600), (150, 150, 150))
    draw = ImageDraw.Draw(image)
    draw.text((3, 70), "Top-W=167.5nm", font=test_font(14), fill=(159, 179, 179))
    draw.text((847, 400), "A1=28.4°", font=test_font(14), fill=(179, 159, 179))
    image = image.filter(ImageFilter.GaussianBlur(0.35))
    save("faint_color", image, [("167.5", "nm"), ("28.4", "°")])

    image = Image.new("RGB", (2200, 1300), (95, 95, 95))
    draw = ImageDraw.Draw(image)
    draw.text((1, 600), "17.5 deg", font=test_font(12), fill=(180, 180, 180))
    draw.text((2095, 400), "123.45 nm", font=test_font(12), fill=(120, 205, 205))
    draw.text((600, 930), "H1=284.2nm", font=test_font(14), fill=(210, 210, 210))
    save("large_canvas_small_text", image, [("17.5", "°"), ("123.45", "nm"), ("284.2", "nm")])

    image = Image.new("RGB", (900, 500), (95, 95, 95))
    draw = ImageDraw.Draw(image)
    draw.text((2, 448), "41.2°", font=test_font(20), fill="white")
    draw.text((810, 447), "56.7°", font=test_font(20), fill="white")
    draw.text((680, 430), "100 nm", font=test_font(16), fill="white")
    draw.line((670, 465, 775, 465), fill="white", width=2)
    save("bottom_angles_and_scale", image, [("41.2", "°"), ("56.7", "°")])

    image = Image.new("RGB", (900, 600), (110, 110, 110))
    for side, text, color in [("left", "15.2 deg", (255, 30, 30)), ("right", "28.4°", (20, 240, 240))]:
        label = Image.new("RGB", (150, 32), (110, 110, 110))
        ImageDraw.Draw(label).text((5, 1), text, font=test_font(22), fill=color)
        label = label.rotate(90 if side == "left" else -90, expand=True)
        image.paste(label, (0 if side == "left" else 868, 200))
    save("vertical_edges", image, [("15.2", "°"), ("28.4", "°")])
    return cases


def run(folder, output):
    engine = RapidOCREngine()
    records = []
    for path, expected in make_cases(folder):
        result = scan_image(ImageIdentity(path, path.stem, 1), engine)
        found = [(d.value_text, d.unit) for d in result.included_detections if d.value_text]
        record = dict(case=path.stem, expected=expected, found=found,
                      matched=sum(x in found for x in expected), total=len(expected),
                      unexpected=[x for x in found if x not in expected],
                      seconds=round(result.elapsed_seconds, 3), error=result.error,
                      detections=[dict(value=d.value_text, unit=d.unit, point=d.point_name,
                                       box=d.box.as_tuple(), included=d.included,
                                       review=d.review_required, raw=d.raw_text, note=d.note) for d in result.detections])
        records.append(record)
        print(json.dumps({k:v for k,v in record.items() if k != "detections"}, ensure_ascii=False), flush=True)
    Path(output).write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    import sys
    run(sys.argv[1], sys.argv[2])
