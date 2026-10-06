"""Application configuration, loaded from environment variables / a .env file.

Key settings:
    ANTHROPIC_API_KEY      — required, used by litellm (Claude) and web search
    OPENAI_API_KEY         — required for RAG embeddings (text-embedding-3-small)
    LLM_MODEL              — litellm model string, default anthropic/claude-sonnet-5
    WEB_SEARCH_MODEL       — Anthropic model for web search, default claude-haiku-4-5
    SYSTEM_PROMPT_FILE     — path to base system-prompt markdown file
    CORS_ORIGINS           — comma-separated list of allowed CORS origins
    PORT                   — uvicorn port
    WEAVIATE_URL           — Weaviate URL, self-hosted or Cloud (optional, enables RAG)
    WEAVIATE_API_KEY       — Weaviate Cloud admin API key (leave blank for self-hosted)
    MCP_SERVER_URL         — bucketlistt MCP server URL (optional, enables live catalog tools)
    REDIS_URL              — Redis URL (optional, MCP result cache + chat session store)
    SESSION_TTL_SECONDS    — chat session TTL in Redis, default 7200 (2h)
    LOGIN_PROMPT_AFTER     — user messages before showing the login-details prompt, default 3
    MAX_MESSAGES_PER_SESSION — user messages allowed per chat session before a friendly
                             "start a new chat" reply replaces the LLM call, default 15
    CHAT_API_KEY           — optional; when set, POST /api/chat and /api/session/user-info need
                             `Authorization: Bearer <key>`, and valid-key calls skip the rate limit
    CHAT_LIMIT_PER_MINUTE  — per-IP /api/chat limit for callers without a valid key, default 20
    DASHBOARD_API_KEY      — bearer token guarding GET /api/admin/session-summaries (optional,
                             endpoints return 503 if unset — see app/dashboard.py)
    IDLE_SUMMARY_MINUTES   — inactivity before a session is summarized for the dashboard, default 15
    SUMMARY_WEBHOOK_URL    — optional; POSTed with every newly generated session summary
                             (see app/dashboard.py _send_webhook). Blank = feature off.
    SUMMARY_WEBHOOK_SECRET — optional HMAC-SHA256 secret used to sign SUMMARY_WEBHOOK_URL
                             deliveries (X-Webhook-Signature header). Recommended whenever
                             SUMMARY_WEBHOOK_URL is set; deliveries go out unsigned without it.
"""
import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration for the chatbot backend.

    Every field is overridable via an environment variable of the same
    name (case-insensitive) or via a `.env` file in the working directory.
    """

    anthropic_api_key: str
    openai_api_key: str = ""
    llm_model: str = "anthropic/claude-sonnet-5"
    web_search_model: str = "claude-haiku-4-5"
    system_prompt_file: str = "data/knowledge_base.md"
    cors_origins: str = "http://localhost:5173"
    port: int = 8000
    # Weaviate vector DB for RAG — optional; if blank, falls back to flat file KB
    weaviate_url: str = ""
    weaviate_api_key: str = ""
    # bucketlistt MCP server — read-only catalog tools only, see app/mcp_client.py
    mcp_server_url: str = ""
    # Redis — MCP result cache (app/cache.py) and chat session store /
    # login-prompt trigger (app/session_store.py). Optional; both features
    # no-op if unset or unreachable.
    redis_url: str = "redis://localhost:6379/0"
    session_ttl_seconds: int = 7200          # 2 hours
    login_prompt_after: int = 3              # prompt after 3 user messages
    max_messages_per_session: int = 15       # past this, a friendly "start a new chat" reply
    # Machine-to-machine callers (e.g. the WhatsApp CRM). Blank = /api/chat
    # stays open exactly as before (the browser frontend can't hold a secret).
    # Set it on a dedicated deployment to require `Authorization: Bearer <key>`;
    # requests carrying a valid key also skip the per-IP rate limit.
    chat_api_key: str = ""
    chat_limit_per_minute: int = 20          # per-IP, requests without a valid key
    # Employee-dashboard session summaries — see app/dashboard.py. Idle-scan
    # runs regardless of REDIS_URL/DASHBOARD_API_KEY; it just no-ops without
    # them (nothing to scan / no way to authenticate reads).
    dashboard_api_key: str = ""              # blank = /api/admin/* returns 503
    idle_summary_minutes: int = 15           # inactivity before summarizing a session
    idle_scan_interval_seconds: int = 300    # how often to check for idle sessions
    # Push delivery of generated summaries to an external system — optional,
    # blank = off. See app/dashboard.py _send_webhook for retry/signing.
    summary_webhook_url: str = ""
    summary_webhook_secret: str = ""

    # SMTP for out-of-credits / critical error notifications
    smtp_server: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_to: str = ""

    # Attachments (§4) — all optional; the feature no-ops if attachment_ids
    # is never sent, same pattern as MCP_SERVER_URL/WEAVIATE_URL.
    attachments_dir: str = "/tmp/chatbot-attachments"  # local disk, TTL-swept — see app/dashboard.py
    attachment_max_image_mb: int = 10       # matches Claude's own per-image cap
    attachment_max_pdf_mb: int = 32         # matches Claude's request-body cap for documents
    attachment_max_docx_mb: int = 8
    attachment_max_per_message: int = 5
    clamd_host: str = "localhost"
    clamd_port: int = 3310

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def cors_origins_list(self) -> list[str]:
        """CORS_ORIGINS as a list, split on commas, for use with CORSMiddleware."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()

# litellm and the Anthropic SDK read credentials from os.environ. If a key
# only came from a .env file (not an actual exported env var) it wouldn't be
# visible otherwise, so mirror them here.
os.environ.setdefault("ANTHROPIC_API_KEY", settings.anthropic_api_key)
if settings.openai_api_key:
    os.environ.setdefault("OPENAI_API_KEY", settings.openai_api_key)
