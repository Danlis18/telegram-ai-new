from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    telegram_session: str | None = None
    telegram_session_file_b64: str | None = None
    telegram_bot_token: str
    target_channel: str
    openai_api_key: str
    openai_model: str = "gpt-5-mini"
    auto_publish: bool = False
    min_publish_score: int = 70
    database_path: str = "data/news.db"
    session_file_path: str = "data/telegram_reader.session"
    admin_user_id: int | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
