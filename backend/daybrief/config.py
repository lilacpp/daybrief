from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    daybrief_mode: Literal["mock", "real"] = "real"
    tavily_api_key: str = ""
    tavily_base_url: str = "https://api.tavily.com"
    nebius_api_key: str = ""
    nebius_base_url: str = ""
    nebius_model: str = ""
    news_lookback_hours: int = Field(default=24, ge=1, le=168)
    enrichment_max_candidates: int = Field(default=6, ge=0, le=20)
    enrichment_queries_per_candidate: int = Field(default=2, ge=0, le=3)
    fallback_max_candidates: int = Field(default=6, ge=0, le=20)
    fallback_max_articles: int = Field(default=40, ge=0, le=100)
    provider_cache_enabled: bool = True
    provider_cache_dir: str = ".daybrief-cache"
    provider_cache_ttl_seconds: int = Field(default=3600, ge=1)
    database_url: str = "sqlite:///./daybrief.db"
    cors_origins: str = "http://localhost:3000"
    request_timeout_seconds: float = Field(default=45, gt=0, le=120)

    def validate_real(self):
        if self.daybrief_mode == "real" and not all(
            [
                self.tavily_api_key,
                self.nebius_api_key,
                self.nebius_base_url,
                self.nebius_model,
            ]
        ):
            raise ValueError(
                "Real mode requires TAVILY_API_KEY, NEBIUS_API_KEY, NEBIUS_BASE_URL and NEBIUS_MODEL"
            )
