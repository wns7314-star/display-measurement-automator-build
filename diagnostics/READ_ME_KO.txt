디스플레이 측정값 자동 정리 v2.2.0 - 자체검사 실패 진단

이 파일은 v2.2.0 원본 배포본의 장애 원인을 좁히는 보조 도구입니다.
문제를 자동으로 고치거나 OCR 정확도를 높이는 업데이트는 아닙니다.

사용 순서
1. 진단 ZIP의 압축을 풉니다.
2. DIAGNOSE_SELF_TEST.bat을 기존 DisplayMeasurementAutomator.exe와 같은 폴더에 복사합니다.
3. DIAGNOSE_SELF_TEST.bat을 더블클릭합니다. Python이나 Tesseract를 설치할 필요가 없습니다.
4. 별도 오류 창이 나오면 화면을 캡처한 뒤 창을 닫습니다. 닫아야 검사가 끝날 수 있습니다.
5. 콘솔에 LOGFILE=로 표시된 텍스트 파일을 엽니다. 보통 프로그램 폴더에 생성되고,
   그곳에 쓸 수 없으면 Windows 임시 폴더에 생성됩니다.
6. RESULT, EXE_EXIT_CODE, MISSING_FILE, EMPTY_FILE, HASH_MISMATCH 줄과 별도 오류 창을 전달하세요.

기록의 뜻
MISSING_FILE / EMPTY_FILE: 필요한 파일이 없거나 비어 있습니다. 원본 ZIP 전체를 다시 압축 해제합니다.
HASH_MISMATCH: OCR 모델이 원본 v2.2.0의 모델과 다릅니다. 손상, 다른 버전 혼용 등을 확인합니다.
HASH_NOT_CHECKED: Windows 파일 검증 명령이 없거나 실행이 제한되어 모델 무결성 검사를 못했습니다.
EXE_EXIT_CODE=0 / RESULT=PASS: 이 PC에서 이번 자체검사가 통과했습니다.
EXE_EXIT_CODE가 0이 아님: EXE 실행 또는 내부 OCR/Excel 시험이 실패했습니다.
EXE_EXIT_CODE=1만으로는 원인이 확정되지 않습니다. 별도 오류 창과 EXE 직접 실행 결과가 필요합니다.
RESULT=PACKAGE_CHECK_FAILED: 누락/빈 파일/모델 불일치가 있어서 EXE는 실행하지 않았습니다.
RESULT=APP_FOLDER_INACCESSIBLE: 프로그램 폴더 접근 권한을 확인해야 합니다.

검사 범위와 제한
- 필수 파일 15개의 존재/빈 파일 여부, OCR 모델 3개의 SHA-256, Windows 버전/프로세스 아키텍처,
  기존 EXE 자체검사의 실제 종료 코드를 기록합니다. 모든 배포 파일의 해시를 검증하는 도구는 아닙니다.
- 기존 v2.2.0 EXE는 OCR 정답 불일치의 상세 내역을 기록하지 않습니다.
  이 보조 도구만으로 모든 내부 실패를 구분할 수는 없습니다.
- 파일 검증에 Windows certutil을 사용합니다. 사용할 수 없으면 미검사로 명시합니다.
- 관리자 권한, 설치, 인터넷, 보안 설정 변경을 요구하지 않습니다.
- 사용자 분석 이미지는 열지 않습니다. EXE 자체검사가 만드는 예제 이미지만 사용합니다.
- 로그에 실행 폴더 경로가 포함됩니다. 회사명/사용자명 등 민감한 부분은 가리고 필요한 줄만 공유해도 됩니다.
- 회사 정책으로 실행이 제한되면 IT 담당자에게 기록을 전달하세요. 보안 프로그램을 끄지 마세요.

먼저 확인할 것
- EXE 하나만 전달하면 안 됩니다. 원본 ZIP 전체를 새 폴더에 풀어 _internal 폴더와 함께 실행합니다.
- EXE를 직접 더블클릭했을 때 창이 열리는지, 구체적인 DLL 오류가 뜨는지 확인합니다.
- v2.2.0 EXE는 Windows x64 실행 파일입니다. ARM64/구형 Windows에서는 별도 호환성 확인이 필요합니다.
- 배포 ZIP에는 Python, OCR 모델 및 주요 MSVC DLL이 포함됩니다.
- ONNX Runtime은 Windows에서 Visual C++ 런타임을 요구하지만,
  이 메시지만으로 런타임 미설치가 원인이라고 단정할 수 없습니다.
  DLL 오류를 확인한 뒤 회사 IT 담당자가 공식 x64 런타임 설치/복구 필요성을 판단해야 합니다.

공식 근거
ONNX Runtime 설치 조건: https://onnxruntime.ai/docs/install/
Microsoft Visual C++ 재배포 패키지: https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist
PyInstaller 배포 방식: https://pyinstaller.org/en/stable/operating-mode.html
