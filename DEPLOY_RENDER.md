# Two repos, one flow

```
D:\2026\
  demo_app\   -> GitHub repo "demo_app"  -> deployed on Render (public)
  AI-BUG\     -> GitHub repo "bug-agent" (private) -> runs on your laptop only
```
```
 Render (public)                          Your laptop
 ┌──────────────┐   outbox of errors      ┌─────────────────────────────┐
 │ acme-shop    │ <─── pull every 10s ─── │ AI-BUG/dashboard/server.py  │
 │ (demo_app)   │      (secret token)     │ + agent (Groq/Gemini/...)   │
 └──────────────┘                         └─────────────────────────────┘
        ▲                                      │ approve → file changed in ..\demo_app
        └──── git push → Render redeploys ─────┘   (manual, or AUTO_PUSH=1)
```
Nothing on your laptop is exposed. The dashboard calls Render, never the other way round.

The two repos only talk over HTTP (`/_autofix/errors` + token) and through the shop's git remote.
No code is shared, so each can change on its own.

## 1. Check it locally first (no Render yet)
```
cd D:\2026\AI-BUG
pip install -r requirements.txt
python scripts\test_remote_poll.py      # must end with: REMOTE MODE TEST OK
python dashboard\server.py              # terminal 1  -> http://127.0.0.1:8000
python ..\demo_app\webapp.py            # terminal 2  -> http://127.0.0.1:5001
```
Press **Sign in** on 5001 (it crashes), watch the incident on 8000, Approve, press **Sign in** again.

## 2. Push the two repos separately
Shop (already done if you followed the earlier steps):
```
cd D:\2026\demo_app
git status            # no .env, no __pycache__
git push
```
Agent (new private repo, e.g. `bug-agent`):
```
cd D:\2026\AI-BUG
git init -b main
git add .
git status            # no .env, no dashboard\incidents.db, no out folders
git commit -m "Bug agent + dashboard"
git remote add origin https://github.com/nit3sh007/bug-agent.git
git push -u origin main
```

## 3. Render (shop repo only)
New → Web Service → `nit3sh007/demo_app` (or New → Blueprint, which reads the `render.yaml` in that repo).
Root Directory empty. Build `pip install -r requirements.txt`.
Start `gunicorn webapp:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT`. Health check `/healthz`.
Env var `AUTOFIX_TOKEN` = `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
Keep `--workers 1`: errors wait in an in-memory outbox.

## 4. Point the local dashboard at Render
`AI-BUG\.env` (see `.env.example`):
```
REMOTE_APP_URL=https://acme-shop-xxxx.onrender.com
AUTOFIX_TOKEN=same_value_as_on_render
AUTO_PUSH=1
```
Run `python dashboard\server.py`. It prints `Pulling errors from ...`.

## Things to know
- Free tier sleeps after ~15 min idle (wakes in up to a minute). Dashboard polling keeps it awake and uses free hours.
- After an approval the same error is ignored for 5 min while Render redeploys (`DEPLOY_GRACE_SECONDS`).
- Reset demo restores the shop repo to its git tag `buggy-baseline` (files added since are removed, `.git` is kept).
  With `AUTO_PUSH=1` it commits and pushes the restore too. Without the tag it refuses and tells you the command.
- Never press Reset demo when `DEMO_REPO` points at a real app that has no `buggy-baseline` tag.

## Adding pages or bugs (all in the shop repo, nothing in AI-BUG)
1. In `demo_app`: add the module under `app/`, a route + page in `webapp.py`, and a passing happy-path test in `tests/`.
2. `python -m pytest` must pass. Commit.
3. Mark it as the new buggy baseline: `git tag -f buggy-baseline` then `git push` and `git push -f origin buggy-baseline`.
4. Render redeploys on push. Reset demo now restores to this version.
- Never expose the dashboard with a tunnel (ngrok etc.).
- Troubleshooting: `HTTP 403` = tokens differ. `HTTP 404` = `AUTOFIX_TOKEN` missing on Render or wrong URL.
  `timed out` after idle = the free instance is waking up, it retries by itself.
