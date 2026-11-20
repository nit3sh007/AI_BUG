"""State machine: investigate -> fix -> verify, with a capped repair loop. The orchestrator
re-runs the tests itself and never trusts the model's claim that it passed."""
import json
import os
import time
from pathlib import Path

from . import prompts
from .sandbox import Sandbox
from .tools import Toolbox
from .workspace import Workspace

READ_ONLY = ["list_files", "read_file", "search_code", "git_log", "submit_findings"]
FIX_TOOLS = ["list_files", "read_file", "search_code", "write_file", "run_tests", "submit_fix"]


def is_test_file(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return "tests" in parts[:-1] or name.startswith("test_") or name.endswith("_test.py")


def preload_files(ws, findings: dict, max_chars: int = 8000) -> str:
    """Contents of the files the investigator named (plus their matching tests/test_<name>.py), so the fixing step does
    not spend several slow tool calls re-reading them. Re-read from disk each attempt so edits are reflected."""
    root = ws.path.resolve()
    rels: list[str] = []
    for f in (findings or {}).get("relevant_files") or []:
        rel = str(f).replace("\\", "/").split(":")[0].strip().removeprefix("./")
        p = (root / rel)
        if rel and p.is_file() and p.resolve().is_relative_to(root) and ".git" not in p.relative_to(root).parts \
                and rel not in rels:
            rels.append(rel)
            if not is_test_file(rel):
                t = f"tests/test_{Path(rel).stem}.py"
                if (root / t).is_file() and t not in rels:
                    rels.append(t)
    out, left = [], max_chars
    for rel in rels:
        if left <= 200:
            break
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        if len(text) > left:
            text = text[:left] + "\n...(truncated)"
        out.append(f"--- {rel} ---\n{text}")
        left -= len(text)
    return "\n".join(out)


def run_agent(repo: str, issue: str, llm, use_docker: bool = False, max_attempts: int = 3,
              out_dir: str | None = None, log_fn=print, log: list | None = None) -> dict:
    t0 = time.time()
    log = log if log is not None else []  # callers may pass a list subclass to watch tool calls live
    ws = Workspace.create(repo)
    sandbox = Sandbox(ws.path, use_docker=use_docker)
    tb = Toolbox(ws, sandbox)

    base_code, base_out = sandbox.run_tests()
    log_fn(f"[baseline] tests exit code {base_code}")

    log_fn("[investigate] running investigator...")
    findings = llm.run_phase(prompts.INVESTIGATOR, f"BUG REPORT:\n{issue}", tb.tools(READ_ONLY),
                             "submit_findings", int(os.environ.get("INVESTIGATE_STEPS", "12")), log)
    result = {"status": "escalated", "attempts": 0, "findings": findings, "workspace": str(ws.path)}
    if not findings:
        result["reason"] = "Investigator did not reach a conclusion within its step budget."
    else:
        log_fn(f"[investigate] root cause: {findings.get('root_cause')}")
        feedback, last_out = "", ""
        for attempt in range(1, max_attempts + 1):
            result["attempts"] = attempt
            log_fn(f"[fix] attempt {attempt}/{max_attempts}")
            res = llm.run_phase(prompts.CODER, prompts.fix_prompt(issue, findings, feedback, preload_files(ws, findings)),
                                tb.tools(FIX_TOOLS), "submit_fix", int(os.environ.get("FIX_STEPS", "20")), log)
            code, last_out = sandbox.run_tests()
            changed = ws.changed_files()
            if res is None:
                feedback = "You never called submit_fix within the step budget."
            elif not changed:
                feedback = "No files were changed."
            elif code != 0:
                feedback = f"The full test suite fails after your change:\n{last_out}"
            elif not any(is_test_file(f) for f in changed):
                feedback = "No regression test was added or changed. Add one under tests/."
            else:
                result.update(status="fixed", fix_summary=res.get("summary", ""))
                break
            log_fn(f"[fix] rejected: {feedback.splitlines()[0]}")
        result["final_test_output"] = last_out
        if result["status"] != "fixed":
            result["reason"] = feedback

    result["patch"] = ws.diff()
    result["changed_files"] = ws.changed_files()
    result["seconds"] = round(time.time() - t0, 1)
    result["tokens"] = getattr(llm, "total_tokens", 0)
    result["tool_calls"] = len(log)
    if out_dir:
        write_outputs(Path(out_dir), issue, result, log)
    return result


def write_outputs(out: Path, issue: str, r: dict, log: list):
    out.mkdir(parents=True, exist_ok=True)
    (out / "patch.diff").write_text(r["patch"] + "\n", encoding="utf-8")
    (out / "run_log.json").write_text(json.dumps(log, indent=2, default=str), encoding="utf-8")
    f = r.get("findings") or {}
    md = [f"# Bug Resolution Report\n",
          f"**Status:** {r['status'].upper()}  |  **Attempts:** {r['attempts']}  |  "
          f"**Time:** {r['seconds']}s  |  **Tokens:** {r['tokens']}  |  **Tool calls:** {r['tool_calls']}\n",
          "## Bug report\n", issue.strip() + "\n",
          "## Root cause\n", str(f.get("root_cause", "n/a")) + "\n",
          "## Evidence\n", str(f.get("evidence", "n/a")) + "\n",
          "## Fix\n", str(r.get("fix_summary") or r.get("reason") or "n/a") + "\n",
          "## Files changed\n", "\n".join(f"- {p}" for p in r["changed_files"]) or "none", "",
          "## Final test output\n", "```\n" + r.get("final_test_output", "") + "\n```\n",
          "## Patch\n", "```diff\n" + r["patch"] + "\n```\n",
          "**Human approval required before merging.**\n"]
    (out / "report.md").write_text("\n".join(md), encoding="utf-8")
