@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist "DisplayMeasurementAutomator.exe" (
  echo DisplayMeasurementAutomator.exe 파일이 없습니다.
  pause
  exit /b 1
)

start "" /wait "DisplayMeasurementAutomator.exe" --smoke-test
if errorlevel 1 (
  echo 실행 파일 자체검사에 실패했습니다.
  pause
  exit /b 1
)

echo OCR 모델과 실행 파일 자체검사가 통과했습니다.
pause
exit /b 0
