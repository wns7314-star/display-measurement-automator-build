"""End-to-end benchmark: six different images, 120 distinct labeled measurements."""
from pathlib import Path
import json
from PIL import Image, ImageDraw
from benchmark_regressions import test_font
from measurement_ocr_v2 import ImageIdentity, RapidOCREngine, scan_image, export_excel


def run(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    engine = RapidOCREngine()
    rows, scans = [], []
    colors = [(235, 235, 235), (30, 230, 230), (235, 50, 50), (60, 240, 60), (180, 180, 180), (240, 220, 30)]
    for image_index in range(6):
        image = Image.new("RGB", (1500, 1000), (150, 150, 150))
        draw = ImageDraw.Draw(image)
        expected = []
        for index in range(20):
            angle = index % 3 == 0
            point = ("A" if angle else "H") + str(index + 1)
            value = f"{20 + image_index * 20 + index}.5" if angle else f"{150 + image_index * 20 + index}.2"
            unit = "°" if angle else "nm"
            text = f"{point}={value}{unit}"
            size = (14, 18, 22)[(image_index + index) % 3]
            font = test_font(size)
            x = [0, 400, 800, 1500 - draw.textbbox((0, 0), text, font=font)[2]][index % 4]
            y = [30, 215, 400, 600, 800][index // 4]
            draw.text((x, y), text, font=font, fill=colors[(image_index + index) % 6])
            expected.append((point, value, unit))
        path = folder / f"batch-{image_index+1}.png"
        image.save(path)
        result = scan_image(ImageIdentity(path, str(image_index+1), 1), engine)
        scans.append(result)
        found = [(d.point_name, d.value_text, d.unit) for d in result.included_detections if d.value_text]
        row = dict(file=path.name, total=len(expected), exact=sum(t in found for t in expected),
                   missing=[t for t in expected if t not in found],
                   unexpected=[t for t in found if t not in expected],
                   review=sum(d.review_required for d in result.included_detections),
                   seconds=round(result.elapsed_seconds, 3), error=result.error)
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    export_excel(scans, folder / "batch_output.xlsx", overwrite=True)
    (folder / "batch_results.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    import sys
    run(sys.argv[1])
