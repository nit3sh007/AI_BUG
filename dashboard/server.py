"""Bug Agent dashboard backend.

Receives runtime errors from the monitored app, turns each new error into a bug report, runs the agent on it
automatically, streams progress to the dashboard page, and applies the patch only after a human approves.

Run:  python dashboard/server.py        then open http://127.0.0.1:8000
"""
import hashlib
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from bugagent.llm_factory import make_llm    # noqa: E402
from bugagent import env as llm_env          # noqa: E402
from bugagent.orchestrator import run_agent  # noqa: E402

# The shop is its own repo, checked out next to this one (D:\2026\demo_app next to D:\2026\AI-BUG). Override with DEMO_REPO.
_default_demo = ROOT.parent / "demo_app" if os.access(ROOT.parent, os.W_OK) else Path("/tmp/demo_app")
DEMO_DIR = Path(os.environ.get("DEMO_REPO", _default_demo)).resolve()   # the live, monitored app
DEMO_REPO_URL = os.environ.get("DEMO_REPO_URL", "https://github.com/nit3sh007/demo_app.git")   # cloned here when DEMO_DIR is missing
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")                     # only needed to push (AUTO_PUSH=1)
BASELINE = os.environ.get("DEMO_BASELINE", "buggy-baseline")   # git tag in the SHOP repo that marks its buggy state
# Hosted mode (e.g. the app runs on Render, this dashboard on your laptop): the dashboard PULLS errors from the app.
REMOTE_APP_URL = os.environ.get("REMOTE_APP_URL", "").rstrip("/")     # e.g. https://acme-shop.onrender.com
AUTOFIX_TOKEN = os.environ.get("AUTOFIX_TOKEN", "")                   # same secret as on the hosted app
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "10"))
DEPLOY_GRACE = int(os.environ.get("DEPLOY_GRACE_SECONDS", "300" if REMOTE_APP_URL else "0"))
AUTO_PUSH = os.environ.get("AUTO_PUSH") == "1"                        # git commit+push after approval
DEMO_URL = os.environ.get("DEMO_URL", REMOTE_APP_URL or "https://demo-app-eatu.onrender.com").rstrip("/")
DB_PATH = Path(os.environ.get("DASHBOARD_DB", HERE / "incidents.db"))
USE_DOCKER = os.environ.get("USE_DOCKER") == "1"

ACTIVE = ("queued", "investigating", "fixing", "ready", "escalated", "failed", "stopped")  # statuses that absorb repeats
app = Flask(__name__)
_lock = threading.RLock()
_jobs: "queue.PriorityQueue[int]" = queue.PriorityQueue()   # lowest incident id first = oldest first
_resume = threading.Event()
_resume.set()
_pause = {"on": False, "reason": "", "since": 0.0, "until": 0.0}
_timer = None


# ---------------------------------------------------------------- storage
def db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _lock, db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS incidents(
            id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT, status TEXT, title TEXT,
            created REAL, updated REAL, started REAL, finished REAL, occurrences INTEGER DEFAULT 1,
            attempt INTEGER DEFAULT 0, error TEXT, issue TEXT, root_cause TEXT, result TEXT);
        CREATE TABLE IF NOT EXISTS fixes(
            id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id INTEGER, ts REAL, title TEXT, root_cause TEXT,
            summary TEXT, files TEXT, diff TEXT);
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id INTEGER, ts REAL, kind TEXT, text TEXT);
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
        """)


def q(sql, args=(), one=False):
    with _lock, db() as c:
        cur = c.execute(sql, args)
        rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def x(sql, args=()):
    with _lock, db() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def add_event(iid: int, kind: str, text: str):
    x("INSERT INTO events(incident_id, ts, kind, text) VALUES(?,?,?,?)", (iid, time.time(), kind, text[:600]))
    x("UPDATE incidents SET updated=? WHERE id=?", (time.time(), iid))


def set_status(iid: int, status: str, **fields):
    sets = ", ".join(f"{k}=?" for k in fields)
    x(f"UPDATE incidents SET status=?, updated=?{', ' + sets if sets else ''} WHERE id=?",
      (status, time.time(), *fields.values(), iid))


# ---------------------------------------------------------------- code snapshots (to detect conflicting fixes)
SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", "node_modules", ".venv", "venv"}


def _sha(path: Path):
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return None


def snapshot(root: Path) -> dict:
    out = {}
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if p.is_file() and not any(part in SKIP_DIRS for part in rel.parts) and p.stat().st_size < 2_000_000:
            out[rel.as_posix()] = _sha(p)
    return out


def queued_since(iid: int) -> float:
    r = q("SELECT MAX(ts) AS t FROM events WHERE incident_id=? AND (text LIKE 'Agent queued%' OR text LIKE 'Queued%' "
          "OR text LIKE 'Re-run requested%' OR text LIKE 'LLM daily limit%' OR text LIKE 'Cannot apply%')", (iid,), one=True)
    return r["t"] or time.time()


# ---------------------------------------------------------------- error -> bug report
def fingerprint(err: dict) -> str:
    frames = re.findall(r'File "([^"]+)", line (\d+)', err.get("traceback", ""))
    where = f"{frames[-1][0]}:{frames[-1][1]}" if frames else err.get("path", "")
    return hashlib.sha1(f"{err.get('error_type')}|{where}".encode()).hexdigest()[:16]


# ---------------------------------------------------------------- zero-LLM triage and fix ledger
def app_frames(err: dict) -> list:
    """Stack-trace frames that point into the monitored app's own code: [(file, line, function)]."""
    out = []
    for f, ln, fn in re.findall(r'File "([^"]+)", line (\d+), in (\S+)', err.get("traceback", "")):
        f = f.replace("\\", "/")
        if not f.startswith("/") and not re.match(r"^[A-Za-z]:", f) and (DEMO_DIR / f).is_file():
            out.append((f, int(ln), fn))
    return out


def replay(err: dict):
    """Re-send the failing request to the monitored app (no LLM). Returns the HTTP status, or None if we cannot
    tell (disabled, body contains redacted values, app unreachable). Set REPLAY_REQUESTS=0 to disable.
    Only use this against apps where re-sending the request is harmless."""
    if os.environ.get("REPLAY_REQUESTS", "1") == "0":
        return None
    body = err.get("request_body") or {}
    if "***" in json.dumps(body):
        return None  # a redacted field means the replay would not be faithful
    method = (err.get("method") or "GET").upper()
    try:
        req = urllib.request.Request(DEMO_URL + err.get("path", "/"), method=method,
                                     data=None if method in ("GET", "HEAD") else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "X-Autofix-Replay": "1"})
        with urllib.request.urlopen(req, timeout=float(os.environ.get("REPLAY_TIMEOUT", "20" if REMOTE_APP_URL else "5"))) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def ledger_note(err: dict):
    """Short notes about earlier approved fixes for the agent. Full diff only when a fix touched a file that is in
    this error's stack trace; otherwise one line per fix. Returns (text, n_fixes, related_ids)."""
    rows = q("SELECT * FROM fixes ORDER BY id DESC LIMIT 5")
    if not rows:
        return "", 0, []
    frame_files = {f for f, _, _ in app_frames(err)}
    lines, related = [], []
    for r in rows:
        files = json.loads(r["files"] or "[]")
        code_files = [f for f in files if "tests/" not in f and not Path(f).name.startswith("test_")]
        lines.append(f"- Fix #{r['incident_id']} ({r['title']}): changed {', '.join(files)}. {r['summary'] or ''}")
        if frame_files & set(code_files):
            related.append(r)
    text = ("\n\nRECENT APPROVED FIXES (already applied to the code you are working on):\n" + "\n".join(lines))
    for r in related[:2]:
        text += (f"\n\nRELATED FIX #{r['incident_id']}: it changed a file that appears in this error's stack trace. "
                 "It may have caused this error or not fully solved it. Check it first. Its diff:\n"
                 + (r["diff"] or "")[:3500])
    return text, len(rows), [r["incident_id"] for r in related]


def build_issue(err: dict) -> str:
    body = json.dumps(err.get("request_body") or {})
    return (f"Runtime error detected automatically in '{err.get('app', 'app')}'.\n\n"
            f"Request: {err.get('method', '?')} {err.get('path', '?')}  body: {body}\n"
            f"Error: {err.get('error_type')}: {err.get('message')}\n\n"
            f"Traceback:\n{err.get('traceback', '').strip()}\n\n"
            + ("START HERE (frames in the app's own code, innermost last): "
               + "; ".join(f"{f}:{ln} in {fn}" for f, ln, fn in app_frames(err)) + ". Open these first.\n\n"
               if app_frames(err) else "") +
            "Find the root cause in the code, fix it with the smallest correct change, and add a regression test. "
            "Decide sensible expected behaviour from the endpoint's purpose. Prefer a fix that makes valid user input "
            "work (e.g. normalise input to match how data is stored) over one that only turns the crash into an "
            "error response; use a 4xx response only when the input is genuinely invalid.")


def ingest(err: dict) -> dict:
    """Turn one error report into an incident (or absorb it as a duplicate) and queue it for the agent."""
    fp = fingerprint(err)
    if DEPLOY_GRACE:  # an approved fix may still be deploying on the host: do not investigate the same error again
        fx = q("SELECT f.ts, f.incident_id FROM fixes f JOIN incidents i ON i.id=f.incident_id "
               "WHERE i.fingerprint=? ORDER BY f.ts DESC LIMIT 1", (fp,), one=True)
        if fx and time.time() - fx["ts"] < DEPLOY_GRACE:
            x("UPDATE incidents SET occurrences=occurrences+1 WHERE id=?", (fx["incident_id"],))
            add_event(fx["incident_id"], "info", "Same error seen again shortly after approval. The fix is probably "
                                                 "still deploying, so it was not investigated again.")
            return dict(incident=fx["incident_id"], duplicate=True)
    existing = q(f"SELECT id FROM incidents WHERE fingerprint=? AND status IN ({','.join('?' * len(ACTIVE))}) "
                 "ORDER BY id DESC", (fp, *ACTIVE), one=True)
    if existing:
        x("UPDATE incidents SET occurrences=occurrences+1, updated=? WHERE id=?", (time.time(), existing["id"]))
        add_event(existing["id"], "info", "Same error happened again (duplicate, not re-investigated).")
        return dict(incident=existing["id"], duplicate=True)
    title = f"{err.get('error_type')} on {err.get('method', '')} {err.get('path', '')}".strip()
    iid = x("INSERT INTO incidents(fingerprint,status,title,created,updated,error,issue) VALUES(?,?,?,?,?,?,?)",
            (fp, "queued", title, time.time(), time.time(), json.dumps(err), build_issue(err)))
    add_event(iid, "detect", f"Error detected: {err.get('error_type')}: {err.get('message')}")
    ahead = q("SELECT COUNT(*) AS n FROM incidents WHERE id<? AND status IN ('queued','investigating','fixing')",
              (iid,), one=True)["n"]
    if _pause["on"]:
        add_event(iid, "info", "Queued. The queue is paused because the LLM daily limit was reached; it starts "
                               "automatically when the queue resumes.")
    elif ahead:
        add_event(iid, "info", f"Queued behind {ahead} other incident{'s' if ahead > 1 else ''}. One incident is "
                               "handled at a time; this one starts automatically when it is next.")
    else:
        add_event(iid, "info", "Queued. Starting now.")
    _jobs.put(iid)
    return dict(incident=iid, duplicate=False)


@app.post("/api/errors")
def ingest_error():
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify(error="local only"), 403
    return jsonify(ingest(request.get_json(force=True)))


# ---------------------------------------------------------------- hosted app: pull errors from its outbox
_remote = {"last_ok": 0.0, "last_error": ""}


def _kv_get(k):
    r = q("SELECT value FROM kv WHERE key=?", (k,), one=True)
    return r["value"] if r else None


def _kv_set(k, v):
    x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))


def poll_remote_once() -> int:
    """Fetch new errors from the hosted app and ingest them. Returns the number of errors received."""
    cur = json.loads(_kv_get("remote_cursor") or "{}")
    after = cur.get("seq", 0) if cur.get("url") == REMOTE_APP_URL else 0
    for _ in range(2):
        req = urllib.request.Request(f"{REMOTE_APP_URL}/_autofix/errors?after={after}",
                                     headers={"X-Autofix-Token": AUTOFIX_TOKEN})
        with urllib.request.urlopen(req, timeout=75) as r:  # a sleeping free instance can take ~1 minute to wake
            data = json.loads(r.read())
        if after and cur.get("boot") and data.get("boot") != cur.get("boot"):
            after = 0  # the hosted app restarted, so its numbering started over: read its outbox from the start
            continue
        break
    errors = sorted(data.get("errors", []), key=lambda e: e.get("seq", 0))
    for e in errors:
        ingest(e)
    top = max([e.get("seq", 0) for e in errors], default=after)
    _kv_set("remote_cursor", json.dumps({"url": REMOTE_APP_URL, "boot": data.get("boot"), "seq": top}))
    return len(errors)


def poll_loop():
    last = None
    while True:
        try:
            poll_remote_once()
            _remote.update(last_ok=time.time(), last_error="")
            err = ""
        except urllib.error.HTTPError as e:
            err = f"hosted app answered HTTP {e.code}" + (" (check AUTOFIX_TOKEN)" if e.code in (401, 403, 404) else "")
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        if err:
            _remote["last_error"] = err
        if err != last and err:
            print(f"[remote] {err}")
        last = err
        time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------- optional: push approved fixes to git (host redeploys)
def _note(iid, kind, text):
    add_event(iid, kind, text) if iid else print(f"[git] {text}")


def git_publish(iid, message: str, paths: list, wait: bool = False):
    """git add + commit + push for these paths, in a background thread. Needs AUTO_PUSH=1, a git repo with a
    configured remote and saved credentials."""
    def run():
        try:
            top = subprocess.run(["git", "-C", str(DEMO_DIR), "rev-parse", "--show-toplevel"],
                                 capture_output=True, text=True, timeout=20)
            if top.returncode:
                return _note(iid, "warn", "Auto-push skipped: the app folder is not inside a git repository.")
            root, rels = top.stdout.strip(), [str(p) for p in paths]
            for step in (["add", "-A", "--", *rels], ["commit", "-m", message, "--", *rels], ["push"]):
                r = subprocess.run(["git", "-C", root, *step], capture_output=True, text=True, timeout=120)
                out = (r.stdout + r.stderr).strip()
                if r.returncode and not (step[0] == "commit" and "nothing to commit" in out.lower()):
                    return _note(iid, "warn", f"git {step[0]} failed: {out[-300:]}")
            _note(iid, "done", "Pushed to git. The host should redeploy automatically (usually a couple of minutes).")
        except Exception as e:
            _note(iid, "warn", f"Auto-push failed: {type(e).__name__}: {e}")
    t = threading.Thread(target=run, daemon=True)
    t.start()
    if wait:
        t.join()
    return t


# ---------------------------------------------------------------- agent worker
class Cancelled(BaseException):
    """Raised inside a running agent when a human pressed Stop. BaseException so `except Exception` in the LLM loop
    cannot swallow it."""


_cancel: set = set()   # incident ids a human asked to stop
_purge: set = set()    # incident ids to delete once their running agent has stopped


def check_cancel(iid):
    if iid in _cancel:
        raise Cancelled()


class LiveLog(list):
    """run_agent appends every tool call here; we turn each one into a timeline event."""
    def __init__(self, iid):
        super().__init__()
        self.iid = iid

    def append(self, entry):
        check_cancel(self.iid)
        super().append(entry)
        a = entry.get("args") or {}
        detail = a.get("path") or a.get("pattern") or a.get("target") or ""
        if entry["tool"] == "submit_findings":
            detail = "root cause reported"
        elif entry["tool"] == "submit_fix":
            detail = "fix submitted"
        add_event(self.iid, "tool", (f"{entry['tool']} {detail}".strip()) + (" (repeat, skipped)" if entry.get("skipped") else ""))


def is_daily_limit(e: Exception) -> bool:
    return getattr(e, "status_code", None) == 429 and bool(re.search(r"per day|\bTPD\b|\bRPD\b", str(e)))


def retry_seconds(e: Exception) -> int:
    m = re.search(r"try again in (?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:([\d.]+)s)?", str(e))
    if m and any(m.groups()):
        h, mi, sec = (float(g) if g else 0 for g in m.groups())
        return int(h * 3600 + mi * 60 + sec) + 5
    return 600


def pause_queue(e: Exception):
    global _timer
    secs = retry_seconds(e)
    _pause.update(on=True, since=time.time(), until=time.time() + secs,
                  reason="The LLM provider's daily limit was reached. All investigations are paused.")
    _resume.clear()
    if _timer:
        _timer.cancel()
    _timer = threading.Timer(secs, resume_queue)
    _timer.daemon = True
    _timer.start()


def resume_queue():
    global _timer
    if _timer:
        _timer.cancel()
        _timer = None
    _pause["on"] = False
    _resume.set()


def _run_incident(iid: int):
    row = q("SELECT * FROM incidents WHERE id=?", (iid,), one=True)
    waited = time.time() - queued_since(iid)
    set_status(iid, "investigating", started=time.time())
    if waited >= 5:
        add_event(iid, "info", f"Agent started after waiting {int(waited // 60)}m {int(waited % 60)}s in the queue.")
    snap = snapshot(DEMO_DIR)  # what the code looked like when this agent started (used to detect conflicts)
    err = json.loads(row["error"] or "{}")
    check_cancel(iid)
    status = replay(err)
    check_cancel(iid)
    if status is not None and status < 500:
        add_event(iid, "info", f"Replayed the failing request: it now returns HTTP {status}, so it no longer fails.")
        set_status(iid, "resolved", finished=time.time(), result=json.dumps(
            {"reason": f"Replaying the failing request now returns HTTP {status}. An earlier fix most likely solved "
                       "it, so the LLM was not used.", "seconds": 0, "tokens": 0, "attempts": 0}))
        add_event(iid, "done", "Closed without using the LLM (already fixed).")
        return None
    if status is not None:
        add_event(iid, "info", f"Replayed the failing request: still fails (HTTP {status}). Starting the agent.")
    note, n_fixes, related = ledger_note(err)
    if n_fixes:
        add_event(iid, "info", f"Gave the agent notes on {n_fixes} earlier fix{'es' if n_fixes > 1 else ''}" +
                  (f"; fix #{', #'.join(map(str, related))} touched the same file as this error, so its diff was "
                   "included." if related else "."))
    issue_text = row["issue"] + note

    def on_log(msg: str):
        check_cancel(iid)
        if msg.startswith("[investigate] root cause:"):
            rc = msg.split(":", 1)[1].strip()
            x("UPDATE incidents SET root_cause=? WHERE id=?", (rc, iid))
            add_event(iid, "finding", "Root cause: " + rc)
        elif msg.startswith("[fix] attempt"):
            n = int(re.search(r"attempt (\d+)/", msg).group(1))
            set_status(iid, "fixing", attempt=n)
            add_event(iid, "stage", msg[len("[fix] "):].capitalize())
        elif msg.startswith("[fix] rejected"):
            add_event(iid, "warn", "Attempt rejected by verification: " + msg[len("[fix] rejected: "):])
        elif msg.startswith("[investigate]"):
            add_event(iid, "stage", "Investigating the codebase")
        elif msg.startswith("[baseline]"):
            code = msg.rsplit(" ", 1)[-1]
            add_event(iid, "info", "Ran the existing tests on an isolated copy of the app first: " +
                      ("all pass, so the bug is not covered by tests." if code == "0" else "some tests already fail."))

    try:
        llm = make_llm()
        r = run_agent(str(DEMO_DIR), issue_text, llm, use_docker=USE_DOCKER, log_fn=on_log, log=LiveLog(iid))
        keep = {k: r.get(k) for k in ("status", "attempts", "findings", "fix_summary", "reason", "patch",
                                      "changed_files", "final_test_output", "seconds", "tokens", "tool_calls",
                                      "workspace")}
        keep["base_hashes"] = {f: snap.get(f) for f in (r.get("changed_files") or [])}
        if r["status"] == "fixed":
            set_status(iid, "ready", finished=time.time(), result=json.dumps(keep, default=str))
            add_event(iid, "done", "Fix verified (tests pass, regression test added). Waiting for human approval.")
        else:
            set_status(iid, "escalated", finished=time.time(), result=json.dumps(keep, default=str))
            add_event(iid, "warn", "Could not produce a verified fix. Escalated to a human.")
    except Exception as e:  # e.g. rate limit, missing key
        if is_daily_limit(e):
            pause_queue(e)
            set_status(iid, "queued", started=None, attempt=0)
            add_event(iid, "warn", "LLM daily limit reached. Everything is paused and this incident will resume "
                                   "automatically (or press Resume).")
            return "paused"
        set_status(iid, "failed", finished=time.time(), result=json.dumps({"reason": f"{type(e).__name__}: {e}"}))
        add_event(iid, "warn", f"Agent run failed: {type(e).__name__}: {str(e)[:300]}")


def run_incident(iid: int):
    try:
        return _run_incident(iid)
    except Cancelled:
        if iid in _purge:
            remove_incident(iid)
        else:
            set_status(iid, "stopped", finished=time.time(), result=json.dumps(
                {"reason": "Stopped by a human before the agent finished.", "seconds": 0, "tokens": 0, "attempts": 0}))
            add_event(iid, "warn", "Stopped by a human.")
        return None
    finally:
        _cancel.discard(iid)
        _purge.discard(iid)


def remove_incident(iid: int):
    x("DELETE FROM events WHERE incident_id=?", (iid,))
    x("DELETE FROM incidents WHERE id=?", (iid,))


def worker():
    while True:
        iid = _jobs.get()
        _resume.wait()
        row = q("SELECT status FROM incidents WHERE id=?", (iid,), one=True)
        if not row or row["status"] != "queued":   # deleted, stopped or already handled while it waited in the queue
            continue
        try:
            if run_incident(iid) == "paused":
                _jobs.put(iid)
        except Exception as e:
            print("worker error:", e)


# ---------------------------------------------------------------- API
def summary(r) -> dict:
    err = json.loads(r["error"] or "{}")
    return {"id": r["id"], "status": r["status"], "title": r["title"], "created": r["created"],
            "updated": r["updated"], "occurrences": r["occurrences"], "attempt": r["attempt"],
            "message": err.get("message", ""), "path": err.get("path", "")}


@app.get("/api/state")
def state():
    rows = q("SELECT * FROM incidents ORDER BY id DESC")
    done = [r for r in rows if r["finished"] and r["started"]]
    fixed = [r for r in rows if r["status"] in ("ready", "approved", "resolved")]
    res = [json.loads(r["result"]) for r in done if r["result"]]
    n = len(done)
    m = {"detected": len(rows),
         "auto_fixed": len(fixed),
         "escalated": sum(r["status"] in ("escalated", "failed") for r in rows),
         "in_progress": sum(r["status"] in ("queued", "investigating", "fixing") for r in rows),
         "approved": sum(r["status"] == "approved" for r in rows),
         "fix_rate": round(100 * len(fixed) / n) if n else None,
         "avg_seconds": round(sum(r["finished"] - r["started"] for r in done) / n) if n else None,
         "avg_tokens": round(sum((x_.get("tokens") or 0) for x_ in res) / n) if n else None}
    running = [r["id"] for r in rows if r["status"] in ("investigating", "fixing")]
    inc = []
    for r in rows:
        d = summary(r)
        if r["status"] == "queued":
            d["ahead"] = sum(1 for o in rows if o["status"] in ("queued", "investigating", "fixing") and o["id"] < r["id"])
        inc.append(d)
    return jsonify(metrics=m, incidents=inc, demo_url=DEMO_URL, paused=dict(_pause) if _pause["on"] else None,
                   running=running,
                   remote=dict(url=REMOTE_APP_URL, **_remote) if REMOTE_APP_URL else None,
                   model=os.environ.get("LLM_MODEL") or os.environ.get("LLM_PROVIDER", "gemini"))


@app.get("/api/incidents/<int:iid>")
def incident(iid):
    r = q("SELECT * FROM incidents WHERE id=?", (iid,), one=True)
    if not r:
        return jsonify(error="not found"), 404
    d = summary(r)
    older = q("SELECT id, status FROM incidents WHERE id<? AND status IN ('queued','investigating','fixing') "
              "ORDER BY id", (iid,))
    d.update(ahead=len(older) if r["status"] == "queued" else 0,
             waiting_for=next((o["id"] for o in older if o["status"] in ("investigating", "fixing")), None),
             error=json.loads(r["error"] or "{}"), issue=r["issue"], root_cause=r["root_cause"],
             result=json.loads(r["result"]) if r["result"] else None,
             started=r["started"], finished=r["finished"], stopping=iid in _cancel,
             events=[dict(e) for e in q("SELECT ts, kind, text FROM events WHERE incident_id=? ORDER BY id", (iid,))])
    return jsonify(d)


@app.post("/api/incidents/<int:iid>/approve")
def approve(iid):
    r = q("SELECT * FROM incidents WHERE id=?", (iid,), one=True)
    if not r or r["status"] != "ready":
        return jsonify(error="incident is not waiting for approval"), 400
    res = json.loads(r["result"])
    ws = Path(res.get("workspace") or "")
    if not ws.is_dir():
        return jsonify(error="agent workspace no longer exists; use Re-run"), 410
    for rel in res["changed_files"]:
        p = Path(rel)
        if p.is_absolute() or ".." in p.parts or ".git" in p.parts:
            return jsonify(error=f"unsafe path {rel}"), 400
    base = res.get("base_hashes") or {}
    changed_since = [f for f in res["changed_files"] if f in base and _sha(DEMO_DIR / f) != base[f]]
    if changed_since:
        set_status(iid, "queued", attempt=0, started=None, finished=None, result=None, root_cause=None)
        x("UPDATE incidents SET issue=issue||? WHERE id=?",
          ("\n\nNOTE: the code changed since this error was reported (another fix was applied to "
           + ", ".join(changed_since) + "). Re-check whether the bug still exists and build on the current code.", iid))
        add_event(iid, "warn", "Cannot apply: " + ", ".join(changed_since) + " changed after this fix was made "
                  "(another fix was approved). Re-running the agent on the updated code.")
        _jobs.put(iid)
        return jsonify(error="Another fix changed the same file(s) since this patch was made, so applying it would "
                             "overwrite that fix. The agent is re-running on the updated code.", conflict=True), 409
    for rel in res["changed_files"]:
        dst = DEMO_DIR / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ws / rel, dst)
    set_status(iid, "approved")
    x("INSERT INTO fixes(incident_id, ts, title, root_cause, summary, files, diff) VALUES(?,?,?,?,?,?,?)",
      (iid, time.time(), r["title"], r["root_cause"], (res.get("fix_summary") or "")[:300],
       json.dumps(res["changed_files"]), res.get("patch") or ""))
    add_event(iid, "done", f"Approved by a human. Patch applied to the app ({', '.join(res['changed_files'])}).")
    if AUTO_PUSH:
        add_event(iid, "info", "Committing and pushing the fix to git so the host can redeploy.")
        git_publish(iid, f"autofix #{iid}: {r['title']}", [DEMO_DIR / f for f in res["changed_files"]])
    elif REMOTE_APP_URL:
        add_event(iid, "info", "The hosted app only changes after you commit and push these files "
                               "(or set AUTO_PUSH=1 to do it automatically).")
    return jsonify(ok=True)


@app.post("/api/incidents/<int:iid>/reject")
def reject(iid):
    r = q("SELECT status FROM incidents WHERE id=?", (iid,), one=True)
    if not r or r["status"] not in ("ready", "escalated", "failed"):
        return jsonify(error="nothing to reject"), 400
    set_status(iid, "rejected")
    add_event(iid, "warn", "Rejected by a human. No code was changed.")
    return jsonify(ok=True)


@app.post("/api/incidents/<int:iid>/rerun")
def rerun(iid):
    r = q("SELECT status FROM incidents WHERE id=?", (iid,), one=True)
    if not r or r["status"] not in ("escalated", "failed", "rejected", "stopped"):
        return jsonify(error="cannot re-run now"), 400
    guidance = ((request.get_json(silent=True) or {}).get("guidance") or "").strip()[:1000]
    if guidance:
        x("UPDATE incidents SET issue=issue||? WHERE id=?",
          (f"\n\nHUMAN GUIDANCE (a reviewer rejected the previous fix; follow this): {guidance}", iid))
        add_event(iid, "info", f"Human guidance added: {guidance}")
    set_status(iid, "queued", attempt=0, started=None, finished=None, result=None, root_cause=None)
    add_event(iid, "info", "Re-run requested. Queued; it starts when the agent is free.")
    _jobs.put(iid)
    return jsonify(ok=True)


@app.post("/api/incidents/<int:iid>/stop")
def stop(iid):
    r = q("SELECT status FROM incidents WHERE id=?", (iid,), one=True)
    if not r:
        return jsonify(error="not found"), 404
    if r["status"] == "queued":
        set_status(iid, "stopped", finished=time.time(), result=json.dumps(
            {"reason": "Stopped by a human before it started.", "seconds": 0, "tokens": 0, "attempts": 0}))
        add_event(iid, "warn", "Stopped by a human before it started.")
    elif r["status"] in ("investigating", "fixing"):
        _cancel.add(iid)
        add_event(iid, "warn", "Stop requested. The agent halts after its current step (a few seconds).")
    else:
        return jsonify(error="nothing to stop"), 400
    return jsonify(ok=True)


@app.delete("/api/incidents/<int:iid>")
def delete_incident(iid):
    r = q("SELECT status FROM incidents WHERE id=?", (iid,), one=True)
    if not r:
        return jsonify(ok=True)
    if r["status"] in ("investigating", "fixing"):
        _purge.add(iid)      # removed as soon as the running agent has stopped
        _cancel.add(iid)
    else:
        remove_incident(iid)
    return jsonify(ok=True)


@app.post("/api/resume")
def resume():
    resume_queue()
    return jsonify(ok=True)


@app.post("/api/demo/reset")
def reset_demo():
    """Restore the shop to its buggy baseline (a git tag in the shop repo) and clear history. The shop repo is the
    only copy of the shop code, so new pages and bugs are added there and then tagged."""
    def git(*a):
        return subprocess.run(["git", "-C", str(DEMO_DIR), *a], capture_output=True, text=True, timeout=60)
    with _lock:
        if git("rev-parse", "--verify", "--quiet", f"{BASELINE}^{{commit}}").returncode:
            return jsonify(error=f"No git tag '{BASELINE}' in {DEMO_DIR}. In the shop repo, while its code is in the "
                                 f"buggy state, run: git tag {BASELINE}"), 400
        r = git("read-tree", "-u", "--reset", BASELINE)   # working tree + index := baseline (removes files added since)
        if r.returncode:
            return jsonify(error=f"git read-tree failed: {(r.stdout + r.stderr).strip()[-300:]}"), 500
        x("DELETE FROM events")
        x("DELETE FROM fixes")
        x("DELETE FROM incidents")
    if AUTO_PUSH:
        git_publish(None, "demo reset: restore the buggy baseline", [DEMO_DIR])
    return jsonify(ok=True)


@app.get("/")
def index():
    return send_from_directory(HERE, "index.html")


def ensure_demo_repo():
    """Hosted mode: the agent needs the shop's code on this machine. Clone it if it is not there yet."""
    if (DEMO_DIR / ".git").is_dir() or not REMOTE_APP_URL:
        return
    url = DEMO_REPO_URL
    if GITHUB_TOKEN and url.startswith("https://"):
        url = url.replace("https://", f"https://x-access-token:{GITHUB_TOKEN}@", 1)
    try:
        DEMO_DIR.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "clone", url, str(DEMO_DIR)], capture_output=True, text=True, timeout=180)
        if r.returncode:
            print("WARNING: clone of the shop repo failed:", (r.stdout + r.stderr).replace(GITHUB_TOKEN or "\0", "***")[-300:])
            return
        for k, v in (("user.name", "bug-agent"), ("user.email", "bug-agent@users.noreply.github.com")):
            subprocess.run(["git", "-C", str(DEMO_DIR), "config", k, v], capture_output=True, timeout=20)
        print(f"Cloned shop repo into {DEMO_DIR}")
    except Exception as e:
        print(f"WARNING: could not clone the shop repo: {type(e).__name__}: {e}")


_started = False


def startup():
    """Create tables and start background threads. Runs once, whether launched by gunicorn or `python server.py`."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    init_db()
    ensure_demo_repo()
    # requeue anything that was interrupted by a restart
    for r in q("SELECT id FROM incidents WHERE status IN ('queued','investigating','fixing')"):
        set_status(r["id"], "queued")
        _jobs.put(r["id"])
    threading.Thread(target=worker, daemon=True).start()
    if REMOTE_APP_URL and AUTOFIX_TOKEN:
        threading.Thread(target=poll_loop, daemon=True).start()
        print(f"Pulling errors from {REMOTE_APP_URL} every {POLL_SECONDS:g}s")
    elif REMOTE_APP_URL:
        print("REMOTE_APP_URL is set but AUTOFIX_TOKEN is missing: not polling the hosted app.")
    if not (DEMO_DIR / "webapp.py").is_file():
        print(f"WARNING: no webapp.py in {DEMO_DIR}. Clone the shop repo there or set DEMO_REPO in .env.")
    print(f"LLM:         {llm_env.describe()}")


startup()

if __name__ == "__main__":
    print(f"Dashboard:   http://127.0.0.1:8000\nMonitored app: {DEMO_URL}  (code in {DEMO_DIR})")
    app.run(host="127.0.0.1", port=8000, threaded=True)