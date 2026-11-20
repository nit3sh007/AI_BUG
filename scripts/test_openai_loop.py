"""Tests the OpenAI-compatible backend's tool loop with a scripted fake client (no network/key)."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bugagent.llm_openai import OpenAICompatLLM, _to_json_schema  # noqa: E402
from bugagent.tools import SUBMIT_FIX, Tool  # noqa: E402


def call(id_, name, args):
    return NS(id=id_, function=NS(name=name, arguments=json.dumps(args)))


def reply(calls=None, content=None):
    return NS(choices=[NS(message=NS(tool_calls=calls, content=content))], usage=NS(total_tokens=10))


script = iter([
    reply(content="thinking out loud, no tool"),                       # should be nudged
    reply([call("1", "echo", {"text": "hi"})]),
    reply([call("2", "boom", {}), call("3", "submit_fix", {"summary": "done"})]),
])
seen = []
llm = OpenAICompatLLM.__new__(OpenAICompatLLM)
llm.model, llm.total_tokens, llm._last_call = "m", 0, 0.0
llm.client = NS(chat=NS(completions=NS(create=lambda **k: (seen.append(len(k["messages"])), next(script))[1])))

def boom():
    raise ValueError("kaboom")

tools = {"echo": Tool("echo", "d", {"type": "OBJECT", "properties": {"text": {"type": "STRING"}}}, lambda text: text),
         "boom": Tool("boom", "d", {"type": "OBJECT", "properties": {}}, boom),
         "submit_fix": SUBMIT_FIX}
log = []
out = llm.run_phase("sys", "task", tools, "submit_fix", 10, log)
assert out == {"summary": "done"}, out
assert [l["tool"] for l in log] == ["echo", "boom", "submit_fix"], log
assert "kaboom" in log[1]["output"] and llm.total_tokens == 30
assert _to_json_schema({"type": "OBJECT", "properties": {"x": {"type": "ARRAY", "items": {"type": "STRING"}}}}) == \
    {"type": "object", "properties": {"x": {"type": "array", "items": {"type": "string"}}}}
print("OPENAI BACKEND LOOP OK")

# ---- duplicate read-only calls are skipped; writes invalidate; step budget nudge
runs = []
def reader(path):
    runs.append(path)
    return "content of " + path * 300   # long enough to matter

script = iter([
    reply([call("a", "read_file", {"path": "x.py"}), call("b", "read_file", {"path": "x.py"})]),  # same step dup
    reply([call("c", "read_file", {"path": "x.py"})]),                                              # earlier-step dup
    reply([call("d", "write_file", {"path": "x.py"})]),                                             # invalidates
    reply([call("e", "read_file", {"path": "x.py"})]),                                              # must run again
    reply([call("f", "submit_fix", {"summary": "ok"})]),
])
sent = []
llm.client = NS(chat=NS(completions=NS(create=lambda **k: (sent.append([m.get("content") for m in k["messages"]]), next(script))[1])))
tools2 = {"read_file": Tool("read_file", "d", {"type": "OBJECT", "properties": {"path": {"type": "STRING"}}}, reader),
          "write_file": Tool("write_file", "d", {"type": "OBJECT", "properties": {"path": {"type": "STRING"}}}, lambda path: "w"),
          "submit_fix": SUBMIT_FIX}
log2 = []
assert llm.run_phase("sys", "task", tools2, "submit_fix", 8, log2) == {"summary": "ok"}
assert runs == ["x.py", "x.py"], runs                       # executed twice only (first read, and read after the write)
assert [l["skipped"] for l in log2] == [False, True, True, False, False, False], [l["skipped"] for l in log2]
script = iter([reply([call("g", "read_file", {"path": "y.py"})]) for _ in range(3)] + [reply([call("h", "submit_fix", {"summary": "ok"})])])
sent.clear()
llm.client = NS(chat=NS(completions=NS(create=lambda **k: (sent.append([m.get("content") for m in k["messages"]]), next(script))[1])))
llm.run_phase("sys", "task", tools2, "submit_fix", 4, [])
assert any("almost out of steps" in str(c) for c in sent[2]), "nudge missing"
print("DEDUPE + NUDGE OK")
