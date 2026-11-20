"""Run every case, verify with the hidden test, print a results table."""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bugagent.llm_factory import make_llm    # noqa: E402
from bugagent.orchestrator import run_agent  # noqa: E402
from bugagent.sandbox import Sandbox         # noqa: E402


def run_case(case: Path, llm, use_docker: bool) -> dict:
    cfg = json.loads((case / "case.json").read_text(encoding="utf-8"))
    repo = (case / cfg["repo"]).resolve()
    r = run_agent(str(repo), (case / "issue.md").read_text(encoding="utf-8"), llm, use_docker=use_docker,
                  out_dir=f"bench_out/{case.name}", log_fn=lambda *_: None)
    hidden_ok = False
    if r["status"] == "fixed":
        dst = Path(r["workspace"]) / "tests" / "_hidden_check.py"
        shutil.copy(case / "hidden_test.py", dst)
        hidden_ok = Sandbox(Path(r["workspace"]), use_docker).run_tests("tests/_hidden_check.py")[0] == 0
    files = set((r.get("findings") or {}).get("relevant_files", []))
    return {"case": case.name, "status": r["status"], "hidden_test_pass": hidden_ok,
            "root_cause_file_hit": bool(files & set(cfg["expected_files"])),
            "attempts": r["attempts"], "seconds": r["seconds"], "tokens": r["tokens"]}


def main(use_docker: bool = False):
    llm_cls = make_llm
    rows = []
    only = [a for a in sys.argv[1:] if not a.startswith("--")]  # e.g. python bench\\run_bench.py 002 004
    for case in sorted((Path(__file__).parent / "cases").iterdir()):
        if case.is_dir() and (not only or any(o in case.name for o in only)):
            rows.append(run_case(case, llm_cls(), use_docker))
    print(f"{'case':28}{'status':10}{'correct':9}{'rc-hit':8}{'tries':7}{'sec':7}{'tokens'}")
    for x in rows:
        print(f"{x['case']:28}{x['status']:10}{str(x['hidden_test_pass']):9}"
              f"{str(x['root_cause_file_hit']):8}{x['attempts']:<7}{x['seconds']:<7}{x['tokens']}")
    ok = sum(x["hidden_test_pass"] for x in rows)
    print(f"\nCorrectly fixed: {ok}/{len(rows)}")
    Path("bench_out").mkdir(exist_ok=True)
    Path("bench_out/results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main(use_docker="--docker" in sys.argv)
