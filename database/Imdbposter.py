import asyncio
import logging
import os
import re
from io import BytesIO

import aiohttp
from PIL import Image
from imdb import Cinemagoer

from info import DEENDAYAL_IMAGE_FETCH

logger = logging.getLogger(__name__)
ia = Cinemagoer()
LONG_IMDB_DESCRIPTION = False
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE = "https://image.tmdb.org/t/p/w500"


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
    text = re.sub(r"\s+", " ", text).strip(" ._-")
    return text, year


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
        if year:
            matching = [item for item in results if str(item.get("year", "")) == year]
            if matching:
                results = matching
        preferred = [item for item in results if item.get("kind") in ("movie", "tv series", "tv mini series", "tv series episode")]
        movie_id = (preferred or results)[0].movieID

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


async def _search_tmdb(query, file=None):
    if not TMDB_API_KEY:
        logger.warning("TMDB_API_KEY is not configured; skipping TMDb fallback")
        return None

    title, year = _split_title_year(query, file)
    if not title:
        return None

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for media_type in ("movie", "tv"):
            params = {"api_key": TMDB_API_KEY, "query": title, "include_adult": "false"}
            params["year" if media_type == "movie" else "first_air_date_year"] = year if year else ""
            params = {key: value for key, value in params.items() if value != ""}
            endpoint = f"{TMDB_BASE}/search/{media_type}"
            async with session.get(endpoint, params=params) as response:
                if response.status != 200:
                    logger.warning("TMDb %s search returned HTTP %s", media_type, response.status)
                    continue
                payload = await response.json()
            results = payload.get("results") or []
            if not results:
                continue

            # Prefer the closest year match when a year is present; otherwise use TMDb's relevance order.
            if year:
                date_key = "release_date" if media_type == "movie" else "first_air_date"
                exact_year = [item for item in results if str(item.get(date_key, "")).startswith(year)]
                if exact_year:
                    results = exact_year
            item = next((entry for entry in results if entry.get("poster_path")), results[0])
            date_key = "release_date" if media_type == "movie" else "first_air_date"
            date_value = item.get(date_key) or ""
            poster_path = item.get("poster_path")
            genre_ids = item.get("genre_ids") or []
            genre_names = {
                "movie": {28:"Action",12:"Adventure",16:"Animation",35:"Comedy",80:"Crime",99:"Documentary",18:"Drama",10751:"Family",14:"Fantasy",36:"History",27:"Horror",10402:"Music",9648:"Mystery",10749:"Romance",878:"Science Fiction",10770:"TV Movie",53:"Thriller",10752:"War",37:"Western"},
                "tv": {10759:"Action & Adventure",16:"Animation",35:"Comedy",80:"Crime",99:"Documentary",18:"Drama",10751:"Family",10762:"Kids",9648:"Mystery",10763:"News",10764:"Reality",10765:"Sci-Fi & Fantasy",10766:"Soap",10767:"Talk",10768:"War & Politics",37:"Western"},
            }[media_type]
            title_value = item.get("title") or item.get("name") or title
            return {
                "title": title_value,
                "year": int(date_value[:4]) if len(date_value) >= 4 and date_value[:4].isdigit() else None,
                "genres": list_to_str([genre_names.get(gid, "") for gid in genre_ids if genre_names.get(gid)]),
                "plot": item.get("overview") or "",
                "poster_url": f"{TMDB_IMAGE}{poster_path}" if poster_path else None,
                "imdb_id": None,
                "rating": str(item.get("vote_average") or ""),
                "languages": item.get("original_language") or "",
                "url": f"https://www.themoviedb.org/{media_type}/{item.get('id')}",
                "kind": "movie" if media_type == "movie" else "tv series",
                "votes": item.get("vote_count"),
                "runtime": "",
                "release_date": date_value or "N/A",
            }
    return None


async def get_movie_details(query, id=False, file=None):
    """Find metadata with IMDb first, then TMDb when IMDb has no usable poster."""
    if not id:
        try:
            details = await asyncio.to_thread(_get_imdb_details_sync, query, False, file)
            if details and details.get("poster_url"):
                logger.info("IMDb metadata found a poster for %r", query)
                return details
            if details:
                logger.info("IMDb matched %r but returned no poster; trying TMDb", query)
            else:
                logger.info("IMDb found no match for %r; trying TMDb", query)
        except Exception:
            logger.exception("IMDb lookup failed for %r; trying TMDb", query)
        try:
            details = await _search_tmdb(query, file)
            if details:
                logger.info("TMDb metadata found for %r (poster_found=%s)", query, bool(details.get("poster_url")))
                return details
        except Exception:
            logger.exception("TMDb lookup failed for %r", query)
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
