"""Controlled execution layer. The LLM can only trigger fixed commands (pytest), never a shell."""
import os
import subprocess
import sys
from pathlib import Path

MAX_OUTPUT = 6000


class Sandbox:
    def __init__(self, workspace: Path, use_docker: bool = False,
                 image: str = "bugagent-sandbox", timeout: int = 120):
        self.ws = Path(workspace).resolve()
        self.use_docker = use_docker
        self.image = image
        self.timeout = timeout

    def run_tests(self, target: str | None = None) -> tuple[int, str]:
        cmd = ["python", "-m", "pytest", "-q", "--no-header", "--maxfail=5", "-p", "no:cacheprovider"]
        if target:
            t = target.strip()
            p = Path(t)
            if t.startswith("-") or p.is_absolute() or ".." in p.parts:
                raise ValueError("invalid test target")
            cmd.append(t)
        return self._run(cmd)

    def _limits(self):
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (self.timeout, self.timeout))
        resource.setrlimit(resource.RLIMIT_FSIZE, (50 * 1024 * 1024, 50 * 1024 * 1024))

    def _run(self, cmd: list[str]) -> tuple[int, str]:
        try:
            if self.use_docker:
                base = ["docker", "run", "--rm", "--network", "none", "--memory", "512m",
                        "--cpus", "1", "--pids-limit", "256",
                        "-v", f"{self.ws}:/work", "-w", "/work",
                        "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1"]
                if hasattr(os, "getuid"):
                    base += ["--user", f"{os.getuid()}:{os.getgid()}"]
                full = base + [self.image] + cmd
                env = None
                pre = None
            else:
                # Local mode: throwaway workspace copy, scrubbed env (API keys are NOT passed on),
                # timeout, and CPU/file-size limits on POSIX. No network or filesystem isolation:
                # run only on repos you trust.
                full = [sys.executable] + cmd[1:]
                keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "VIRTUAL_ENV", "LANG")
                env = {k: os.environ[k] for k in keep if k in os.environ}
                env["PYTHONDONTWRITEBYTECODE"] = "1"
                pre = self._limits if os.name == "posix" else None
            r = subprocess.run(full, cwd=self.ws, capture_output=True, text=True,
                               timeout=self.timeout, env=env, preexec_fn=pre)
            out = (r.stdout + r.stderr).strip()
            return r.returncode, out[-MAX_OUTPUT:]
        except subprocess.TimeoutExpired:
            return 124, f"Timed out after {self.timeout}s"
