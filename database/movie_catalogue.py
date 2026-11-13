import asyncio
import logging
import re
import secrets
from datetime import datetime, timezone

from database.ia_filterdb import db
from database.Imdbposter import get_movie_details

logger = logging.getLogger(__name__)
catalogue_collection = db["kamina_movies"]
_metadata_lock = asyncio.Lock()
_background_tasks = set()


def clean_movie_title(file_name, caption=None):
    """Extract a searchable title from noisy Telegram release filenames."""
    if caption:
        match = re.search(r"(?im)^\s*/?title\s*:\s*(.+?)\s*$", str(caption))
        if match and match.group(1).strip():
            return match.group(1).strip()

    title = str(file_name or "Untitled")
    title = re.sub(r"(?i)\.(mkv|mp4|avi|mov|m4v|webm|mpeg|mpg|wmv)$", "", title)
    title = re.sub(r"@\w+", " ", title)
    title = re.sub(r"\[[^\]]*(?:@|movie|movies|channel|clipmate)[^\]]*\]", " ", title, flags=re.I)
    title = re.sub(r"[_\.•|~]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" -")

    title = re.sub(r"(?i)^(?:(?:2160p|1440p|1080p|720p|480p|360p|4k|8k)\s*)+", "", title)
    title = re.sub(r"(?i)^(?:(?:E\d{1,3}(?:\s*[-–]\s*\d{1,3})?|S\d{1,2}|Season\s*\d+|Complete)\s*)+", "", title)
    title = re.sub(r"(?i)^(?:sample\s+video|sample)\s+", "", title)

    years = list(re.finditer(r"\b(?:19|20)\d{2}\b", title))
    if years:
        year_match = years[-1] if len(years) > 1 and title[:years[0].start()].strip().isdigit() else years[0]
        title = title[:year_match.end()]
    else:
        title = re.sub(r"(?i)\b(?:S\d{1,2}(?:E\d{1,3})?|Season\s*\d+)\b.*$", "", title)
        title = re.sub(
            r"(?i)\b(hindi|english|tamil|telugu|malayalam|kannada|marathi|"
            r"bengali|bangla|punjabi|gujarati|urdu|dual audio)\b.*$", "", title
        )

    title = re.sub(
        r"(?i)\b(2160p|1440p|1080p|720p|480p|360p|4k|8k|bluray|blu[ -]?ray|"
        r"web[ -]?dl|webrip|hdrip|brrip|dvdrip|hdtv|hdtc|x264|x265|hevc|aac|"
        r"10bit|5\.1|2\.0|cleaned|proper|repack|combined|hindi|english|tamil|"
        r"telugu|malayalam|kannada|marathi|dual audio)\b.*$", "", title
    )
    title = re.sub(r"[\[\](){}]", " ", title)
    title = re.sub(r"[._+\-•|:~]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" -")
    if not title or re.fullmatch(r"[a-f0-9]{20,}", title, flags=re.I):
        return "Untitled"
    return title


def extract_catalogue_info(file_name, title=None):
    """Identify a TV episode, movie quality, and a stable grouping key from a release filename."""
    raw = str(file_name or "")
    episode = re.search(r"(?i)\bS(\d{1,2})\s*E(\d{1,3})\b|\bSeason\s*(\d{1,2})\s*[- ]?Episode\s*(\d{1,3})\b|\b(\d{1,2})x(\d{1,3})\b", raw)
    if episode:
        groups = episode.groups()
        season = int(groups[0] or groups[2] or groups[4])
        number = int(groups[1] or groups[3] or groups[5])
        group_title = (title or re.sub(r"(?i)[._]+", " ", raw[:episode.start()])).strip(" -_.[]()")
        group_title = re.sub(r"(?i)\b(complete|season|series)\b.*$", "", group_title).strip(" -_.")
        group_title = group_title or title or "Untitled Series"
        kind = "series"
        quality = None
    else:
        season = number = None
        kind = "movie"
        group_title = clean_movie_title(raw) or title or "Untitled"
        quality_match = re.search(r"(?i)\b(2160p|1440p|1080p|720p|480p|360p|4k|8k)\b", raw)
        quality = quality_match.group(1).upper() if quality_match else "File"
    year_match = re.search(r"\b(?:19|20)\d{2}\b", raw)
    year = year_match.group(0) if year_match else None
    normalized = re.sub(r"[^a-z0-9]+", " ", group_title.lower()).strip()
    identity = f"{kind}|{normalized}|{year or ''}"
    group_key = __import__("hashlib").sha1(identity.encode("utf-8")).hexdigest()[:16]
    return {
        "catalogue_type": kind,
        "catalogue_group_key": group_key,
        "group_title": group_title,
        "catalogue_year": year,
        "season_number": season,
        "episode_number": number,
        "quality_label": quality,
    }


async def _fetch_and_store_metadata(file_id, title, file_name):
    try:
        logger.info("Starting strict IMDb/TVmaze metadata lookup: title=%r file_name=%r", title, file_name)
        async with _metadata_lock:
            details = await get_movie_details(title, False, file_name)

        if not details:
            logger.warning("No safe IMDb/TVmaze match for new upload: title=%r", title)
            await catalogue_collection.update_one(
                {"file_id": file_id},
                {"$set": {"metadata_status": "no_match", "poster_url": None}},
            )
            return

        if details.get("needs_review"):
            token = secrets.token_hex(5)
            candidates = []
            for candidate in details.get("candidates", [])[:5]:
                if not candidate.get("imdb_id"):
                    continue
                candidates.append({
                    "title": candidate.get("title") or title,
                    "year": candidate.get("year"),
                    "kind": candidate.get("kind") or "movie",
                    "imdb_id": candidate.get("imdb_id"),
                    "poster_url": candidate.get("poster_url"),
                    "url": candidate.get("url"),
                })
            if not candidates:
                await catalogue_collection.update_one(
                    {"file_id": file_id},
                    {"$set": {"metadata_status": "no_match", "poster_url": None}},
                )
                return
            await catalogue_collection.update_one(
                {"file_id": file_id},
                {"$set": {
                    "metadata_status": "needs_review",
                    "poster_url": None,
                    "poster_review_id": token,
                    "poster_candidates": candidates,
                    "poster_search_title": details.get("search_title") or title,
                }},
            )
            logger.info("Poster selection required: title=%r review_id=%s candidates=%s", title, token, len(candidates))
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
            "poster_review_id": None,
            "poster_candidates": [],
        }
        await catalogue_collection.update_one(
            {"file_id": file_id},
            {"$set": values},
            upsert=True,
        )
        logger.info(
            "Website metadata saved: title=%r source=%s status=%s poster_found=%s",
            title, values["metadata_source"], values["metadata_status"], bool(poster_url),
        )
    except Exception:
        logger.exception("Could not fetch/store movie metadata for %s", title)


async def publish_movie(file_id, file_name, file_size, caption=None):
    """Publish a newly indexed Telegram file to KAMINA's website catalogue."""
    title = clean_movie_title(file_name, caption)
    grouping = extract_catalogue_info(file_name, title)
    if grouping["catalogue_type"] == "series":
        title = grouping["group_title"]
    now = datetime.now(timezone.utc)
    await catalogue_collection.update_one(
        {"file_id": file_id},
        {
            "$set": {
                "file_id": file_id,
                "file_name": file_name or "download",
                "file_size": int(file_size or 0),
                "title": title,
                **grouping,
                "poster_url": None,
                "created_at": now,
                "download_path": "/download/" + file_id,
                "metadata_status": "pending",
                "poster_review_id": None,
                "poster_candidates": [],
            },
        },
        upsert=True,
    )

    logger.info("New upload added to website catalogue: title=%r file_id=%s", title, file_id)
    task = asyncio.create_task(_fetch_and_store_metadata(file_id, title, file_name))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
