import html
import logging

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from info import ADMINS
from database.movie_catalogue import catalogue_collection
from database.Imdbposter import get_movie_details

logger = logging.getLogger(__name__)


@Client.on_message(filters.command("posterreview") & filters.user(ADMINS))
async def poster_review_queue(client, message):
    """Show newly indexed files whose poster match needs an admin decision."""
    cursor = catalogue_collection.find({"metadata_status": "needs_review"}).limit(5)
    pending = await cursor.to_list(length=5)
    if not pending:
        return await message.reply_text("No movies are waiting for poster review.")

    await message.reply_text(
        f"Found {len(pending)} movie(s) waiting for poster selection. "
        "Choose the correct IMDb result below each poster."
    )

    for record in pending:
        token = record.get("poster_review_id")
        candidates = record.get("poster_candidates") or []
        file_name = html.escape(record.get("file_name") or "")
        search_title = html.escape(record.get("poster_search_title") or record.get("title") or "Unknown")
        await message.reply_text(
            f"File: {file_name}\nSearch title: {search_title}\n"
            f"Candidates: {len(candidates)}"
        )
        for index, candidate in enumerate(candidates[:5]):
            title = candidate.get("title") or "Unknown title"
            year = candidate.get("year") or "Year unknown"
            kind = candidate.get("kind") or "unknown type"
            label = f"{title} ({year}) - {kind}"
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    text=f"Use {title[:35]} ({year})",
                    callback_data=f"posterpick:{token}:{index}",
                )
            ]])
            caption = f"{html.escape(label)}\nSelect this result if the poster and year are correct."
            try:
                if candidate.get("poster_url"):
                    await message.reply_photo(
                        photo=candidate["poster_url"],
                        caption=caption,
                        reply_markup=markup,
                    )
                else:
                    await message.reply_text(caption, reply_markup=markup)
            except Exception:
                logger.exception("Could not display poster candidate for review %s", token)
                await message.reply_text(caption, reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^posterpick:"))
async def choose_poster_candidate(client, query):
    if not query.from_user or query.from_user.id not in ADMINS:
        return await query.answer("Only admins can select posters.", show_alert=True)

    try:
        _, token, index_text = query.data.split(":")
        index = int(index_text)
    except (ValueError, AttributeError):
        return await query.answer("Invalid poster selection.", show_alert=True)

    record = await catalogue_collection.find_one({
        "poster_review_id": token,
        "metadata_status": "needs_review",
    })
    if not record:
        return await query.answer("This review request is no longer active.", show_alert=True)

    candidates = record.get("poster_candidates") or []
    if index < 0 or index >= len(candidates):
        return await query.answer("That candidate is no longer available.", show_alert=True)

    candidate = candidates[index]
    provider = candidate.get("provider") or "unknown"
    if candidate.get("imdb_id"):
        details = await get_movie_details(candidate["imdb_id"], id=True)
        if not details:
            details = candidate
    else:
        # Non-IMDb candidates already contain the selected provider metadata.
        details = candidate

    source_url = details.get("url") or candidate.get("url") or ""
    poster_url = details.get("poster_url") or candidate.get("poster_url")
    values = {
        "title": details.get("title") or candidate.get("title") or record.get("title"),
        "year": details.get("year") or candidate.get("year"),
        "genres": details.get("genres") or "",
        "plot": details.get("plot") or "",
        "poster_url": poster_url,
        "imdb_id": details.get("imdb_id") or imdb_id,
        "rating": details.get("rating") or "",
        "languages": details.get("languages") or "",
        "imdb_url": source_url,
        "metadata_source": provider + "_admin_selected",
        "metadata_status": "ready" if poster_url else "no_poster",
        "poster_review_id": None,
        "poster_candidates": [],
    }
    result = await catalogue_collection.update_one(
        {"file_id": record.get("file_id"), "poster_review_id": token},
        {"$set": values},
    )
    if not getattr(result, "modified_count", 0):
        return await query.answer("The record changed; please reopen /posterreview.", show_alert=True)

    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.reply_text(
        f"Poster selected successfully.\n"
        f"Movie: {values['title']} ({values['year'] or 'Year unknown'})\n"
        f"Website metadata status: {values['metadata_status']}"
    )
    await query.answer("Poster saved.")
