"""Tests for Settings loading from environment variables."""
from app.config import Settings


def test_settings_reads_env_vars(monkeypatch):
    """All fields should be overridable via env vars."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")
    monkeypatch.setenv("OPENAI_API_KEY", "abc123")
    monkeypatch.setenv("LLM_MODEL", "anthropic/claude-sonnet-5-5")
    monkeypatch.setenv("SYSTEM_PROMPT_FILE", "data/knowledge_base.md")
    monkeypatch.setenv("CORS_ORIGINS", "http://example.com,http://other.com")
    monkeypatch.setenv("PORT", "9000")

    settings = Settings(_env_file=None)

    assert settings.anthropic_api_key == "ant-key"
    assert settings.openai_api_key == "abc123"
    assert settings.llm_model == "anthropic/claude-sonnet-5-5"
    assert settings.system_prompt_file == "data/knowledge_base.md"
    assert settings.cors_origins_list == ["http://example.com", "http://other.com"]
    assert settings.port == 9000


def test_settings_defaults(monkeypatch):
    """Everything but ANTHROPIC_API_KEY should have a sane default."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("SYSTEM_PROMPT_FILE", raising=False)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    monkeypatch.delenv("PORT", raising=False)

    settings = Settings(_env_file=None)

    assert settings.llm_model == "anthropic/claude-sonnet-5-5"
    assert settings.system_prompt_file == "data/knowledge_base.md"
    assert settings.cors_origins_list == ["http://localhost:5173"]
    assert settings.port == 8000
