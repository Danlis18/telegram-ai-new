import html
import logging
import re
from contextlib import suppress

import aiosqlite
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ApplicationHandlerStop, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from app.auth import is_authorized_id
from app.config import settings
from app.database import (
    add_user_source,
    add_user_target,
    delete_user_source,
    delete_user_target,
    get_default_target,
    list_user_sources,
    list_user_targets,
    set_default_target,
)
from app.formatting import post_html

log = logging.getLogger("telegram-ai-news.channel-workspace")


def _normalize_source(value: str) -> str:
    value = (value or "").strip()
    value = re.sub(r"^https?://(?:www\.)?t\.me/", "", value, flags=re.I)
    value = re.sub(r"^telegram\.me/", "", value, flags=re.I)
    value = value.split("?", 1)[0].strip("/ ").lstrip("@").lower()
    if value.startswith("+") or "/" in value or not re.fullmatch(r"[a-zA-Z0-9_]{4,64}", value):
        raise ValueError("Потрібен публічний @username Telegram-каналу")
    return value


async def ensure_channel_route_schema() -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS target_source_routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, username)
            )"""
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_target_source_routes_target "
            "ON target_source_routes(user_id,target_id,username)"
        )
        await db.commit()


async def _ensure_user_routes(user_id: int) -> None:
    """Every source belongs to exactly one publication channel; default is fallback."""
    await ensure_channel_route_schema()
    targets = await list_user_targets(user_id)
    sources = await list_user_sources(user_id)
    if not targets:
        return
    default = await get_default_target(user_id) or targets[0]
    target_ids = {int(row["id"]) for row in targets}
    source_names = {str(row["username"]).lower() for row in sources}

    async with aiosqlite.connect(settings.database_path) as db:
        if target_ids:
            placeholders = ",".join("?" for _ in target_ids)
            await db.execute(
                f"DELETE FROM target_source_routes WHERE user_id=? AND target_id NOT IN ({placeholders})",
                (int(user_id), *sorted(target_ids)),
            )
        if source_names:
            placeholders = ",".join("?" for _ in source_names)
            await db.execute(
                f"DELETE FROM target_source_routes WHERE user_id=? AND lower(username) NOT IN ({placeholders})",
                (int(user_id), *sorted(source_names)),
            )
        else:
            await db.execute("DELETE FROM target_source_routes WHERE user_id=?", (int(user_id),))

        for username in sorted(source_names):
            await db.execute(
                """INSERT OR IGNORE INTO target_source_routes (user_id,target_id,username)
                   VALUES (?,?,?)""",
                (int(user_id), int(default["id"]), username),
            )
        await db.commit()


async def assign_source_to_target(user_id: int, username: str, target_id: int) -> None:
    await ensure_channel_route_schema()
    normalized = _normalize_source(username)
    targets = await list_user_targets(user_id)
    if int(target_id) not in {int(row["id"]) for row in targets}:
        raise ValueError("Канал публікації не знайдено")
    await add_user_source(int(user_id), normalized)
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """INSERT INTO target_source_routes (user_id,target_id,username)
               VALUES (?,?,?)
               ON CONFLICT(user_id,username) DO UPDATE SET target_id=excluded.target_id""",
            (int(user_id), int(target_id), normalized),
        )
        await db.commit()


async def _delete_source(user_id: int, source_id: int) -> None:
    sources = await list_user_sources(user_id, active_only=False)
    row = next((r for r in sources if int(r["id"]) == int(source_id)), None)
    if row:
        async with aiosqlite.connect(settings.database_path) as db:
            await db.execute(
                "DELETE FROM target_source_routes WHERE user_id=? AND lower(username)=?",
                (int(user_id), str(row["username"]).lower()),
            )
            await db.commit()
    await delete_user_source(int(source_id), int(user_id))


async def _delete_target(user_id: int, target_id: int) -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "DELETE FROM target_source_routes WHERE user_id=? AND target_id=?",
            (int(user_id), int(target_id)),
        )
        await db.commit()
    await delete_user_target(int(target_id), int(user_id))
    await _ensure_user_routes(int(user_id))


async def list_target_sources(user_id: int, target_id: int) -> list[dict]:
    await _ensure_user_routes(int(user_id))
    sources = await list_user_sources(user_id)
    by_name = {str(row["username"]).lower(): row for row in sources}
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT username FROM target_source_routes
               WHERE user_id=? AND target_id=? ORDER BY username""",
            (int(user_id), int(target_id)),
        )
        names = [str(row[0]).lower() for row in await cur.fetchall()]
    return [by_name[name] for name in names if name in by_name]


async def get_target_for_source(user_id: int, source: str | None) -> dict | None:
    await ensure_channel_route_schema()
    normalized = (source or "").strip().lstrip("@").lower()
    if normalized:
        async with aiosqlite.connect(settings.database_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT t.* FROM target_source_routes r
                   JOIN user_targets t ON t.id=r.target_id AND t.user_id=r.user_id
                   WHERE r.user_id=? AND lower(r.username)=? AND t.is_active=1
                   LIMIT 1""",
                (int(user_id), normalized),
            )
            row = await cur.fetchone()
            if row:
                return dict(row)
    return await get_default_target(int(user_id))


async def _route_map(user_id: int) -> dict[str, dict]:
    await _ensure_user_routes(user_id)
    targets = {int(row["id"]): row for row in await list_user_targets(user_id)}
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            "SELECT username,target_id FROM target_source_routes WHERE user_id=?",
            (int(user_id),),
        )
        rows = await cur.fetchall()
    return {
        str(username).lower(): targets.get(int(target_id))
        for username, target_id in rows
        if int(target_id) in targets
    }


async def _target_open_url(context: ContextTypes.DEFAULT_TYPE, target: dict) -> str | None:
    ref = str(target.get("channel_ref") or "").strip()
    if ref.startswith("@"):
        return f"https://t.me/{ref[1:]}"
    if re.fullmatch(r"[A-Za-z0-9_]{4,64}", ref):
        return f"https://t.me/{ref}"
    try:
        chat = await context.bot.get_chat(ref)
        username = getattr(chat, "username", None)
        if username:
            return f"https://t.me/{username}"
    except Exception:
        pass
    return None


async def _show_channels(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = int(query.from_user.id)
    await _ensure_user_routes(uid)
    targets = await list_user_targets(uid)
    sources = await list_user_sources(uid)
    routes = await _route_map(uid)
    default_target = await get_default_target(uid)

    buttons: list[list[InlineKeyboardButton]] = []
    buttons.append([InlineKeyboardButton("📺 ПІДКЛЮЧЕНІ КАНАЛИ", callback_data="cw_noop")])
    for target in targets[:15]:
        target_id = int(target["id"])
        assigned = sum(1 for row in routes.values() if row and int(row["id"]) == target_id)
        title = str(target.get("title") or target.get("channel_ref") or "Канал")[:26]
        marker = "✅" if target.get("is_default") else "▫️"
        open_url = await _target_open_url(context, target)
        first = InlineKeyboardButton(
            f"{marker} 📺 {title}",
            url=open_url,
        ) if open_url else InlineKeyboardButton(
            f"{marker} 📺 {title}",
            callback_data=f"cw_target:{target_id}",
        )
        buttons.append([
            first,
            InlineKeyboardButton(f"⚙️ {assigned}", callback_data=f"cw_target:{target_id}"),
            InlineKeyboardButton("🗑", callback_data=f"cw_target_delete:{target_id}"),
        ])
    buttons.append([InlineKeyboardButton("➕ Додати канал публікації", callback_data="cw_target_add")])

    buttons.append([InlineKeyboardButton("📡 КАНАЛИ, З ЯКИХ ПАРСЯТЬСЯ НОВИНИ", callback_data="cw_noop")])
    for source in sources[:30]:
        username = str(source["username"]).lower()
        target = routes.get(username)
        target_title = str((target or {}).get("title") or (target or {}).get("channel_ref") or "без каналу")[:18]
        buttons.append([
            InlineKeyboardButton(f"📡 @{username}", url=f"https://t.me/{username}"),
            InlineKeyboardButton(f"→ {target_title}", callback_data=f"cw_source_route:{source['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"cw_source_delete:{source['id']}"),
        ])

    buttons.append([InlineKeyboardButton("🏠 Меню", callback_data="menu")])
    active_name = (default_target or {}).get("title") or (default_target or {}).get("channel_ref") or "не вибрано"
    await query.edit_message_text(
        "📺 <b>Мій робочий простір</b>\n\n"
        f"Підключених каналів: <b>{len(targets)}</b>\n"
        f"Каналів-джерел: <b>{len(sources)}</b>\n"
        f"Активний канал: <b>{html.escape(str(active_name))}</b>\n\n"
        "Натисни на назву каналу або джерела — Telegram відкриє його. "
        "Через ⚙️ біля каналу можна окремо налаштувати, з яких джерел саме в нього мають іти новини.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
        disable_web_page_preview=True,
    )


async def _show_target(query, context: ContextTypes.DEFAULT_TYPE, target_id: int) -> None:
    uid = int(query.from_user.id)
    await _ensure_user_routes(uid)
    targets = await list_user_targets(uid)
    target = next((row for row in targets if int(row["id"]) == int(target_id)), None)
    if not target:
        await query.answer("Канал не знайдено", show_alert=True)
        raise ApplicationHandlerStop

    all_sources = await list_user_sources(uid)
    assigned = await list_target_sources(uid, target_id)
    assigned_names = {str(row["username"]).lower() for row in assigned}
    title = str(target.get("title") or target.get("channel_ref") or "Канал")
    open_url = await _target_open_url(context, target)

    buttons: list[list[InlineKeyboardButton]] = []
    if open_url:
        buttons.append([InlineKeyboardButton(f"📺 Відкрити {title[:28]}", url=open_url)])
    if not target.get("is_default"):
        buttons.append([InlineKeyboardButton("🎯 Зробити активним", callback_data=f"cw_target_select:{target_id}")])
    buttons.append([InlineKeyboardButton("➕ Додати нове джерело сюди", callback_data=f"cw_source_add:{target_id}")])

    if assigned:
        buttons.append([InlineKeyboardButton("✅ ДЖЕРЕЛА ЦЬОГО КАНАЛУ", callback_data="cw_noop")])
        for source in assigned[:30]:
            username = str(source["username"]).lower()
            buttons.append([
                InlineKeyboardButton(f"📡 @{username}", url=f"https://t.me/{username}"),
                InlineKeyboardButton("🗑", callback_data=f"cw_source_delete:{source['id']}"),
            ])

    other_sources = [row for row in all_sources if str(row["username"]).lower() not in assigned_names]
    if other_sources:
        buttons.append([InlineKeyboardButton("➕ ПЕРЕНЕСТИ СЮДИ ІСНУЮЧЕ ДЖЕРЕЛО", callback_data="cw_noop")])
        for source in other_sources[:30]:
            username = str(source["username"]).lower()
            buttons.append([
                InlineKeyboardButton(f"➕ @{username}", callback_data=f"cw_assign:{target_id}:{source['id']}"),
                InlineKeyboardButton("↗", url=f"https://t.me/{username}"),
            ])

    buttons.append([InlineKeyboardButton("⬅️ Мої канали", callback_data="channels_menu")])
    await query.edit_message_text(
        f"⚙️ <b>{html.escape(title)}</b>\n\n"
        f"Джерел, прив'язаних до цього каналу: <b>{len(assigned)}</b>\n\n"
        "Кожне джерело належить одному каналу публікації. Якщо перенести джерело сюди, "
        "нові пости з нього будуть публікуватися саме в цей канал.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
        disable_web_page_preview=True,
    )


async def _show_source_route_picker(query, source_id: int) -> None:
    uid = int(query.from_user.id)
    sources = await list_user_sources(uid)
    source = next((row for row in sources if int(row["id"]) == int(source_id)), None)
    if not source:
        await query.answer("Джерело не знайдено", show_alert=True)
        raise ApplicationHandlerStop
    targets = await list_user_targets(uid)
    username = str(source["username"]).lower()
    buttons = [
        [InlineKeyboardButton(f"📡 Відкрити @{username}", url=f"https://t.me/{username}")]
    ]
    for target in targets:
        title = str(target.get("title") or target.get("channel_ref") or "Канал")[:28]
        buttons.append([
            InlineKeyboardButton(f"📺 {title}", callback_data=f"cw_assign:{target['id']}:{source_id}")
        ])
    buttons.append([InlineKeyboardButton("⬅️ Мої канали", callback_data="channels_menu")])
    await query.edit_message_text(
        f"🔀 <b>Куди направляти @{html.escape(username)}?</b>\n\n"
        "Вибери канал публікації:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def channel_workspace_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not is_authorized_id(q.from_user.id):
        return
    data = q.data or ""

    if data == "channels_menu" or data == "sources":
        await q.answer()
        context.user_data.pop("channel_workspace_input", None)
        await _show_channels(q, context)
        raise ApplicationHandlerStop
    if data == "cw_noop":
        await q.answer()
        raise ApplicationHandlerStop
    if data == "cw_target_add":
        await q.answer()
        context.user_data["channel_workspace_input"] = "target_add"
        await q.edit_message_text(
            "📺 <b>Додати канал публікації</b>\n\n"
            "1. Додай цього бота адміністратором каналу.\n"
            "2. Надішли <code>@username</code> каналу або numeric chat ID <code>-100...</code>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="channels_menu")]]),
        )
        raise ApplicationHandlerStop
    if data.startswith("cw_target:"):
        await q.answer()
        await _show_target(q, context, int(data.split(":", 1)[1]))
        raise ApplicationHandlerStop
    if data.startswith("cw_target_select:"):
        await q.answer("Активний канал змінено")
        target_id = int(data.split(":", 1)[1])
        await set_default_target(target_id, int(q.from_user.id))
        await _show_target(q, context, target_id)
        raise ApplicationHandlerStop
    if data.startswith("cw_target_delete:"):
        await q.answer()
        await _delete_target(int(q.from_user.id), int(data.split(":", 1)[1]))
        await _show_channels(q, context)
        raise ApplicationHandlerStop
    if data.startswith("cw_source_add:"):
        await q.answer()
        target_id = int(data.split(":", 1)[1])
        context.user_data["channel_workspace_input"] = f"source_add:{target_id}"
        await q.edit_message_text(
            "📡 <b>Додати джерело для цього каналу</b>\n\n"
            "Надішли <code>@username</code> або <code>https://t.me/channel</code>.\n"
            "Reader підхопить нове публічне джерело під час синхронізації.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data=f"cw_target:{target_id}")]]),
        )
        raise ApplicationHandlerStop
    if data.startswith("cw_assign:"):
        await q.answer("Джерело прив'язано")
        _, target_id, source_id = data.split(":", 2)
        sources = await list_user_sources(int(q.from_user.id))
        source = next((row for row in sources if int(row["id"]) == int(source_id)), None)
        if source:
            await assign_source_to_target(int(q.from_user.id), str(source["username"]), int(target_id))
        await _show_target(q, context, int(target_id))
        raise ApplicationHandlerStop
    if data.startswith("cw_source_route:"):
        await q.answer()
        await _show_source_route_picker(q, int(data.split(":", 1)[1]))
        raise ApplicationHandlerStop
    if data.startswith("cw_source_delete:"):
        await q.answer()
        await _delete_source(int(q.from_user.id), int(data.split(":", 1)[1]))
        await _show_channels(q, context)
        raise ApplicationHandlerStop


async def channel_workspace_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not is_authorized_id(update.effective_user.id):
        return
    mode = context.user_data.get("channel_workspace_input")
    if not mode:
        return
    text = (update.message.text or "").strip()
    uid = int(update.effective_user.id)

    if mode == "target_add":
        try:
            ref: str | int = text
            if re.fullmatch(r"-?\d+", text):
                ref = int(text)
            chat = await context.bot.get_chat(ref)
            member = await context.bot.get_chat_member(chat.id, context.bot.id)
            status = str(getattr(member, "status", "")).lower()
            if status not in {"administrator", "creator", "owner"}:
                raise ValueError("Бот має бути адміністратором цього каналу")
            title = getattr(chat, "title", None) or getattr(chat, "username", None) or str(chat.id)
            username = getattr(chat, "username", None)
            channel_ref = f"@{username}" if username else str(chat.id)
            target_id = await add_user_target(uid, channel_ref, str(title))
            context.user_data.pop("channel_workspace_input", None)
            await _ensure_user_routes(uid)
            await update.message.reply_text(
                f"✅ Канал <b>{html.escape(str(title))}</b> додано.\n\n"
                "Тепер відкрий його налаштування та додай/перенеси потрібні джерела.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Налаштувати джерела", callback_data=f"cw_target:{target_id}")]]),
            )
        except Exception as exc:
            await update.message.reply_text(
                f"❌ Не вдалося додати канал.\n<code>{html.escape(str(exc)[:900])}</code>",
                parse_mode="HTML",
            )
        raise ApplicationHandlerStop

    if mode.startswith("source_add:"):
        target_id = int(mode.split(":", 1)[1])
        try:
            username = _normalize_source(text)
            await assign_source_to_target(uid, username, target_id)
            context.user_data.pop("channel_workspace_input", None)
            await update.message.reply_text(
                f"✅ <b>@{html.escape(username)}</b> додано до цього каналу публікації.\n"
                "Reader підхопить джерело автоматично.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ До каналу", callback_data=f"cw_target:{target_id}")]]),
            )
        except Exception as exc:
            await update.message.reply_text(f"❌ {html.escape(str(exc))}", parse_mode="HTML")
        raise ApplicationHandlerStop


async def routed_publish_row(bot, row: dict) -> None:
    """Publish a post to the channel assigned to row.source, with album support."""
    from app import album_support

    user_id = int(row.get("user_id") or settings.admin_user_id or 0)
    target = await get_target_for_source(user_id, row.get("source"))
    if not target:
        raise RuntimeError("TARGET_CHANNEL_NOT_CONFIGURED: add a publishing channel in 'Мої канали'")
    destination = target["channel_ref"]
    text = post_html(row.get("rewritten_text") or "")

    items = await album_support.get_news_media(int(row["id"]))
    if items:
        await album_support._send_file_id_media_set(
            bot,
            destination,
            items,
            caption=text,
            parse_mode="HTML",
        )
        return

    media_type = row.get("media_type")
    file_id = row.get("media_file_id")
    if media_type == "photo" and file_id:
        await bot.send_photo(destination, photo=file_id, caption=text, parse_mode="HTML")
    elif media_type == "video" and file_id:
        await bot.send_video(destination, video=file_id, caption=text, parse_mode="HTML", supports_streaming=True)
    else:
        await bot.send_message(destination, text, parse_mode="HTML", disable_web_page_preview=True)


async def routed_send_preview(context: ContextTypes.DEFAULT_TYPE, row: dict, chat_id: int) -> None:
    from app import album_support, publish_ui

    user_id = int(row.get("user_id") or chat_id)
    target = await get_target_for_source(user_id, row.get("source"))
    target_name = (target or {}).get("title") or (target or {}).get("channel_ref") or "не налаштовано"
    text = post_html(row.get("rewritten_text") or "")
    items = await album_support.get_news_media(int(row["id"]))

    await context.bot.send_message(
        chat_id,
        f"👁 <b>Фінальне прев’ю поста #{row['id']}</b>\n"
        f"📺 Канал: <b>{html.escape(str(target_name))}</b>\n"
        f"📡 Джерело: <b>@{html.escape(str(row.get('source') or '—'))}</b>\n\n"
        "Нижче пост виглядає так, як буде виглядати в каналі.",
        parse_mode="HTML",
    )

    if len(items) > 1:
        await album_support._send_file_id_media_set(context.bot, chat_id, items, caption=text, parse_mode="HTML")
        await context.bot.send_message(
            chat_id,
            "Керування цим альбомом:",
            reply_markup=publish_ui._preview_buttons(int(row["id"])),
        )
        return

    buttons = publish_ui._preview_buttons(int(row["id"]))
    if row.get("media_type") == "photo" and row.get("media_file_id"):
        await context.bot.send_photo(chat_id, photo=row["media_file_id"], caption=text, parse_mode="HTML", reply_markup=buttons)
    elif row.get("media_type") == "video" and row.get("media_file_id"):
        await context.bot.send_video(chat_id, video=row["media_file_id"], caption=text, parse_mode="HTML", supports_streaming=True, reply_markup=buttons)
    else:
        await context.bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=buttons)


def install_channel_routing() -> None:
    """Install DB schema and source->publication-channel routing after album support."""
    from app import album_support, main as main_mod, publish_ui, publishing

    if getattr(main_mod, "_channel_routing_installed", False):
        return

    original_init_db = main_mod.init_db

    async def init_db_with_channel_routes():
        await original_init_db()
        await ensure_channel_route_schema()

    main_mod.init_db = init_db_with_channel_routes
    publishing.publish_row = routed_publish_row
    publish_ui.publish_row = routed_publish_row
    publish_ui._send_preview = routed_send_preview
    album_support.publish_row = routed_publish_row
    album_support._send_preview = routed_send_preview
    main_mod._channel_routing_installed = True
    log.info("Installed per-publication-channel source routing")


def register_channel_workspace_ui(app: Application) -> None:
    pattern = (
        r"^(channels_menu$|sources$|cw_noop$|cw_target_add$|cw_target:|cw_target_select:|"
        r"cw_target_delete:|cw_source_add:|cw_assign:|cw_source_route:|cw_source_delete:)"
    )
    app.add_handler(CallbackQueryHandler(channel_workspace_callback, pattern=pattern), group=-30)
    app.add_handler(MessageHandler(filters.ALL, channel_workspace_input_handler), group=-30)
