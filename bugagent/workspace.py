"""Isolated copy of the target repo. The agent only ever edits this copy."""
import shutil
import subprocess
import tempfile
from pathlib import Path

IGNORE = shutil.ignore_patterns("__pycache__", ".venv", "venv", "node_modules", ".pytest_cache", "*.pyc")


class Workspace:
    def __init__(self, path: Path):
        self.path = Path(path)

    @classmethod
    def create(cls, repo: str) -> "Workspace":
        dst = Path(tempfile.mkdtemp(prefix="bugagent_")) / "repo"
        shutil.copytree(repo, dst, ignore=IGNORE)
        ws = cls(dst)
        if not (dst / ".git").exists():
            ws.git("init", "-q")
        exclude = dst / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with open(exclude, "a") as f:
            f.write("\n__pycache__/\n.pytest_cache/\n*.pyc\n")
        ws.git("add", "-A")
        ws.git("-c", "user.email=agent@local", "-c", "user.name=bugagent",
               "commit", "-q", "--allow-empty", "-m", "bugagent baseline")
        return ws

    def git(self, *args: str) -> str:
        r = subprocess.run(["git", *args], cwd=self.path, capture_output=True, text=True, timeout=60)
        return (r.stdout + r.stderr).strip()

    def diff(self) -> str:
        self.git("add", "-A")
        return self.git("diff", "--cached", "HEAD")

    def changed_files(self) -> list[str]:
        self.git("add", "-A")
        out = self.git("diff", "--cached", "--name-only", "HEAD")
        return [l for l in out.splitlines() if l.strip()]
