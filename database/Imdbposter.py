import asyncio
import logging
import re
from difflib import SequenceMatcher
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


def _infer_media_type(query, file=None):
    source = f"{query or ''} {file or ''}"
    if re.search(r"(?i)\b(?:S\d{1,2}(?:E\d{1,3})?|Season\s*\d+|Episode\s*\d+)\b", source):
        return "tv"
    return "movie"


def _normalise_title(value):
    value = str(value or "").lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _title_match_score(query, candidate):
    left = _normalise_title(query)
    right = _normalise_title(candidate)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    left_words, right_words = set(left.split()), set(right.split())
    overlap = len(left_words & right_words) / max(1, len(left_words))
    return max(SequenceMatcher(None, left, right).ratio(), overlap)


def _details_from_movie(movie, movie_id):
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


def _get_imdb_by_id_sync(query):
    movie_id = str(query).removeprefix("tt")
    movie = ia.get_movie(movie_id)
    return _details_from_movie(movie, movie_id)


def _search_imdb_sync(query, file=None):
    title, year = _split_title_year(query, file)
    if not title:
        return None

    expected_type = _infer_media_type(query, file)
    results = ia.search_movie(title, results=30)
    if not results:
        return None

    allowed_kinds = (
        {"tv series", "tv mini series"}
        if expected_type == "tv"
        else {"movie", "tv movie", "video movie"}
    )
    ranked = []
    for item in results:
        candidate_title = item.get("title") or ""
        score = _title_match_score(title, candidate_title)
        kind = str(item.get("kind") or "").lower()
        candidate_year = str(item.get("year") or "")
        if kind not in allowed_kinds:
            continue
        if score < (0.78 if len(title.split()) > 1 else 0.88):
            continue
        # A supplied year is a hard filter, not just a small ranking bonus.
        if year and candidate_year != year:
            continue
        ranked.append((score, item))

    if not ranked:
        logger.info("IMDb found no reliable %s match for %r (%s)", expected_type, title, year or "year unknown")
        return None

    ranked.sort(
        key=lambda pair: (
            pair[0] == 1.0,
            pair[0],
            str(pair[1].get("year") or ""),
        ),
        reverse=True,
    )

    # Fetch details for a small shortlist so the admin can compare real posters.
    candidates = []
    for _, item in ranked[:6]:
        try:
            details = _details_from_movie(ia.get_movie(item.movieID), item.movieID)
            if details.get("title"):
                candidates.append(details)
        except Exception:
            logger.exception("Could not load IMDb candidate %s", getattr(item, "movieID", "?"))

    if not candidates:
        return None

    exact = [
        candidate for candidate in candidates
        if _normalise_title(candidate.get("title")) == _normalise_title(title)
    ]
    if year:
        exact_year = [candidate for candidate in exact if str(candidate.get("year") or "") == year]
        if len(exact_year) == 1:
            return exact_year[0]
        if len(exact_year) > 1:
            return {"needs_review": True, "candidates": exact_year, "search_title": title}
        # Never silently choose a different release year.
        return None

    if len(exact) == 1:
        return exact[0]

    # Same-name titles or fuzzy-only results require an admin to choose.
    review_candidates = exact if len(exact) > 1 else candidates
    return {
        "needs_review": True,
        "candidates": review_candidates[:5],
        "search_title": title,
    }


async def _search_tvmaze(query, file=None):
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

    shows = [entry.get("show", {}) for entry in results if entry.get("show")]
    ranked = []
    for show in shows:
        score = _title_match_score(title, show.get("name") or "")
        premiered_year = str(show.get("premiered") or "")[:4]
        if score < 0.90 or _normalise_title(show.get("name")) != _normalise_title(title):
            continue
        if year and premiered_year != year:
            continue
        ranked.append(show)

    # TVmaze is only used automatically when the title/year identifies one show.
    if len(ranked) != 1:
        if ranked:
            logger.info("TVmaze has multiple possible exact matches for %r; not auto-selecting", title)
        return None

    show = ranked[0]
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
    """Use strict IMDb title/year/type matching, with a no-key TVmaze fallback."""
    if id:
        try:
            return await asyncio.to_thread(_get_imdb_by_id_sync, query)
        except Exception:
            logger.exception("IMDb ID lookup failed for %r", query)
            return None

    try:
        details = await asyncio.to_thread(_search_imdb_sync, query, file)
        if details:
            if details.get("needs_review"):
                logger.info("IMDb match for %r needs admin selection", query)
            elif details.get("poster_url"):
                logger.info("IMDb metadata found a verified poster for %r", query)
                return details
            else:
                logger.info("IMDb matched %r without a poster; trying TVmaze if applicable", query)
        else:
            logger.info("IMDb found no strict match for %r; trying TVmaze if applicable", query)
        if details and details.get("needs_review"):
            return details
    except Exception:
        logger.exception("IMDb lookup failed for %r; trying TVmaze if applicable", query)
        details = None

    try:
        tvmaze_details = await _search_tvmaze(query, file)
        if tvmaze_details:
            return tvmaze_details
    except Exception:
        logger.exception("TVmaze lookup failed for %r", query)
    return details


async def _search_anilist(query, year=None):
    """Search AniList's public GraphQL API without an API key."""
    graphql = """
    query ($search: String) {
      Page(perPage: 10) {
        media(search: $search, type: ANIME) {
          id
          title { romaji english native }
          startDate { year }
          coverImage { extraLarge large }
          description(asHtml: false)
          genres
          averageScore
          siteUrl
        }
      }
    }
    """
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post("https://graphql.anilist.co", json={"query": graphql, "variables": {"search": query}},) as response:
            if response.status != 200:
                return []
            payload = await response.json()
    found = []
    media = ((payload.get("data") or {}).get("Page") or {}).get("media") or []
    for item in media:
        names = item.get("title") or {}
        item_year = (item.get("startDate") or {}).get("year")
        if year and item_year and str(item_year) != str(year):
            continue
        cover = item.get("coverImage") or {}
        found.append({
            "title": names.get("english") or names.get("romaji") or names.get("native") or query,
            "year": item_year,
            "genres": list_to_str(item.get("genres") or []),
            "plot": re.sub(r"\s+", " ", item.get("description") or "")[:800],
            "poster_url": cover.get("extraLarge") or cover.get("large"),
            "rating": str(item.get("averageScore") or ""),
            "languages": "",
            "url": item.get("siteUrl") or "",
            "kind": "anime",
            "provider": "anilist",
            "provider_id": str(item.get("id")),
            "imdb_id": None,
        })
    return found


async def _search_jikan(query, year=None):
    """Search MyAnimeList anime through the public Jikan API."""
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get("https://api.jikan.moe/v4/anime", params={"q": query, "limit": 10, "sfw": "true"}) as response:
            if response.status != 200:
                return []
            payload = await response.json()
    found = []
    for item in payload.get("data") or []:
        item_year = item.get("year")
        if not item_year:
            item_year = (((item.get("aired") or {}).get("prop") or {}).get("from") or {}).get("year")
        if year and item_year and str(item_year) != str(year):
            continue
        jpg = ((item.get("images") or {}).get("jpg") or {})
        found.append({
            "title": item.get("title_english") or item.get("title") or query,
            "year": item_year,
            "genres": list_to_str([g.get("name") for g in item.get("genres") or []]),
            "plot": item.get("synopsis") or "",
            "poster_url": jpg.get("large_image_url") or jpg.get("image_url"),
            "rating": str(item.get("score") or ""),
            "languages": "",
            "url": item.get("url") or "",
            "kind": "anime",
            "provider": "jikan",
            "provider_id": str(item.get("mal_id")),
            "imdb_id": None,
        })
    return found


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
