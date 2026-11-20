"""OpenAI-compatible backend: Azure OpenAI deployments, Azure AI Foundry endpoints, or any
OpenAI-style API. Same run_phase interface as the Gemini backend."""
import json
import re
import os
import time

from . import env  # noqa: F401  (loads <project root>/.env)


def _need(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Set {name} in your .env file.")
    return v


def _to_json_schema(node):
    """Tool schemas are written Gemini-style (type: 'OBJECT'); OpenAI wants lowercase JSON Schema."""
    if isinstance(node, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else _to_json_schema(v))
                for k, v in node.items()}
    if isinstance(node, list):
        return [_to_json_schema(x) for x in node]
    return node


READ_ONLY_TOOLS = {"read_file", "list_files", "search_code", "git_log"}


def mask_old_tool_outputs(messages: list, meta: dict, keep: int, min_chars: int = 500) -> int:
    """Every step resends the whole conversation, so old file dumps are paid for again and again. Replace the
    content of tool results older than the last `keep` ones with a short placeholder (the agent can simply call the
    tool again). This is plain truncation, not LLM summarising, so it costs no extra tokens. Returns chars saved."""
    if keep <= 0:
        return 0
    idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    saved = 0
    for i in idx[:-keep]:
        m = messages[i]
        c = m.get("content") or ""
        if len(c) >= min_chars and not c.startswith("[omitted"):
            what = meta.get(m.get("tool_call_id"), "earlier tool call")
            m["content"] = f"[omitted to save tokens: output of {what} ({len(c)} chars). Call the tool again if needed.]"
            saved += len(c) - len(m["content"])
    return saved


class OpenAICompatLLM:
    def __init__(self):
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
        if endpoint:  # classic Azure OpenAI resource
            from openai import AzureOpenAI
            self.client = AzureOpenAI(azure_endpoint=endpoint, api_key=_need("AZURE_OPENAI_API_KEY"),
                                      api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"))
            self.model = _need("AZURE_OPENAI_DEPLOYMENT")  # the *deployment name*, not the model family
        else:  # Foundry / generic OpenAI-compatible endpoint
            from openai import OpenAI
            key = _need("OPENAI_API_KEY")
            base = _need("OPENAI_BASE_URL")
            kw = {}
            if os.environ.get("OPENAI_API_VERSION"):  # Foundry model-inference endpoints need ?api-version=
                kw["default_query"] = {"api-version": os.environ["OPENAI_API_VERSION"]}
            if "azure.com" in base:  # Azure accepts the key in an api-key header
                kw["default_headers"] = {"api-key": key}
            self.client = OpenAI(base_url=base, api_key=key, **kw)
            self.model = _need("LLM_MODEL")
        self.total_tokens = 0
        self._last_call = 0.0

    def _throttle(self):
        gap = float(os.environ.get("LLM_MIN_INTERVAL", "1"))
        wait = self._last_call + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def _generate(self, **kwargs):
        delays = [10, 20, 40, 60]
        for attempt in range(len(delays) + 1):
            self._throttle()
            try:
                return self.client.chat.completions.create(**kwargs)
            except Exception as e:
                code = getattr(e, "status_code", None)
                if code == 429 and re.search(r"per day|\bTPD\b|\bRPD\b", str(e)):
                    raise  # a daily quota will not recover in a minute; let the caller pause instead of retrying
                retryable = code in (429, 500, 502, 503, 504)
                if not retryable or attempt == len(delays):
                    raise
                print(f"[llm] rate limited or busy (code {code}); waiting {delays[attempt]}s then retrying...")
                time.sleep(delays[attempt])

    def run_phase(self, system, user_msg, tools, terminal, max_steps, log):
        specs = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                   "parameters": _to_json_schema(t.parameters)}}
                 for t in tools.values()]
        extra = {}
        if os.environ.get("LLM_TEMPERATURE"):
            extra["temperature"] = float(os.environ["LLM_TEMPERATURE"])
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user_msg}]
        bad_calls = 0
        meta: dict = {}                       # tool_call_id -> short description, for the masking placeholder
        keep = int(os.environ.get("LLM_MASK_KEEP", "6"))   # 0 disables masking
        seen: dict = {}                       # (tool, args) -> id of the call whose output is still in the conversation
        nudged = False
        for step in range(max_steps):
            if not nudged and max_steps >= 4 and step == max_steps - 2:
                nudged = True
                messages.append({"role": "user", "content":
                                 f"You are almost out of steps. Call {terminal} now with your best conclusion."})
            mask_old_tool_outputs(messages, meta, keep)
            try:
                resp = self._generate(model=self.model, messages=messages, tools=specs, **extra)
            except Exception as e:
                # Some open models (e.g. on Groq) occasionally emit a malformed tool call, which the API rejects
                # with a 400. Nudge the model and retry a few times instead of crashing the whole run.
                msg_l = str(e).lower()
                malformed = any(k in msg_l for k in ("tool", "parsing failed", "failed_generation", "could not be parsed"))
                if getattr(e, "status_code", None) == 400 and malformed and bad_calls < 6:
                    bad_calls += 1
                    messages.append({"role": "user", "content":
                                     "Your last tool call was malformed. Call a tool again with valid JSON arguments."})
                    continue
                raise
            if getattr(resp, "usage", None) and resp.usage.total_tokens:
                self.total_tokens += resp.usage.total_tokens
            msg = resp.choices[0].message
            calls = msg.tool_calls or []
            entry = {"role": "assistant", "content": msg.content or ""}
            if calls:
                entry["tool_calls"] = [{"id": c.id, "type": "function",
                                        "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                       for c in calls]
            messages.append(entry)
            if not calls:
                messages.append({"role": "user", "content": f"Use the tools. Call {terminal} when you are done."})
                continue
            final = None
            for c in calls:
                name = c.function.name
                try:
                    _a = json.loads(c.function.arguments or "{}")
                    meta[c.id] = f"{name} {_a.get('path') or _a.get('pattern') or _a.get('target') or ''}".strip()
                except Exception:
                    meta[c.id] = name
                try:
                    args = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = None
                key = (name, json.dumps(args, sort_keys=True)) if args is not None else None
                dup = False
                if name in READ_ONLY_TOOLS and key in seen:
                    prev = next((m for m in messages if m.get("role") == "tool"
                                 and m.get("tool_call_id") == seen[key]), None)
                    dup = prev is not None and not prev["content"].startswith("[omitted")
                if args is None:
                    out = {"error": "tool arguments were not valid JSON"}
                elif dup:   # same read-only call as before and its output is still above: do not resend it
                    out = {"result": "(identical to your earlier call; the output is already above, not run again)"}
                elif name == terminal:
                    final, out = args, {"result": "accepted"}
                elif name in tools and tools[name].fn:
                    try:
                        out = {"result": tools[name].fn(**args)}
                    except Exception as e:
                        out = {"error": str(e)}
                    if name in READ_ONLY_TOOLS:
                        seen[key] = c.id
                    elif name == "write_file":
                        seen.clear()   # files changed: earlier reads are stale
                else:
                    out = {"error": f"unknown tool {name}"}
                log.append({"tool": name, "args": args, "output": str(out)[:1500], "skipped": dup})
                messages.append({"role": "tool", "tool_call_id": c.id, "content": json.dumps(out, default=str)})
            if final is not None:
                return final
        return None