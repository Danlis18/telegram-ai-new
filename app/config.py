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
    openai_image_model: str = "gpt-image-1"
    auto_publish: bool = False
    min_publish_score: int = 70
    database_path: str = "data/news.db"
    session_file_path: str = "data/telegram_reader.session"
    admin_user_id: int | None = None
    telegram_concurrent_updates: int = 8
    publish_timezone: str = "Europe/Kyiv"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()


# python-telegram-bot processes updates sequentially by default. In this project
# long image-generation callbacks must not block text edits, navigation or a
# second image generation. Configure every ApplicationBuilder created after
# this module is imported to process several updates concurrently.
def _enable_concurrent_telegram_updates() -> None:
    try:
        from telegram.ext import ApplicationBuilder

        if getattr(ApplicationBuilder, "_sports_news_concurrency_patch", False):
            return

        original_build = ApplicationBuilder.build

        def concurrent_build(builder):
            builder.concurrent_updates(max(2, settings.telegram_concurrent_updates))
            return original_build(builder)

        ApplicationBuilder.build = concurrent_build
        ApplicationBuilder._sports_news_concurrency_patch = True
    except Exception:
        # The bot can still start sequentially if the installed PTB version ever
        # changes; this avoids breaking deployment because of an optional tuning.
        pass


_enable_concurrent_telegram_updates()

