"""Shared pytest fixtures.

Sets required env vars *before* app.config is imported anywhere, since
Settings() is instantiated once at module import time. Tests are expected to
run from the backend/ directory so the relative SYSTEM_PROMPT_FILE path
resolves.
"""
import fnmatch
import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "anthropic/claude-sonnet-5")
os.environ.setdefault("SYSTEM_PROMPT_FILE", "data/knowledge_base.md")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:5173")


class FakeRedis:
    """Just enough of redis.asyncio's async API to test session_store /
    dashboard logic without a real Redis server.
    """

    def __init__(self, data: dict | None = None):
        self._data = data or {}  # {key: {field: value}}
        self._strings = {}
        self._ttls = {}

    async def hgetall(self, key):
        return self._data.get(key, {})

    async def hget(self, key, field):
        return self._data.get(key, {}).get(field)

    async def hset(self, key, field=None, value=None, mapping=None):
        bucket = self._data.setdefault(key, {})
        if mapping:
            bucket.update(mapping)
        if field is not None:
            bucket[field] = value

    async def expire(self, key, seconds):
        pass

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self._strings:
            return None
        self._strings[key] = value
        self._ttls[key] = ex
        return True

    async def get(self, key):
        return self._strings.get(key)

    async def ttl(self, key):
        return self._ttls.get(key) or -1

    async def delete(self, key):
        self._strings.pop(key, None)

    async def scan_iter(self, match="*"):
        for key in list(self._data.keys()):
            if fnmatch.fnmatch(key, match):
                yield key
