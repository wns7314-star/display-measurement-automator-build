@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>&1
if errorlevel 1 goto use_python
py -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto failed
py -m unittest -v test_measurement_ocr_v2 test_regressions_v22
if errorlevel 1 goto failed
py benchmark_120_points.py
if errorlevel 1 goto failed
goto passed

:use_python
python -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto failed
python -m unittest -v test_measurement_ocr_v2 test_regressions_v22
if errorlevel 1 goto failed
python benchmark_120_points.py
if errorlevel 1 goto failed

:passed
echo.
echo 모든 자체검사가 통과했습니다.
pause
exit /b 0

:failed
echo.
echo 자체검사에 실패했습니다. 위 오류 내용을 확인하세요.
pause
exit /b 1
