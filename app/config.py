"""Application configuration via environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = f"sqlite:///{(PROJECT_ROOT / 'data' / 'app.db').as_posix()}"
    gmail_credentials_path: Path = PROJECT_ROOT / "data" / "credentials.json"
    gmail_token_path: Path = PROJECT_ROOT / "data" / "tokens" / "gmail_token.json"
    gmail_query: str = (
        "(from:jobs-listings@linkedin.com OR from:linkedin.com OR subject:(job alert) "
        "OR subject:(internship) OR from:indeed.com OR from:glassdoor.com "
        "OR from:handshake.com OR subject:(career)) newer_than:90d"
    )
    gmail_sync_max_results: int = 100

    enable_llm: bool = False
    llm_provider: str = "openai"  # openai | anthropic
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None

    scoring_weights_path: Path = PROJECT_ROOT / "config" / "scoring_weights.yaml"
    interest_keywords_path: Path = PROJECT_ROOT / "config" / "interest_keywords.yaml"


settings = Settings()
