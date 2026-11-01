from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/redline"
    storage_dir: Path = BACKEND_DIR / "storage"
    cors_origins: str = "http://localhost:5173"

    jwt_secret: str = "change-me-in-production"
    jwt_hours: int = 24

    max_upload_mb: int = 40
    max_unzipped_mb: int = 400

    llm_provider: str = "groq"
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "medium"
    anthropic_api_key: str | None = None
    groq_api_key: str | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "openai/gpt-oss-120b"
    groq_max_tokens: int = 3000
    llamacpp_base_url: str = "http://localhost:8080/v1"
    llamacpp_model: str = "local"
    llamacpp_max_tokens: int = 8192

    pinecone_api_key: str | None = None
    pinecone_index: str = "redline-chunks"
    pinecone_cloud: str = "aws"
    pinecone_region: str = "us-east-1"
    pinecone_embed_model: str = "multilingual-e5-large"

    tavily_api_key: str | None = None
    web_results: int = 5
    evidence_chars: int = 6000

    max_writer_attempts: int = 3

    @property
    def llm_label(self) -> str:
        labels = {"llamacpp": self.llamacpp_model, "groq": self.groq_model}
        return labels.get(self.llm_provider, self.llm_model)

    @property
    def llm_configured(self) -> bool:
        keys = {"anthropic": self.anthropic_api_key, "groq": self.groq_api_key, "llamacpp": "local"}
        return bool(keys.get(self.llm_provider))

    @property
    def sync_checkpoint_url(self) -> str:
        return self.database_url.replace("postgresql+psycopg://", "postgresql://")


@lru_cache
def get_settings() -> Settings:
    return Settings()
