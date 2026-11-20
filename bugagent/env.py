"""Single place where .env is loaded. Looks in <project root>/.env first, then each parent folder (so a .env kept
one level up still works). Override with DOTENV_PATH."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
_forced = os.environ.get("DOTENV_PATH")
_candidates = [Path(_forced)] if _forced else [d / ".env" for d in (ROOT, *ROOT.parents)]
ENV_FILE = next((p for p in _candidates if p.is_file()), None)
if ENV_FILE:
    load_dotenv(ENV_FILE, override=True, encoding="utf-8-sig")


def describe() -> str:
    """One line for logs and error messages: which provider was picked and from which file."""
    where = f".env loaded from {ENV_FILE}" if ENV_FILE else f"NO .env FOUND (looked in {ROOT} and its parent folders; put it in {ROOT} or set DOTENV_PATH)"
    return f"LLM_PROVIDER={os.environ.get('LLM_PROVIDER', '(not set, defaults to gemini)')}; {where}"
