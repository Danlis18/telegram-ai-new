from contextlib import contextmanager
from contextvars import ContextVar, Token

from telegram import Update
from telegram.ext import Application, ContextTypes, TypeHandler


_current_user_id: ContextVar[int | None] = ContextVar("sports_news_current_user_id", default=None)


def get_current_user_id() -> int | None:
    return _current_user_id.get()


def set_current_user_id(user_id: int | None) -> Token:
    return _current_user_id.set(int(user_id) if user_id is not None else None)


def reset_current_user_id(token: Token) -> None:
    _current_user_id.reset(token)


@contextmanager
def user_scope(user_id: int | None):
    token = set_current_user_id(user_id)
    try:
        yield
    finally:
        reset_current_user_id(token)


async def _set_update_tenant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        set_current_user_id(user.id)


def register_tenant_context(app: Application) -> None:
    app.add_handler(TypeHandler(Update, _set_update_tenant), group=-100)
