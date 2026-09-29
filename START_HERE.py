from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REQUIRED_IMPORTS = ("PIL", "numpy", "cv2", "rapidocr", "onnxruntime", "xlsxwriter")


def missing_modules() -> list[str]:
    return [name for name in REQUIRED_IMPORTS if importlib.util.find_spec(name) is None]


def show_error(message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("설치 또는 실행 오류", message, parent=root)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr)


def install_requirements(base: Path) -> bool:
    requirements = base / "requirements.txt"
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "-r",
        str(requirements),
    ]
    print("필요 Python 패키지를 설치합니다. Tesseract 설치는 필요하지 않습니다.")
    print(" ".join(command))
    completed = subprocess.run(command, cwd=base, check=False)
    return completed.returncode == 0


def main() -> int:
    base = Path(__file__).resolve().parent
    missing = missing_modules()
    if missing:
        print("누락 패키지:", ", ".join(missing))
        if not install_requirements(base):
            show_error(
                "필요 패키지 자동 설치에 실패했습니다.\n\n"
                "회사망에서 Python 패키지 저장소 접속이 허용되는지 확인한 뒤, "
                "VSCode 터미널에서 다음 명령을 실행하세요.\n\n"
                f'"{sys.executable}" -m pip install -r "{base / "requirements.txt"}"'
            )
            return 1
        importlib.invalidate_caches()
        remaining = missing_modules()
        if remaining:
            show_error("설치 후에도 불러올 수 없는 패키지: " + ", ".join(remaining))
            return 1

    sys.path.insert(0, str(base))
    try:
        from display_measurement_automator_v2 import main as run_app

        run_app()
        return 0
    except Exception as exc:
        show_error(f"프로그램 실행 중 오류가 발생했습니다.\n\n{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

