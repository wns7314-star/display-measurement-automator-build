"""Run on Windows against the previously delivered v2.2.0 package."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REFERENCE = Path(sys.argv.pop(1)).resolve()
LAUNCHER = Path(__file__).with_name("DIAGNOSE_SELF_TEST.bat")
REQUIRED = re.findall(r'^call :check_file "([^"]+)"', LAUNCHER.read_text(encoding="utf-8"), re.M)
LOGS = Path("diagnostic_test_logs").resolve()
LOGS.mkdir(exist_ok=True)


def run_diagnostic(folder, name, code=None):
    env = os.environ.copy()
    if code is not None:
        env["DMA_TEST_EXIT"] = str(code)
    proc = subprocess.run(["cmd.exe", "/d", "/c", "DIAGNOSE_SELF_TEST.bat", "--no-pause"],
                          cwd=folder, env=env, capture_output=True, timeout=180)
    files = list(folder.glob("DMA_DIAGNOSTIC_*.txt"))
    output = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    if not files:
        raise AssertionError("No diagnostic log: " + output)
    report = max(files, key=lambda p: p.stat().st_mtime_ns).read_text(encoding="utf-8", errors="replace")
    (LOGS / (name + ".txt")).write_text(report + "\nCONSOLE\n" + output, encoding="utf-8")
    return proc.returncode, report


class LauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="DMA_fixture_")
        root = Path(cls.temp.name)
        source = root / "Fixture.cs"
        source.write_text('class Fixture { static int Main() { return int.Parse(System.Environment.GetEnvironmentVariable("DMA_TEST_EXIT")); } }')
        cls.stub = root / "fixture.exe"
        compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        subprocess.run([str(compiler), "/nologo", "/target:exe", "/out:" + str(cls.stub), str(source)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.case = tempfile.TemporaryDirectory(prefix="DMA 한글 & space ! ")
        self.addCleanup(self.case.cleanup)
        self.folder = Path(self.case.name)
        for name in REQUIRED:
            dst = self.folder / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            if name.endswith(".onnx"):
                shutil.copy2(REFERENCE / name, dst)
            else:
                dst.write_bytes(b"fixture")
        shutil.copy2(self.stub, self.folder / "DisplayMeasurementAutomator.exe")
        shutil.copy2(LAUNCHER, self.folder / LAUNCHER.name)

    def check(self, expected, marker, code=0):
        status, log = run_diagnostic(self.folder, self._testMethodName, code)
        self.assertEqual(status, expected, log)
        self.assertIn(marker, log)
        return log

    def test_success_in_unicode_spaces_and_metacharacter_path(self):
        self.check(0, "RESULT=PASS")

    def test_positive_exit_is_failure(self):
        self.check(10, "EXE_EXIT_CODE=1", 1)

    def test_negative_exit_is_not_false_success(self):
        self.check(10, "EXE_EXIT_CODE=-1073741819", -1073741819)

    def test_missing_exe_stops_before_launch(self):
        (self.folder / "DisplayMeasurementAutomator.exe").unlink()
        log = self.check(20, "MISSING_FILE: DisplayMeasurementAutomator.exe")
        self.assertNotIn("EXE_EXIT_CODE=", log)

    def test_missing_model_stops_before_launch(self):
        name = "_internal/rapidocr/models/PP-OCRv6_det_small.onnx"
        (self.folder / name).unlink()
        self.check(20, "MISSING_FILE:")

    def test_empty_dll_stops_before_launch(self):
        (self.folder / "_internal/python312.dll").write_bytes(b"")
        self.check(20, "EMPTY_FILE: _internal\\python312.dll")

    def test_corrupted_model_stops_before_launch(self):
        (self.folder / "_internal/rapidocr/models/PP-OCRv6_rec_small.onnx").write_bytes(b"broken")
        self.check(20, "HASH_MISMATCH:")


class OriginalPackageTests(unittest.TestCase):
    def test_original_delivered_exe_passes(self):
        shutil.copy2(LAUNCHER, REFERENCE / LAUNCHER.name)
        code, log = run_diagnostic(REFERENCE, "original_v220")
        self.assertEqual(code, 0, log)
        self.assertIn("EXE_EXIT_CODE=0", log)
        self.assertEqual(log.count("HASH_OK:"), 3, log)


if __name__ == "__main__":
    unittest.main(verbosity=2)
