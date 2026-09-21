@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>&1
if errorlevel 1 (
  echo Python 3.12가 필요합니다.
  pause
  exit /b 1
)

py -3.12 -m venv .build_venv
if errorlevel 1 goto failed
call .build_venv\Scripts\activate.bat
python -m pip install --disable-pip-version-check -r requirements.txt PyInstaller==6.16.0
if errorlevel 1 goto failed
python -m unittest -v test_measurement_ocr_v2 test_regressions_v22
if errorlevel 1 goto failed
pyinstaller --noconfirm --clean DisplayMeasurementAutomator.spec
if errorlevel 1 goto failed
start "" "%~dp0dist\DisplayMeasurementAutomator"
echo.
echo Windows 실행 프로그램 제작이 완료되었습니다.
echo dist\DisplayMeasurementAutomator 폴더를 통째로 사용하세요.
pause
exit /b 0

:failed
echo.
echo 제작 중 오류가 발생했습니다. 위 오류 내용을 확인하세요.
pause
exit /b 1
