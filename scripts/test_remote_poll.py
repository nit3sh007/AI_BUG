"""Integration test for hosted mode (no LLM, no internet): runs the demo shop on a local port with AUTOFIX_TOKEN set,
makes it fail, and checks the dashboard pulls, de-duplicates, survives an app restart, respects the deploy grace
window, and can commit+push an approved fix to a git remote."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHOP = Path(os.environ.get("DEMO_REPO", ROOT.parent / "demo_app")).resolve()  # the shop repo, next to this one
tmp = Path(tempfile.mkdtemp())
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
TOKEN = "test-token-123"
# Hermetic: never read the developer's real .env (it holds the live REMOTE_APP_URL, AUTOFIX_TOKEN, AUTO_PUSH...)
os.environ.update(DOTENV_PATH=str(tmp / "no-such.env"), AUTO_PUSH="0")
os.environ.update(AUTOFIX_TOKEN=TOKEN, REMOTE_APP_URL=f"http://127.0.0.1:{port}", DASHBOARD_DB=str(tmp / "t.db"),
                  DEPLOY_GRACE_SECONDS="300", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                  GIT_COMMITTER_EMAIL="t@t")
(tmp / "repo").mkdir()
os.environ["DEMO_REPO"] = str(tmp / "repo" / "demo_app")
(tmp / "repo" / "demo_app").mkdir()

sys.path.insert(0, str(SHOP))
os.chdir(SHOP)
import webapp                                   # noqa: E402
from werkzeug.serving import make_server        # noqa: E402

srv = make_server("127.0.0.1", port, webapp.app, threaded=True)
threading.Thread(target=srv.serve_forever, daemon=True).start()
sys.path.insert(0, str(ROOT / "dashboard"))
import server                                   # noqa: E402

server.init_db()
base = f"http://127.0.0.1:{port}"


def call(path, body=None, token=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["X-Autofix-Token"] = token
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw)
            except ValueError:
                return r.status, raw.decode()
    except urllib.error.HTTPError as e:
        return e.code, None


def crash():
    assert call("/api/login", {"email": "Alice@Example.com", "password": "secret123"})[0] == 500
    time.sleep(0.05)


def count(sql, *a):
    return server.q(sql, a, one=True)[0]


assert call("/_autofix/errors")[0] == 403 and call("/_autofix/errors", token="wrong")[0] == 403
assert call("/healthz") == (200, "ok")
crash()
code, data = call("/_autofix/errors?after=0", token=TOKEN)
tb = data["errors"][0]["traceback"].replace("\\", "/")  # Windows tracebacks use backslashes
assert code == 200 and len(data["errors"]) == 1 and "app/auth.py" in tb, data
assert str(SHOP).replace("\\", "/") not in tb  # the host's folder path is stripped from the traceback
print("outbox + token auth OK")

assert server.poll_remote_once() == 1 and count("SELECT COUNT(*) FROM incidents") == 1
assert server.poll_remote_once() == 0 and count("SELECT COUNT(*) FROM incidents") == 1
print("pull + cursor OK")

crash(); server.poll_remote_once()
assert count("SELECT COUNT(*) FROM incidents") == 1 and count("SELECT occurrences FROM incidents") == 2
print("duplicate absorbed OK")

# app restart: outbox numbering starts over, boot id changes
webapp._seq = 0; webapp._outbox.clear(); webapp.BOOT_ID = "newboot"
crash(); assert server.poll_remote_once() == 1
assert count("SELECT occurrences FROM incidents") == 3
print("app restart handled OK")

# approved fix still deploying -> not re-investigated; after the grace window -> new incident
server.x("UPDATE incidents SET status='approved'")
server.x("INSERT INTO fixes(incident_id, ts, title, root_cause, summary, files, diff) VALUES(1, ?, 't','r','s','[]','')",
         (time.time(),))
crash(); server.poll_remote_once()
assert count("SELECT COUNT(*) FROM incidents") == 1 and count("SELECT occurrences FROM incidents") == 4
server.x("UPDATE fixes SET ts=?", (time.time() - 1000,))
crash(); server.poll_remote_once()
assert count("SELECT COUNT(*) FROM incidents") == 2
print("deploy grace window OK")

# wrong token surfaces a clear error
server.AUTOFIX_TOKEN = "bad"
try:
    server.poll_remote_once(); raise SystemExit("expected failure")
except urllib.error.HTTPError as e:
    assert e.code == 403
server.AUTOFIX_TOKEN = TOKEN

# git publish: commit + push only the approved files to a remote
run = lambda *a, cwd=tmp: subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True)
run("git", "init", "--bare", "-b", "main", "remote.git")
repo = tmp / "repo"
run("git", "init", "-b", "main", cwd=repo)
run("git", "remote", "add", "origin", str(tmp / "remote.git"), cwd=repo)
(repo / "demo_app" / "a.py").write_text("x = 1\n"); (repo / "other.txt").write_text("keep out\n")
run("git", "add", "demo_app/a.py", cwd=repo); run("git", "commit", "-m", "init", cwd=repo)
run("git", "push", "-u", "origin", "main", cwd=repo)
(repo / "demo_app" / "a.py").write_text("x = 2\n"); (repo / "other.txt").write_text("changed but unrelated\n")
server.git_publish(None, "autofix #1: test", [repo / "demo_app" / "a.py"], wait=True)
shown = run("git", "--git-dir", "remote.git", "show", "main:demo_app/a.py").stdout
assert shown.strip() == "x = 2", shown
changed = run("git", "--git-dir", "remote.git", "show", "--stat", "--format=%s", "main").stdout
assert "a.py" in changed and "other.txt" not in changed, changed
print("git push OK (only approved files)")

# Reset demo: the shop repo is the only copy of the shop; Reset restores its git tag "buggy-baseline"
shop = tmp / "shop"; shop.mkdir()
run("git", "init", "-b", "main", cwd=shop)
(shop / "app").mkdir(); (shop / "webapp.py").write_text("buggy\n"); (shop / "app" / "orders.py").write_text("bug\n")
run("git", "add", "-A", cwd=shop); run("git", "commit", "-m", "buggy", cwd=shop); run("git", "tag", "buggy-baseline", cwd=shop)
(shop / "app" / "orders.py").write_text("fixed\n"); (shop / "app" / "new.py").write_text("x\n"); (shop / "webapp.py").unlink()
run("git", "add", "-A", cwd=shop); run("git", "commit", "-m", "fix + new file + deleted file", cwd=shop)
run("git", "init", "--bare", "-b", "main", "shop_remote.git")
run("git", "remote", "add", "origin", str(tmp / "shop_remote.git"), cwd=shop); run("git", "push", "-u", "origin", "main", cwd=shop)
server.DEMO_DIR = shop
client = server.app.test_client()
server.BASELINE = "no-such-tag"
r = client.post("/api/demo/reset")
assert r.status_code == 400 and "git tag no-such-tag" in r.get_json()["error"], r.get_json()
assert (shop / "app" / "orders.py").read_text() == "fixed\n"      # nothing was touched
server.BASELINE = "buggy-baseline"
assert client.post("/api/demo/reset").status_code == 200
assert (shop / "app" / "orders.py").read_text() == "bug\n" and (shop / "webapp.py").read_text() == "buggy\n"
assert not (shop / "app" / "new.py").exists() and (shop / ".git").is_dir()
print("reset restores baseline OK")

# with AUTO_PUSH the restoration is committed and pushed too
server.AUTO_PUSH = True
(shop / "app" / "orders.py").write_text("fixed again\n"); (shop / "app" / "new.py").write_text("x\n")
run("git", "add", "-A", cwd=shop); run("git", "commit", "-m", "fix again", cwd=shop); run("git", "push", cwd=shop)
assert client.post("/api/demo/reset").status_code == 200
for _ in range(100):
    time.sleep(0.1)
    if run("git", "--git-dir", "shop_remote.git", "show", "main:app/orders.py").stdout.strip() == "bug":
        break
files = run("git", "--git-dir", "shop_remote.git", "ls-tree", "-r", "--name-only", "main").stdout.split()
assert run("git", "--git-dir", "shop_remote.git", "show", "main:app/orders.py").stdout.strip() == "bug", files
assert "app/new.py" not in files and "webapp.py" in files, files
print("reset + auto push OK")
print("REMOTE MODE TEST OK")