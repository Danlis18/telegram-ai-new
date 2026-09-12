from contextlib import suppress
from io import BytesIO


def install_album_edit_fix() -> None:
    """Patch album batch editing for python-telegram-bot immutable InputMedia objects."""
    from app import album_support

    if getattr(album_support, "_album_edit_fix_installed", False):
        return

    async def fixed_edit_media_set(bot, row: dict, user_id: int, progress_message=None):
        items = await album_support.get_news_media(int(row["id"]))
        if not items:
            raise RuntimeError("Media set is empty")

        photo_items = [item for item in items if item["media_type"] == "photo"]
        if not photo_items:
            return row, 0, []

        # Store raw media values first. InputMediaPhoto/InputMediaVideo objects are
        # immutable in python-telegram-bot 22, so caption/parse_mode must be passed
        # in their constructors instead of assigning media.caption afterwards.
        prepared = []
        open_buffers: list[BytesIO] = []
        failures: list[str] = []
        edited_count = 0
        photo_index = 0
        photo_total = len(photo_items)

        for item in items:
            if item["media_type"] == "video":
                prepared.append((item, False, None, item["original_file_id"]))
                continue

            photo_index += 1
            if progress_message is not None:
                with suppress(Exception):
                    await progress_message.edit_text(
                        f"🧹 <b>Редагую всі фото поста #{row['id']}</b>\n\n"
                        f"Фото <b>{photo_index}/{photo_total}</b> · медіа {item['position'] + 1}/{len(items)}\n"
                        "Зберігаю спортивну графіку, рахунок і логотипи команд; прибираю тільки сторонню рекламу/водяні знаки.",
                        parse_mode="HTML",
                    )

            try:
                source = await album_support._download_bot_file(bot, item["original_file_id"])
                edited = await album_support.generate_news_image(
                    row.get("rewritten_text") or row.get("original_text") or "",
                    source_image=source,
                )
                buf = BytesIO(edited)
                buf.name = f"sports_news_{row['id']}_{item['position'] + 1}.jpg"
                open_buffers.append(buf)
                prepared.append((item, True, None, buf))
                edited_count += 1
            except Exception as exc:
                error = f"{type(exc).__name__}: {str(exc)[:500]}"
                failures.append(f"Фото {photo_index}/{photo_total}: {error}")
                prepared.append((item, False, error, item["original_file_id"]))
                album_support.log.exception(
                    "Album photo edit failed news_id=%s media_id=%s",
                    row["id"],
                    item["id"],
                )

        try:
            total = len(prepared)
            if total > 1:
                payload = []
                for index, (item, _success, _error, media_value) in enumerate(prepared, 1):
                    kind = item["media_type"]
                    label = f"{'🎥 Відео' if kind == 'video' else '🖼 Фото'} {index}/{total} · готово"
                    payload.append(
                        album_support._upload_media(
                            kind,
                            media_value,
                            caption=label,
                            parse_mode="HTML",
                        )
                    )
                sent_messages = list(await bot.send_media_group(int(user_id), media=payload))
            else:
                item, _success, _error, media_value = prepared[0]
                if item["media_type"] == "video":
                    sent_messages = [
                        await bot.send_video(
                            int(user_id),
                            video=media_value,
                            caption="🎥 Відео 1/1 · готово",
                            parse_mode="HTML",
                            supports_streaming=True,
                        )
                    ]
                else:
                    sent_messages = [
                        await bot.send_photo(
                            int(user_id),
                            photo=media_value,
                            caption="🖼 Фото 1/1 · готово",
                            parse_mode="HTML",
                        )
                    ]

            if len(sent_messages) != len(prepared):
                raise RuntimeError("Telegram returned incomplete edited media group")

            for (item, success, error, _media_value), sent_message in zip(prepared, sent_messages):
                if item["media_type"] == "video":
                    await album_support._set_media_result(item["id"], None, None)
                    continue
                if success:
                    await album_support._set_media_result(
                        item["id"],
                        album_support._sent_file_id(sent_message, "photo"),
                        None,
                    )
                else:
                    await album_support._set_media_result(item["id"], None, error)

            await album_support._sync_legacy_media(int(row["id"]))
        finally:
            for buf in open_buffers:
                with suppress(Exception):
                    buf.close()

        updated = await album_support.get_news(int(row["id"]))
        return updated or row, edited_count, failures

    album_support._edit_media_set = fixed_edit_media_set
    album_support._album_edit_fix_installed = True
    album_support.log.info("Installed immutable InputMedia multi-photo batch edit fix")
