import argparse
from pathlib import Path

from .llm_factory import make_llm
from .orchestrator import run_agent


def main():
    ap = argparse.ArgumentParser(description="AI Bug Investigation & Resolution Agent")
    ap.add_argument("--repo", required=True, help="path to the repository containing the bug")
    ap.add_argument("--issue", required=True, help="path to a text/markdown file with the bug report")
    ap.add_argument("--out", default="out", help="output directory for patch + report")
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--docker", action="store_true",
                    help="optional: run tests in a Docker container (stronger isolation)")
    a = ap.parse_args()
    r = run_agent(a.repo, Path(a.issue).read_text(), make_llm(), use_docker=a.docker,
                  max_attempts=a.max_attempts, out_dir=a.out)
    print(f"\nStatus: {r['status']} | attempts: {r['attempts']} | {r['seconds']}s | tokens: {r['tokens']}")
    print(f"Report: {a.out}/report.md   Patch: {a.out}/patch.diff")


if __name__ == "__main__":
    main()
