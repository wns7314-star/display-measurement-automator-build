@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>&1
if errorlevel 1 goto use_python
py START_HERE.py
goto finished

:use_python
python START_HERE.py

:finished
if errorlevel 1 (
  echo.
  echo 실행에 실패했습니다. 위 오류 내용을 확인하세요.
  pause
)
endlocal
