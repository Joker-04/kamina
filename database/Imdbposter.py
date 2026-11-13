import asyncio
import logging
import re
from io import BytesIO

import aiohttp
from PIL import Image
from imdb import Cinemagoer

from info import DEENDAYAL_IMAGE_FETCH

logger = logging.getLogger(__name__)
ia = Cinemagoer()
LONG_IMDB_DESCRIPTION = False
TVMAZE_SEARCH = "https://api.tvmaze.com/search/shows"


def list_to_str(items):
    return ", ".join(map(str, items)) if items else ""


def _split_title_year(query, file=None):
    text = str(query or "").strip()
    match = re.search(r"\b((?:19|20)\d{2})\b", text)
    year = match.group(1) if match else None
    if year:
        text = text[:match.start()].strip()
    if not year and file:
        match = re.search(r"\b((?:19|20)\d{2})\b", str(file))
        year = match.group(1) if match else None
    text = re.sub(r"(?i)\b(?:S\d{1,2}(?:E\d{1,3})?|Season\s*\d+)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ._-")
    return text, year


def _normalise_title(value):
    value = str(value or "").lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _title_match_score(query, candidate):
    """Return a conservative similarity score to avoid unrelated poster matches."""
    from difflib import SequenceMatcher

    left = _normalise_title(query)
    right = _normalise_title(candidate)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    left_words, right_words = set(left.split()), set(right.split())
    overlap = len(left_words & right_words) / max(1, len(left_words))
    return max(SequenceMatcher(None, left, right).ratio(), overlap)


def _get_imdb_details_sync(query, id=False, file=None):
    if id:
        movie_id = str(query).removeprefix("tt")
    else:
        title, year = _split_title_year(query, file)
        if not title:
            return None
        results = ia.search_movie(title, results=10)
        if not results:
            return None

        ranked = []
        for item in results:
            candidate_title = item.get("title") or ""
            score = _title_match_score(title, candidate_title)
            candidate_year = str(item.get("year") or "")
            kind = item.get("kind") or ""
            # For films, a supplied year must agree. TV seasons may carry a later
            # release year than the series premiere, so title confidence leads there.
            if year and kind == "movie" and candidate_year and candidate_year != year:
                continue
            if score >= (0.62 if len(title.split()) > 1 else 0.80):
                ranked.append((score, item))
        if not ranked:
            logger.info("IMDb candidates rejected as weak title matches for %r", title)
            return None
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        preferred = [pair for pair in ranked if pair[1].get("kind") in ("movie", "tv series", "tv mini series")]
        movie_id = (preferred or ranked)[0][1].movieID

    movie = ia.get_movie(movie_id)
    plot = movie.get("plot")
    if plot:
        plot = plot[0]
    else:
        plot = movie.get("plot outline")
    if plot and len(plot) > 800:
        plot = plot[:800] + "..."
    imdb_id = str(movie.get("imdbID") or movie_id)
    return {
        "title": movie.get("title"),
        "votes": movie.get("votes"),
        "aka": list_to_str(movie.get("akas")),
        "seasons": movie.get("number of seasons"),
        "box_office": movie.get("box office"),
        "localized_title": movie.get("localized title"),
        "kind": movie.get("kind"),
        "imdb_id": f"tt{imdb_id}",
        "cast": list_to_str(movie.get("cast")),
        "runtime": list_to_str(movie.get("runtimes")),
        "countries": list_to_str(movie.get("countries")),
        "certificates": list_to_str(movie.get("certificates")),
        "languages": list_to_str(movie.get("languages")),
        "director": list_to_str(movie.get("director")),
        "writer": list_to_str(movie.get("writer")),
        "producer": list_to_str(movie.get("producer")),
        "composer": list_to_str(movie.get("composer")),
        "cinematographer": list_to_str(movie.get("cinematographer")),
        "music_team": list_to_str(movie.get("music department")),
        "distributors": list_to_str(movie.get("distributors")),
        "release_date": movie.get("original air date") or movie.get("year") or "N/A",
        "year": movie.get("year"),
        "genres": list_to_str(movie.get("genres")),
        "poster_url": movie.get("full-size cover url") or movie.get("cover url"),
        "plot": plot,
        "rating": str(movie.get("rating") or ""),
        "url": f"https://www.imdb.com/title/tt{imdb_id}",
    }


async def _search_tvmaze(query, file=None):
    """Search TVmaze for a TV-show match. TVmaze's public API needs no API key."""
    title, year = _split_title_year(query, file)
    if not title:
        return None

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(TVMAZE_SEARCH, params={"q": title}) as response:
            if response.status != 200:
                logger.warning("TVmaze search returned HTTP %s for %r", response.status, title)
                return None
            results = await response.json()

    if not results:
        return None

    shows = [entry.get("show", {}) for entry in results if entry.get("show")]
    if not shows:
        return None

    ranked = []
    for candidate in shows:
        score = _title_match_score(title, candidate.get("name") or "")
        premiered_year = str(candidate.get("premiered") or "")[:4]
        if year and premiered_year == year:
            score = min(1.0, score + 0.10)
        if score >= (0.62 if len(title.split()) > 1 else 0.80):
            ranked.append((score, candidate))
    if not ranked:
        logger.info("TVmaze candidates rejected as weak title matches for %r", title)
        return None

    ranked.sort(key=lambda pair: pair[0], reverse=True)
    show = ranked[0][1]
    image = show.get("image") or {}
    poster_url = image.get("original") or image.get("medium")
    premiered = show.get("premiered") or ""
    summary = re.sub(r"<[^>]+>", " ", show.get("summary") or "")
    summary = re.sub(r"\s+", " ", summary).strip()
    rating = (show.get("rating") or {}).get("average")

    return {
        "title": show.get("name") or title,
        "year": int(premiered[:4]) if len(premiered) >= 4 and premiered[:4].isdigit() else None,
        "genres": list_to_str(show.get("genres") or []),
        "plot": summary[:800] + ("..." if len(summary) > 800 else ""),
        "poster_url": poster_url,
        "imdb_id": (show.get("externals") or {}).get("imdb"),
        "rating": str(rating or ""),
        "languages": show.get("language") or "",
        "url": show.get("url") or "",
        "kind": "tv series",
        "votes": None,
        "runtime": str(show.get("averageRuntime") or show.get("runtime") or ""),
        "release_date": premiered or "N/A",
    }


async def get_movie_details(query, id=False, file=None):
    """Find metadata with IMDb first, then use TVmaze as a no-key TV-series fallback."""
    if not id:
        try:
            details = await asyncio.to_thread(_get_imdb_details_sync, query, False, file)
            if details and details.get("poster_url"):
                logger.info("IMDb metadata found a poster for %r", query)
                return details
            if details:
                logger.info("IMDb matched %r but returned no poster; trying TVmaze", query)
            else:
                logger.info("IMDb found no match for %r; trying TVmaze", query)
        except Exception:
            logger.exception("IMDb lookup failed for %r; trying TVmaze", query)

        try:
            details = await _search_tvmaze(query, file)
            if details:
                logger.info("TVmaze metadata found for %r (poster_found=%s)", query, bool(details.get("poster_url")))
                return details
        except Exception:
            logger.exception("TVmaze lookup failed for %r", query)
        return None

    try:
        return await asyncio.to_thread(_get_imdb_details_sync, query, True, file)
    except Exception:
        logger.exception("IMDb ID lookup failed for %r", query)
        return None


async def fetch_image(url, size=(720, 720)):
    if not DEENDAYAL_IMAGE_FETCH:
        logger.info("Image fetching is disabled")
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    logger.warning("Failed to fetch image: HTTP %s", response.status)
                    return None
                content = await response.read()
        image = Image.open(BytesIO(content))
        image = image.resize(size, Image.LANCZOS)
        output = BytesIO()
        image.save(output, format="JPEG")
        output.seek(0)
        return output
    except Exception:
        logger.exception("Could not fetch poster image")
        return None
