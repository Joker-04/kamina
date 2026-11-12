from pyrogram import Client, filters
from info import CHANNELS
from database.ia_filterdb import save_file, unpack_new_file_id
from database.movie_catalogue import publish_movie

media_filter = filters.document | filters.video | filters.audio


@Client.on_message(filters.chat(CHANNELS) & media_filter)
async def media(bot, message):
    """Index the Telegram media and publish new files to KAMINA."""
    for file_type in ("document", "video", "audio"):
        media = getattr(message, file_type, None)
        if media is not None:
            break
    else:
        return

    media.file_type = file_type
    media.caption = message.caption
    saved, status = await save_file(bot, media)

    # Only publish newly indexed files. Existing duplicates are left untouched.
    if saved:
        file_id, _ = unpack_new_file_id(media.file_id)
        caption = getattr(message.caption, "html", message.caption) if message.caption else None
        await publish_movie(file_id, media.file_name, media.file_size, caption)
