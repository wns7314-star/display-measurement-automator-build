@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "DMA_ROOT=%~dp0"
set "DMA_RESULT=20"
set "DMA_BAD=0"
set "DMA_LOG=%DMA_ROOT%DMA_DIAGNOSTIC_%RANDOM%_%RANDOM%.txt"
echo Display Measurement Automator v2.2.0 diagnostics >"%DMA_LOG%" 2>nul
if not errorlevel 1 goto log_ready
set "DMA_LOG=%TEMP%\DMA_DIAGNOSTIC_%RANDOM%_%RANDOM%.txt"
echo Display Measurement Automator v2.2.0 diagnostics >"%DMA_LOG%" 2>nul
if not errorlevel 1 goto log_ready
echo 로그를 저장할 수 없습니다. 폴더 및 임시 폴더의 쓰기 권한을 확인하세요.
if /i not "%~1"=="--no-pause" pause
exit /b 21

:log_ready
echo [ENVIRONMENT]>>"%DMA_LOG%"
ver >>"%DMA_LOG%"
echo APP_FOLDER="%DMA_ROOT%">>"%DMA_LOG%"
echo PROCESS_ARCH=%PROCESSOR_ARCHITECTURE%>>"%DMA_LOG%"
echo NATIVE_ARCH=%PROCESSOR_ARCHITEW6432%>>"%DMA_LOG%"
echo 이 검사는 설치나 보안 설정 변경 없이 필수 파일과 실행 결과를 기록합니다.
echo 검사 중 오류 창이 나타나면 내용을 캡처한 뒤 창을 닫아 주세요.
echo LOGFILE="%DMA_LOG%"
pushd "%DMA_ROOT%" 2>>"%DMA_LOG%"
if errorlevel 1 goto folder_error

echo [REQUIRED_FILES]>>"%DMA_LOG%"
call :check_file "DisplayMeasurementAutomator.exe"
call :check_file "_internal\python312.dll"
call :check_file "_internal\base_library.zip"
call :check_file "_internal\vcruntime140.dll"
call :check_file "_internal\vcruntime140_1.dll"
call :check_file "_internal\msvcp140.dll"
call :check_file "_internal\MSVCP140_1.dll"
call :check_file "_internal\_tkinter.pyd"
call :check_file "_internal\cv2\cv2.pyd"
call :check_file "_internal\onnxruntime\capi\onnxruntime_pybind11_state.pyd"
call :check_file "_internal\onnxruntime\capi\onnxruntime.dll"
call :check_file "_internal\onnxruntime\capi\onnxruntime_providers_shared.dll"
call :check_file "_internal\rapidocr\models\PP-OCRv6_det_small.onnx"
call :check_file "_internal\rapidocr\models\PP-OCRv6_rec_small.onnx"
call :check_file "_internal\rapidocr\models\ch_ppocr_mobile_v2.0_cls_mobile.onnx"
if "%DMA_BAD%"=="1" goto preflight_failed

echo [MODEL_HASHES]>>"%DMA_LOG%"
where certutil.exe >nul 2>&1
if errorlevel 1 goto hash_unavailable
set "DMA_HASH_TMP=%TEMP%\DMA_HASH_%RANDOM%_%RANDOM%.txt"
call :check_hash "_internal\rapidocr\models\PP-OCRv6_det_small.onnx" "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f"
call :check_hash "_internal\rapidocr\models\PP-OCRv6_rec_small.onnx" "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884"
call :check_hash "_internal\rapidocr\models\ch_ppocr_mobile_v2.0_cls_mobile.onnx" "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c"
if exist "%DMA_HASH_TMP%" del /q "%DMA_HASH_TMP%" >nul 2>&1
if "%DMA_BAD%"=="1" goto preflight_failed
goto run_test

:hash_unavailable
echo HASH_NOT_CHECKED: certutil.exe unavailable; integrity is not verified.>>"%DMA_LOG%"

:run_test
echo [SMOKE_TEST]>>"%DMA_LOG%"
echo 필수 파일 확인 완료. 기존 EXE의 자체검사를 실행합니다. 잠시 기다려 주세요.
start "" /wait "DisplayMeasurementAutomator.exe" --smoke-test >>"%DMA_LOG%" 2>&1
set "DMA_EXIT=%ERRORLEVEL%"
echo EXE_EXIT_CODE=%DMA_EXIT%>>"%DMA_LOG%"
echo EXE_EXIT_CODE=%DMA_EXIT%
if not "%DMA_EXIT%"=="0" goto runtime_failed
echo RESULT=PASS>>"%DMA_LOG%"
echo 자체검사가 통과했습니다. 회사 이미지 정확도까지 검증했다는 뜻은 아닙니다.
set "DMA_RESULT=0"
goto done

:preflight_failed
echo RESULT=PACKAGE_CHECK_FAILED>>"%DMA_LOG%"
echo 필수 파일이 없거나 비어 있거나 OCR 모델이 배포본과 다릅니다.
echo 원본 ZIP 전체를 새 폴더에 다시 압축 해제한 뒤 검사하세요.
set "DMA_RESULT=20"
goto done

:runtime_failed
echo RESULT=EXE_TEST_FAILED>>"%DMA_LOG%"
echo EXE 자체검사가 실패했습니다. 위 종료 코드와 별도로 뜬 오류 창을 확인하세요.
echo 종료 코드 1만으로는 DLL 문제와 OCR 판독 불일치를 구분할 수 없습니다.
set "DMA_RESULT=10"
goto done

:folder_error
echo RESULT=APP_FOLDER_INACCESSIBLE>>"%DMA_LOG%"
echo 프로그램 폴더에 접근할 수 없습니다.
set "DMA_RESULT=21"
goto finish

:done
popd
:finish
echo.
echo LOGFILE="%DMA_LOG%"
echo 로그의 RESULT, EXE_EXIT_CODE, MISSING_FILE, EMPTY_FILE, HASH_MISMATCH 줄을 확인하세요.
if /i not "%~1"=="--no-pause" pause
endlocal & exit /b %DMA_RESULT%

:check_file
if not exist "%~1" goto file_missing
for %%F in ("%~1") do if %%~zF==0 goto file_empty
echo FILE_OK: %~1>>"%DMA_LOG%"
exit /b 0
:file_missing
echo MISSING_FILE: %~1>>"%DMA_LOG%"
set "DMA_BAD=1"
exit /b 0
:file_empty
echo EMPTY_FILE: %~1>>"%DMA_LOG%"
set "DMA_BAD=1"
exit /b 0

:check_hash
certutil.exe -hashfile "%~1" SHA256 >"%DMA_HASH_TMP%" 2>&1
if errorlevel 1 goto hash_error
findstr /i /x /c:"%~2" "%DMA_HASH_TMP%" >nul
if errorlevel 1 goto hash_mismatch
echo HASH_OK: %~1>>"%DMA_LOG%"
exit /b 0
:hash_error
echo HASH_NOT_CHECKED: %~1>>"%DMA_LOG%"
type "%DMA_HASH_TMP%">>"%DMA_LOG%"
exit /b 0
:hash_mismatch
echo HASH_MISMATCH: %~1>>"%DMA_LOG%"
set "DMA_BAD=1"
exit /b 0
