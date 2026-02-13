"""Application configuration."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str = ""
    database_url: str = "sqlite+aiosqlite:///./quest_platform.db"
    secret_key: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 24
    app_url: str = "http://localhost:8000"
    webapp_url: str = "http://localhost:8000/player"
    tg_allowed_ids: str = ""  # Комма через запятую — кому доступен mini-app. Пусто = без ограничений.
    event_timezone: str = "Europe/Moscow"  # Таймзона для start_at (Europe/Moscow, UTC и т.д.)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
