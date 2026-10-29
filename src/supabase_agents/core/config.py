"""Runtime configuration, overridable by environment variable.

Models go through OpenRouter: Claude Haiku 5.5 does the work, GPT-6.1 Terra judges (different family).
ChatOpenRouter's `timeout` is in MILLISECONDS. Structured output uses JSON schema, not forced tool calls.
Embeddings are local (fastembed BAAI/bge-small-en-v1.5, 384 dims): no key, nothing leaves the machine.

Database: the local Supabase stack (`supabase start`). SUPABASE_DB_URL is the Postgres connection;
each agent works in its own schema so they never collide.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MODEL = "openrouter:anthropic/claude-haiku-5.5"
DEFAULT_JUDGE_MODEL = "openrouter:openai/gpt-6.1-terra"
APP_URL = "https://buildwithmoses.vercel.app"
APP_TITLE = "Supabase Agents (buildwithmoses)"
OPENROUTER_PROVIDER = {"require_parameters": True, "allow_fallbacks": True}
TIMEOUT_MS = 180_000
LOCAL_DB_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384

SCHEMAS = {"docs-qa": "docs_qa", "rls-analyst": "analyst", "db-health": "health",
           "support-memory": "support", "firebase-migration": "migration"}


def load_env() -> None:
    from dotenv import dotenv_values

    for folder in (PROJECT_ROOT, *PROJECT_ROOT.parents):
        env_file = folder / ".env"
        if env_file.is_file():
            for key, value in dotenv_values(env_file).items():
                if value and not os.environ.get(key):
                    os.environ[key] = value


def model_id() -> str:
    return os.getenv("SBA_MODEL", DEFAULT_MODEL)


def judge_model_id() -> str:
    return os.getenv("SBA_JUDGE_MODEL", DEFAULT_JUDGE_MODEL)


def get_model(mid: str | None = None, **kwargs) -> BaseChatModel:
    load_env()
    mid = mid or model_id()
    if mid.startswith("openrouter:"):
        kwargs.setdefault("timeout", TIMEOUT_MS)
        kwargs.setdefault("max_retries", 0)
        return init_chat_model(mid, app_url=APP_URL, app_title=APP_TITLE,
                               openrouter_provider=kwargs.pop("openrouter_provider", OPENROUTER_PROVIDER), **kwargs)
    return init_chat_model(mid, **kwargs)


def db_url() -> str:
    load_env()
    return os.getenv("SUPABASE_DB_URL", LOCAL_DB_URL)


def user_db_url() -> str:
    """Connection used for end-user work: Supabase's `authenticator` login role (as PostgREST uses).
    Locally its password is the database password; set SUPABASE_USER_DB_URL for a real project."""
    load_env()
    if os.getenv("SUPABASE_USER_DB_URL"):
        return os.environ["SUPABASE_USER_DB_URL"]
    from urllib.parse import urlsplit, urlunsplit

    u = urlsplit(db_url())
    netloc = f"authenticator:{u.password}@{u.hostname}:{u.port}" if u.password else f"authenticator@{u.hostname}:{u.port}"
    return urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))


def data_dir(*parts: str) -> Path:
    return PROJECT_ROOT.joinpath("fixtures", *parts)


@lru_cache(maxsize=1)
def _embedder():
    from fastembed import TextEmbedding

    return TextEmbedding(EMBED_MODEL)


def embed(texts: list[str]) -> list[list[float]]:
    """Local embeddings (384 dims). Deterministic for the same text."""
    return [list(map(float, v)) for v in _embedder().embed(texts)]
