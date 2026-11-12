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
    """Extract a title and year while dropping common release/file tags."""
    if caption:
        match = re.search(r"(?im)^\s*/?title\s*:\s*(.+?)\s*$", str(caption))
        if match and match.group(1).strip():
            return match.group(1).strip()

    title = str(file_name or "Untitled")
    title = re.sub(r"(?i)\.(mkv|mp4|avi|mov|m4v|webm|mpeg|mpg|wmv)$", "", title)
    title = re.sub(r"(?i)@\w+", " ", title)
    title = re.sub(r"(?i)^\s*(2160p|1440p|1080p|720p|480p|360p|4k|8k)[\s._•-]*", "", title)
    title = re.sub(
        r"(?i)\b(2160p|1440p|1080p|720p|480p|360p|4k|8k|bluray|blu[ -]?ray|"
        r"web[ -]?dl|webrip|hdrip|brrip|dvdrip|hdtv|hdtc|x264|x265|hevc|aac|"
        r"10bit|5\.1|2\.0|cleaned|proper|repack|combined)\b.*$",
        "",
        title,
    )
    year_match = re.search(r"\b(?:19|20)\d{2}\b", title)
    if year_match:
        title = title[:year_match.end()]
    else:
        title = re.sub(r"(?i)\bS\d{1,2}(?:E\d{1,3})?\b.*$", "", title)
        title = re.sub(r"(?i)\b(hindi|english|tamil|telugu|malayalam|kannada|"
                       r"marathi|bengali|bangla|punjabi|gujarati|urdu|dual audio)\b.*$", "", title)
    title = re.sub(r"[\[\](){}]", " ", title)
    title = re.sub(r"[._+\-•|:~]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title or "Untitled"


async def _fetch_and_store_metadata(file_id, title, file_name):
    try:
        logger.info("Starting IMDb/TVmaze metadata lookup: title=%r file_name=%r", title, file_name)
        async with _metadata_lock:
            details = await get_movie_details(title, False, file_name)

        if not details:
            logger.warning("IMDb and TVmaze returned no match for new upload: title=%r", title)
            await catalogue_collection.update_one(
                {"file_id": file_id},
                {"$set": {"metadata_status": "no_match"}},
            )
            return

        poster_url = details.get("poster_url")
        source_url = details.get("url") or ""
        metadata_source = "tvmaze" if "tvmaze.com" in source_url else "imdb"
        values = {
            "title": details.get("title") or title,
            "year": details.get("year"),
            "genres": details.get("genres") or "",
            "plot": details.get("plot") or "",
            "poster_url": poster_url,
            "imdb_id": details.get("imdb_id"),
            "rating": details.get("rating"),
            "languages": details.get("languages") or "",
            "imdb_url": source_url,
            "metadata_updated_at": datetime.now(timezone.utc),
            "metadata_status": "ready" if poster_url else "no_poster",
            "metadata_source": metadata_source,
        }
        await catalogue_collection.update_one(
            {"file_id": file_id},
            {"$set": values},
            upsert=True,
        )
        logger.info(
            "Website metadata saved: title=%r source=%s status=%s poster_found=%s",
            title,
            values["metadata_source"],
            values["metadata_status"],
            bool(poster_url),
        )
    except Exception:
        logger.exception("Could not fetch/store movie metadata for %s", title)


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
