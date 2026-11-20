"""Tool layer exposed to the LLM. Every tool is scoped to the workspace."""
from __future__ import annotations
import os
import re
from dataclasses import dataclass
from typing import Callable, Optional

SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv"}
MAX_READ_CHARS = 20000


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Optional[Callable] = None


S = {"type": "STRING"}
I = {"type": "INTEGER"}


def _obj(props: dict, required=()):
    return {"type": "OBJECT", "properties": props, "required": list(required)}


# Terminal tools end a phase; the orchestrator reads their arguments.
SUBMIT_FINDINGS = Tool(
    "submit_findings",
    "Call once when investigation is complete, with your evidence-backed conclusion.",
    _obj({"root_cause": S,
          "relevant_files": {"type": "ARRAY", "items": S},
          "evidence": S,
          "proposed_fix": S,
          "confidence": {"type": "NUMBER"}},
         ["root_cause", "relevant_files", "evidence", "proposed_fix"]),
)
SUBMIT_FIX = Tool(
    "submit_fix",
    "Call once when the fix and regression test are written and you ran the tests.",
    _obj({"summary": S}, ["summary"]),
)


class Toolbox:
    def __init__(self, ws, sandbox, max_changed_files: int = 5):
        self.ws = ws
        self.root = ws.path.resolve()
        self.sandbox = sandbox
        self.max_changed = max_changed_files
        self._all = {
            "list_files": Tool("list_files", "List files under a directory (relative path).",
                               _obj({"path": S}), self.list_files),
            "read_file": Tool("read_file", "Read a file with line numbers. Optional line range.",
                              _obj({"path": S, "start_line": I, "end_line": I}, ["path"]), self.read_file),
            "search_code": Tool("search_code", "Regex search across the repo; returns file:line: text.",
                                _obj({"pattern": S}, ["pattern"]), self.search_code),
            "git_log": Tool("git_log", "Recent commit history, optionally for one path.",
                            _obj({"path": S}), self.git_log),
            "run_tests": Tool("run_tests", "Run pytest in the sandbox. Optional target path.",
                              _obj({"target": S}), self.run_tests),
            "write_file": Tool("write_file", "Create or overwrite a file in the workspace with full content.",
                               _obj({"path": S, "content": S}, ["path", "content"]), self.write_file),
            "submit_findings": SUBMIT_FINDINGS,
            "submit_fix": SUBMIT_FIX,
        }

    def tools(self, names: list[str]) -> dict[str, Tool]:
        return {n: self._all[n] for n in names}

    def _safe(self, rel: str):
        p = (self.root / (rel or ".")).resolve()
        if not p.is_relative_to(self.root):
            raise ValueError("path escapes workspace")
        if ".git" in p.relative_to(self.root).parts:
            raise ValueError(".git is off limits")
        return p

    def list_files(self, path: str = ".") -> str:
        base = self._safe(path)
        out = []
        for p in sorted(base.rglob("*")):
            rel = p.relative_to(self.root)
            if not p.is_file() or any(x.startswith(".") or x in SKIP_DIRS for x in rel.parts):
                continue
            out.append(str(rel))
            if len(out) >= 300:
                out.append("...truncated")
                break
        return "\n".join(out) or "(empty)"

    def read_file(self, path: str, start_line: int = 1, end_line: int = 0) -> str:
        p = self._safe(path)
        if not p.is_file():
            return f"Not a file: {path}"
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        s = max(int(start_line or 1), 1)
        e = int(end_line) if end_line else len(lines)
        text = "\n".join(f"{i}: {l}" for i, l in enumerate(lines[s - 1:e], start=s))
        return text[:MAX_READ_CHARS] + ("\n...truncated" if len(text) > MAX_READ_CHARS else "")

    def search_code(self, pattern: str) -> str:
        rx = re.compile(pattern)
        hits = []
        for dirpath, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
            for f in files:
                fp = os.path.join(dirpath, f)
                try:
                    for i, line in enumerate(open(fp, encoding="utf-8", errors="strict"), 1):
                        if rx.search(line):
                            hits.append(f"{os.path.relpath(fp, self.root)}:{i}: {line.strip()[:160]}")
                            if len(hits) >= 40:
                                return "\n".join(hits) + "\n...truncated"
                except (UnicodeDecodeError, OSError):
                    continue
        return "\n".join(hits) or "No matches"

    def git_log(self, path: str = "") -> str:
        args = ["log", "--oneline", "-n", "15"]
        if path:
            args += ["--", str(self._safe(path).relative_to(self.root))]
        return self.ws.git(*args) or "(no history)"

    def run_tests(self, target: str = "") -> str:
        code, out = self.sandbox.run_tests(target or None)
        return f"exit code {code}\n{out}"

    def write_file(self, path: str, content: str) -> str:
        p = self._safe(path)
        rel = str(p.relative_to(self.root))
        changed = set(self.ws.changed_files())
        if rel not in changed and len(changed) >= self.max_changed:
            return f"Refused: change limit of {self.max_changed} files reached."
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Wrote {rel} ({len(content)} chars)"
