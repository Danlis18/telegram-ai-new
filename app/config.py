from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_api_id: int
    telegram_api_hash: str
    telegram_session: str
    telegram_bot_token: str
    target_channel: str
    openai_api_key: str
    openai_model: str = "gpt-5-mini"
    auto_publish: bool = False
    min_publish_score: int = 70
    database_path: str = "data/news.db"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
