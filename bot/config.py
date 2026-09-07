from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str
    admin_id: int
    database_path: Path = Path("data/bot.db")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")
