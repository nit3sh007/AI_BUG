"""Finds which Azure endpoint style your deployment answers to. Prints status codes and error text only;
it never prints your key. Run from the project folder:  python scripts\\probe_azure.py"""
import os
import sys
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv(override=True)
key = os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
ep = (os.environ.get("AZURE_OPENAI_ENDPOINT") or os.environ.get("OPENAI_BASE_URL") or "").rstrip("/")
dep = os.environ.get("AZURE_OPENAI_DEPLOYMENT") or os.environ.get("LLM_MODEL")
if not (key and ep and dep):
    sys.exit("Need the key, endpoint and deployment name in .env")
u = urlparse(ep)
root = f"{u.scheme}://{u.netloc}"
print("endpoint host:", u.netloc, "| path:", u.path or "/", "| deployment:", dep, "\n")
body = {"messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 5}
cands = [
    ("A) Azure OpenAI style", f"{root}/openai/deployments/{dep}/chat/completions?api-version=2024-10-21",
     {"api-key": key}, {}, "AZURE_OPENAI_ENDPOINT=" + root + "/   (current setup)"),
    ("B) Foundry model-inference style", f"{root}/models/chat/completions?api-version=2024-05-01-preview",
     {"api-key": key}, {"model": dep},
     f"OPENAI_BASE_URL={root}/models\nOPENAI_API_VERSION=2024-05-01-preview\nLLM_MODEL={dep}"),
    ("C) OpenAI v1 style", f"{root}/openai/v1/chat/completions",
     {"api-key": key}, {"model": dep}, f"OPENAI_BASE_URL={root}/openai/v1/\nLLM_MODEL={dep}"),
    ("D) Serverless endpoint (Bearer)", f"{ep}/chat/completions",
     {"Authorization": f"Bearer {key}"}, {"model": dep},
     f"OPENAI_BASE_URL={ep}\nLLM_MODEL={dep}   (and ask me to add Bearer auth)"),
]
for name, url, headers, extra, env in cands:
    try:
        r = requests.post(url, json={**body, **extra}, headers=headers, timeout=30)
        text = r.text.replace(key, "***")[:200].replace("\n", " ")
        print(f"{name}: HTTP {r.status_code}  {text}")
        if r.status_code == 200:
            print("   >>> WORKS. Use these lines in .env:\n   " + env.replace("\n", "\n   ") + "\n")
    except Exception as e:
        print(f"{name}: failed ({type(e).__name__})")
