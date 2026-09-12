import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def install_compact_channel_page() -> None:
    """Keep the main workspace page compact; sources stay inside each channel's gear view."""
    from app import channel_workspace as cw

    async def compact_show_channels(query, context) -> None:
        uid = int(query.from_user.id)
        await cw._ensure_user_routes(uid)
        targets = await cw.list_user_targets(uid)
        sources = await cw.list_user_sources(uid)
        routes = await cw._route_map(uid)
        default_target = await cw.get_default_target(uid)

        buttons: list[list[InlineKeyboardButton]] = []
        buttons.append([InlineKeyboardButton("📺 ПІДКЛЮЧЕНІ КАНАЛИ", callback_data="cw_noop")])

        for target in targets[:15]:
            target_id = int(target["id"])
            assigned = sum(1 for row in routes.values() if row and int(row["id"]) == target_id)
            title = str(target.get("title") or target.get("channel_ref") or "Канал")[:26]
            marker = "✅" if target.get("is_default") else "▫️"
            open_url = await cw._target_open_url(context, target)

            if open_url:
                first = InlineKeyboardButton(f"{marker} 📺 {title}", url=open_url)
            else:
                first = InlineKeyboardButton(f"{marker} 📺 {title}", callback_data=f"cw_target:{target_id}")

            buttons.append([
                first,
                InlineKeyboardButton(f"⚙️ {assigned}", callback_data=f"cw_target:{target_id}"),
                InlineKeyboardButton("🗑", callback_data=f"cw_target_delete:{target_id}"),
            ])

        buttons.append([InlineKeyboardButton("➕ Додати канал публікації", callback_data="cw_target_add")])
        buttons.append([InlineKeyboardButton("🏠 Меню", callback_data="menu")])

        active_name = (
            (default_target or {}).get("title")
            or (default_target or {}).get("channel_ref")
            or "не вибрано"
        )

        await query.edit_message_text(
            "📺 <b>Мій робочий простір</b>\n\n"
            f"Підключених каналів: <b>{len(targets)}</b>\n"
            f"Джерел загалом: <b>{len(sources)}</b>\n"
            f"Активний канал: <b>{html.escape(str(active_name))}</b>\n\n"
            "Натисни на назву каналу, щоб відкрити його. "
            "Через ⚙️ біля кожного каналу відкриваються всі джерела, які парсяться саме для нього, "
            "і там же можна їх додавати, переносити або видаляти.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True,
        )

    cw._show_channels = compact_show_channels
