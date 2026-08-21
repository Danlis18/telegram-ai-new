import sqlite3

from app.config import settings


def is_owner_id(user_id: int | None) -> bool:
    return bool(user_id and settings.admin_user_id and int(user_id) == int(settings.admin_user_id))


def is_authorized_id(user_id: int | None) -> bool:
    if not user_id:
        return False
    if is_owner_id(user_id):
        return True
    try:
        with sqlite3.connect(settings.database_path) as db:
            row = db.execute(
                "SELECT 1 FROM users WHERE telegram_user_id=? AND is_active=1 LIMIT 1",
                (int(user_id),),
            ).fetchone()
            return row is not None
    except Exception:
        return False
