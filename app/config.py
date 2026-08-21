import os
import shutil
from pathlib import Path

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

# Railway containers are ephemeral between redeploys. If a Railway Volume is
# mounted, transparently keep SQLite on that volume unless DATABASE_PATH was
# explicitly overridden. If the legacy DB is visible on the first volume-backed
# boot, copy it once so the existing history is retained.
railway_volume = (os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
if railway_volume and settings.database_path == "data/news.db":
    persistent_db = Path(railway_volume) / "sports_news.db"
    legacy_db = Path("data/news.db")
    try:
        persistent_db.parent.mkdir(parents=True, exist_ok=True)
        if not persistent_db.exists() and legacy_db.exists():
            shutil.copy2(legacy_db, persistent_db)
    except Exception:
        pass
    settings.database_path = str(persistent_db)


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
        pass


_enable_concurrent_telegram_updates()
