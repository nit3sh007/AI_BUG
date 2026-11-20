# AI Bug Investigation & Resolution Agent (MVP)

Bug report in -> investigation -> fix + regression test -> verified in sandbox -> patch + report out.

## Setup
```bash
pip install -r requirements.txt
copy .env.example .env                  # then open .env and paste your key (and model name)
```

## Providers
Default is Gemini. To use Azure (or any OpenAI-compatible endpoint) set `LLM_PROVIDER=azure` and the matching
variables from `.env.example`. Small models (e.g. Phi) may struggle with 20+ tool calls; a GPT-class deployment
is more reliable.

## 1. Check the plumbing (no API key needed)
```bash
python scripts/smoke_test.py            # fake LLM; should print SMOKE TEST OK
python scripts/test_openai_loop.py      # checks the Azure/OpenAI backend loop
```

## 2. Run the agent on the sample app
```bash
python -m bugagent.cli --repo sample_app --issue bench/cases/001_login_case/issue.md --out out
```
Outputs: `out/report.md`, `out/patch.diff`, `out/run_log.json` (every tool call = your evidence trail).

## 3. Benchmark
```bash
python bench/verify_cases.py        # checks the benchmark itself, no LLM needed
python bench/run_bench.py              # all cases (slow on the free tier)
python bench/run_bench.py 002 004      # only cases whose folder name contains these
```
Each case = `issue.md`, `case.json`, and a `hidden_test.py` the agent never sees. A fix only counts as
correct if the hidden test passes, not merely because the agent's own tests pass.

## How it works
- `workspace.py`  isolated copy of the repo, git baseline for diffs
- `tools.py`      list/read/search/git_log/write_file/run_tests, all scoped to the workspace (max 5 files changed)
- `sandbox.py`    only pytest can run, in a throwaway copy of the repo, with a scrubbed environment (your API key
  is not passed to test code), timeout and CPU/file-size limits. Local mode has no network or filesystem
  isolation, so use it on repos you trust. Optional `--docker` adds a no-network container
  (`docker build -t bugagent-sandbox -f Dockerfile.sandbox .`).
- `llm.py`        Gemini function-calling loop
- `orchestrator.py` investigate -> fix -> verify. The orchestrator re-runs the full suite itself, requires a
  regression test, and retries with failure output up to 3 times, then escalates to a human


## Dashboard MVP (error detected -> agent starts automatically)
A demo shop app (its own repo, `demo_app`, checked out next to this folder; has several seeded bugs across 4 pages) reports its runtime errors to the dashboard. Each new error
becomes an incident, the agent investigates and fixes it in an isolated copy, and a human approves the patch.

```bash
pip install -r requirements.txt
python dashboard/server.py        # terminal 1 -> http://127.0.0.1:8000  (the dashboard)
python ../demo_app/webapp.py      # terminal 2 -> http://127.0.0.1:5001  (the buggy "customer app")
```
1. Open both pages. In the demo app click **Sign in** (uppercase email), **Apply coupon** (code WELCOME50),
   **Show average item price**, or use the Checkout (25 kg), Profile (no middle name) and Catalog (toys) pages. Each returns a 500.
2. The dashboard shows "Error detected", then Investigating, Fixing, and Awaiting approval, with a live timeline,
   root cause, diff and test output. Repeats of the same error are grouped (seen Nx), not re-investigated.
3. Click **Approve & apply patch**. The fix is copied into the shop repo (`../demo_app`), the app reloads, and the button now works.
4. **Reset demo** restores the shop repo to its `buggy-baseline` git tag and clears the history.

Incidents are handled one at a time, oldest first. If the LLM provider's daily limit is hit, the whole queue pauses
(banner on the dashboard) and resumes automatically, or press Resume.

If two fixes touch the same file, approving the second one is refused (it would overwrite the first); the agent
re-runs on the updated code instead.


### Cost controls (all optional, no extra LLM calls)
- **Start from the stack trace:** the bug report lists the app's own frames and tells the agent to open them first.
- **Replay before the agent:** the dashboard re-sends the failing request. If it now succeeds (an earlier fix solved it)
  the incident is closed as "Already fixed" without using the LLM. Skipped when the request body has redacted fields.
  Only use this against apps where re-sending a request is harmless; set `REPLAY_REQUESTS=0` to turn it off. The
  monitored app must ignore requests with the header `X-Autofix-Replay` in its error reporter (see the shop's `webapp.py`).
- **Fix ledger:** after each approval a short note (files, summary, diff) is saved. New incidents get a one-line list
  of recent fixes, plus the full diff of any fix that touched a file in the new error's stack trace.
- **No repeated calls:** an identical read-only call (read_file, list_files, search_code, git_log) whose output is still
  in the conversation is not run or resent; the timeline shows it as "(repeat, skipped)". A write clears this.
- **Step budgets:** the investigator gets `INVESTIGATE_STEPS` (default 12) and the fixer `FIX_STEPS` (default 20) steps,
  with a "submit now" nudge two steps before the end.
- **Files pre-loaded for the fixer:** the files the investigator named, plus their `tests/test_<name>.py`, are put in the
  fixer's first message (max 8000 chars) so it does not re-read them.
- **Output masking:** tool results older than the last `LLM_MASK_KEEP` (default 6) are replaced by a short placeholder so
  they are not resent on every step. `LLM_MASK_KEEP=0` disables it.

Notes: the shop is found at `../demo_app` by default; set `DEMO_REPO=path` to point somewhere else or to monitor another Python repo (it needs a `tests/` folder and must POST errors to
`/api/errors`, see the shop's `webapp.py`). Set `USE_DOCKER=1` for container-isolated tests. Incidents are stored in
`dashboard/incidents.db`. Errors are only accepted from localhost, and nothing is applied without approval.

## Next steps
1. Add more seeded bugs under `bench/cases/` (off-by-one, wrong status code, missing null check, bad SQL filter).
2. Add a review phase (flag unrelated diff hunks) and `ruff`/Semgrep gates.
3. Try a small real repo (add its dependencies to `Dockerfile.sandbox`).
4. CLI report is enough for the demo; build the dashboard last.
