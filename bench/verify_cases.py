"""Sanity-check the benchmark itself (no LLM needed). For every case:
  1. the buggy repo's own tests pass (so the bug is 'hidden' from the existing suite),
  2. the hidden test FAILS on the buggy repo (so the bug is real),
  3. the hidden test PASSES once the reference fix is applied (so the case is solvable)."""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def pytest_rc(cwd, target=None):
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"] + ([target] if target else [])
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True).returncode


def main():
    ok_all = True
    for case in sorted((Path(__file__).parent / "cases").iterdir()):
        if not case.is_dir():
            continue
        cfg = json.loads((case / "case.json").read_text())
        repo = (case / cfg["repo"]).resolve()
        tmp = Path(tempfile.mkdtemp()) / "repo"
        shutil.copytree(repo, tmp, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(case / "hidden_test.py", tmp / "tests" / "_hidden_check.py")
        base = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--ignore=tests/_hidden_check.py"],
            cwd=tmp, capture_output=True, text=True).returncode
        buggy = pytest_rc(tmp, "tests/_hidden_check.py")
        shutil.copytree(case / "reference_fix", tmp, dirs_exist_ok=True)
        fixed = pytest_rc(tmp, "tests/_hidden_check.py")
        good = base == 0 and buggy != 0 and fixed == 0
        ok_all &= good
        print(f"{case.name:26} existing-tests-pass={base == 0}  hidden-fails-on-bug={buggy != 0}  "
              f"hidden-passes-with-fix={fixed == 0}  {'OK' if good else 'PROBLEM'}")
    print("\nAll cases valid." if ok_all else "\nSome cases are invalid.")
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
