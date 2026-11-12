import asyncio
import logging
import re
from datetime import datetime, timezone

from database.ia_filterdb import db
from database.Imdbposter import get_movie_details

logger = logging.getLogger(__name__)
catalogue_collection = db["kamina_movies"]
_metadata_lock = asyncio.Lock()
_background_tasks = set()


def clean_movie_title(file_name, caption=None):
    """Use an explicit Title: caption when present; otherwise clean the filename."""
    if caption:
        match = re.search(r"(?im)^\\s*/?title\\s*:\\s*(.+?)\\s*$", str(caption))
        if match:
            return match.group(1).strip()

    title = str(file_name or "Untitled")
    title = re.sub(r"(?i)\\.(mkv|mp4|avi|mov|m4v|webm|mpeg|mpg|wmv)$", "", title)
    title = re.sub(
        r"(?i)\\b(2160p|1440p|1080p|720p|480p|360p|4k|8k|bluray|blu[ -]?ray|"
        r"web[ -]?dl|webrip|hdrip|brrip|dvdrip|hdtv|x264|x265|hevc|aac|"
        r"10bit|5\\.1|2\\.0)\\b.*$",
        "",
        title,
    )
    title = re.sub(r"[._+\\-]+", " ", title)
    title = re.sub(r"[\\[\\](){}]", " ", title)
    return re.sub(r"\\s+", " ", title).strip() or "Untitled"


async def _fetch_and_store_metadata(file_id, title, file_name):
    try:
        logger.info("Starting IMDb metadata lookup: title=%r file_name=%r", title, file_name)
        # get_movie_details is async, while Cinemagoer does blocking work.
        # Run its coroutine in a worker thread so the bot event loop stays responsive.
        async with _metadata_lock:
            details = await asyncio.to_thread(
                lambda: asyncio.run(get_movie_details(title, False, file_name))
            )

        if not details:
            logger.warning("IMDb returned no match for new upload: title=%r", title)
            await catalogue_collection.update_one(
                {"file_id": file_id},
                {"$set": {"metadata_status": "no_match"}},
            )
            return

        poster_url = details.get("poster_url")
        values = {
            "title": details.get("title") or title,
            "year": details.get("year"),
            "genres": details.get("genres") or "",
            "plot": details.get("plot") or "",
            "poster_url": poster_url,
            "imdb_id": details.get("imdb_id"),
            "rating": details.get("rating"),
            "languages": details.get("languages") or "",
            "imdb_url": details.get("url"),
            "metadata_updated_at": datetime.now(timezone.utc),
            "metadata_status": "ready" if poster_url else "no_poster",
        }
        await catalogue_collection.update_one(
            {"file_id": file_id},
            {"$set": values},
            upsert=True,
        )
        logger.info(
            "Website metadata saved: title=%r status=%s poster_found=%s",
            title,
            values["metadata_status"],
            bool(poster_url),
        )
    except Exception:
        logger.exception("Could not fetch/store IMDb metadata for %s", title)


async def publish_movie(file_id, file_name, file_size, caption=None):
    """Publish a newly indexed Telegram file to KAMINA's website catalogue."""
    title = clean_movie_title(file_name, caption)
    now = datetime.now(timezone.utc)
    await catalogue_collection.update_one(
        {"file_id": file_id},
        {
            "$set": {
                "file_id": file_id,
                "file_name": file_name or "download",
                "file_size": int(file_size or 0),
                "title": title,
                "poster_url": None,
                "created_at": now,
                "download_path": "/download/" + file_id,
            },
            "$setOnInsert": {"metadata_status": "pending"},
        },
        upsert=True,
    )

    logger.info("New upload added to website catalogue: title=%r file_id=%s", title, file_id)
    task = asyncio.create_task(_fetch_and_store_metadata(file_id, title, file_name))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
