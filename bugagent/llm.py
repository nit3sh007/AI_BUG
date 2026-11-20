"""Gemini wrapper with a manual function-calling loop (so the orchestrator stays in control)."""
import os
import time

from google import genai
from google.genai import types

from . import env  # noqa: F401  (loads <project root>/.env)


class GeminiLLM:
    def __init__(self, model: str | None = None):
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError(f"Set GEMINI_API_KEY first. ({env.describe()})")
        self.client = genai.Client(api_key=key)
        # Model names change over time; override with GEMINI_MODEL if this one is unavailable.
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
        self.total_tokens = 0
        self._last_call = 0.0

    def _throttle(self):
        # Free tier has low requests-per-minute limits; space calls out (override with GEMINI_MIN_INTERVAL).
        gap = float(os.environ.get("GEMINI_MIN_INTERVAL", "4"))
        wait = self._last_call + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def _generate(self, contents, config):
        delays = [15, 30, 60, 60]  # seconds to wait after a rate-limit / transient error
        for attempt in range(len(delays) + 1):
            self._throttle()
            try:
                return self.client.models.generate_content(model=self.model, contents=contents, config=config)
            except Exception as e:
                code = getattr(e, "code", None)
                msg = str(e)
                retryable = code in (429, 500, 502, 503, 504) or "RESOURCE_EXHAUSTED" in msg or "UNAVAILABLE" in msg
                if not retryable or attempt == len(delays):
                    raise
                print(f"[llm] rate limited or busy (code {code}); waiting {delays[attempt]}s then retrying...")
                time.sleep(delays[attempt])

    def run_phase(self, system, user_msg, tools, terminal, max_steps, log):
        """Run tool-calling until `terminal` is called. Returns its args dict, or None on budget exhaustion."""
        decls = [types.FunctionDeclaration(name=t.name, description=t.description, parameters=t.parameters)
                 for t in tools.values()]
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=decls)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.2,
        )
        contents = [types.Content(role="user", parts=[types.Part(text=user_msg)])]
        for step in range(max_steps):
            resp = self._generate(contents, config)
            if resp.usage_metadata and resp.usage_metadata.total_token_count:
                self.total_tokens += resp.usage_metadata.total_token_count
            cand = resp.candidates[0] if resp.candidates else None
            if cand is None or cand.content is None:
                contents.append(types.Content(role="user", parts=[types.Part(text="Continue using tools.")]))
                continue
            contents.append(cand.content)
            calls = resp.function_calls or []
            if not calls:
                contents.append(types.Content(role="user", parts=[types.Part(
                    text=f"Use the tools. Call {terminal} when you are done.")]))
                continue
            parts, final = [], None
            for c in calls:
                args = dict(c.args or {})
                if c.name == terminal:
                    final, out = args, {"result": "accepted"}
                elif c.name in tools and tools[c.name].fn:
                    try:
                        out = {"result": tools[c.name].fn(**args)}
                    except Exception as e:
                        out = {"error": str(e)}
                else:
                    out = {"error": f"unknown tool {c.name}"}
                log.append({"tool": c.name, "args": args, "output": str(out)[:1500]})
                parts.append(types.Part.from_function_response(name=c.name, response=out))
            contents.append(types.Content(role="user", parts=parts))
            if final is not None:
                return final
        return None
