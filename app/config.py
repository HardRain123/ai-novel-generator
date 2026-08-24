from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    database_url: str = "sqlite:///data.db"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-v4-flash"
    llm_timeout_seconds: float = Field(default=90, gt=0, le=600)
    app_secret_key: str = ""
    allow_insecure_model_urls: bool = False
    web_origin: str = "http://localhost:3000"
    desktop_mode: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# Backwards-compatible constants for the existing service modules. New code should
# receive Settings through dependency injection instead of importing globals.
APP_ENV = settings.app_env
DATABASE_URL = settings.database_url
LLM_API_KEY = settings.llm_api_key
LLM_BASE_URL = settings.llm_base_url
LLM_MODEL = settings.llm_model
LLM_TIMEOUT_SECONDS = settings.llm_timeout_seconds
APP_SECRET_KEY = settings.app_secret_key
ALLOW_INSECURE_MODEL_URLS = settings.allow_insecure_model_urls
WEB_ORIGIN = settings.web_origin
DESKTOP_MODE = settings.desktop_mode

