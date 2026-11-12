import logging

from pyrogram import Client, filters
from info import CHANNELS, FILE_STORE_CHANNEL
from database.ia_filterdb import save_file, unpack_new_file_id
from database.movie_catalogue import publish_movie

logger = logging.getLogger(__name__)

# Watch both configured indexing channels and file-storage channels.
WATCHED_CHANNELS = sorted(set(CHANNELS + FILE_STORE_CHANNEL))
media_filter = filters.document | filters.video | filters.audio


@Client.on_message(filters.chat(WATCHED_CHANNELS) & media_filter)
async def media(bot, message):
    """Index Telegram media and publish it to KAMINA's website catalogue."""
    try:
        logger.info(
            "Received media upload: chat_id=%s message_id=%s",
            message.chat.id,
            message.id,
        )
        for file_type in ("document", "video", "audio"):
            media_file = getattr(message, file_type, None)
            if media_file is not None:
                break
        else:
            logger.warning("Upload event had no supported media object: message_id=%s", message.id)
            return

        media_file.file_type = file_type
        media_file.caption = message.caption
        saved, status = await save_file(bot, media_file)

        if not saved:
            logger.warning(
                "Media was not newly indexed: file_name=%s status=%s",
                getattr(media_file, "file_name", "unknown"),
                status,
            )
            return

        file_id, _ = unpack_new_file_id(media_file.file_id)
        caption = getattr(message.caption, "html", message.caption) if message.caption else None
        logger.info("Starting website catalogue publishing for %s", media_file.file_name)
        await publish_movie(file_id, media_file.file_name, media_file.file_size, caption)
    except Exception:
        logger.exception(
            "Media indexing/publishing failed: chat_id=%s message_id=%s",
            getattr(getattr(message, "chat", None), "id", None),
            getattr(message, "id", None),
        )
