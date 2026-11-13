from aiohttp import web
import re
import math
import logging
import secrets
import time
import mimetypes
from urllib.parse import quote_plus
from aiohttp.http_exceptions import BadStatusLine
from lazybot import multi_clients, work_loads, LazyPrincessBot
from server.exceptions import FIleNotFound, InvalidHash
from zzint import StartTime, __version__
from util.custom_dl import ByteStreamer
from util.time_format import get_readable_time
from util.render_template import render_page
from util.file_properties import get_hash, get_name
from info import *
from database.ia_filterdb import Media
from database.movie_catalogue import catalogue_collection
from utils import temp
from util.human_readable import humanbytes
import jinja2


routes = web.RouteTableDef()

def _catalogue_card(group_key, entries):
    first = entries[0]
    poster = next((x.get("poster_url") for x in entries if x.get("poster_url")), None)
    return {"group_key": group_key, "title": first.get("group_title") or first.get("title") or first.get("file_name") or "Untitled",
            "year": first.get("catalogue_year") or first.get("year"), "poster_url": poster,
            "kind": first.get("catalogue_type") or "movie", "count": len(entries), "url": f"/title/{group_key}"}


@routes.get("/", allow_head=True)
@routes.get("/movies", allow_head=True)
async def movie_catalogue(request: web.Request):
    """Show one catalogue card per movie or series grouping."""
    query = (request.query.get("q") or "").strip()[:100]
    try:
        cursor = Media.find({})
        cursor.sort("$natural", -1)
        cursor.limit(3000)
        records = await cursor.to_list(length=3000)
        ids = [item.file_id for item in records]
        metas = await catalogue_collection.find({"file_id": {"$in": ids}}).to_list(length=len(ids)) if ids else []
        meta_by_id = {m.get("file_id"): m for m in metas}
        groups = {}
        for item in records:
            meta = meta_by_id.get(item.file_id, {})
            info = {**meta, "file_id": item.file_id, "file_name": item.file_name, "file_size": item.file_size}
            key = meta.get("catalogue_group_key") or ("file-" + item.file_id)
            groups.setdefault(key, []).append(info)
        cards = [_catalogue_card(key, values) for key, values in groups.items()]
        if query:
            cards = [c for c in cards if query.casefold() in c["title"].casefold()]
        cards.sort(key=lambda c: c["title"].casefold())
        total = len(cards)
        with open("template/home.html", encoding="utf-8") as f:
            template = jinja2.Environment(autoescape=jinja2.select_autoescape(["html", "xml"])).from_string(f.read())
        return web.Response(text=template.render(site_name="KAMINA", query=query, total=total, movies=cards[:120]), content_type="text/html")
    except Exception:
        logging.exception("Unable to render grouped catalogue")
        raise web.HTTPInternalServerError(text="The movie catalogue is temporarily unavailable.")


@routes.get("/title/{group_key}", allow_head=True)
async def catalogue_title_page(request: web.Request):
    group_key = request.match_info["group_key"]
    try:
        metadata = await catalogue_collection.find({"catalogue_group_key": group_key}).to_list(length=500)
        if not metadata:
            raise web.HTTPNotFound(text="This title is not available.")
        ids = [m.get("file_id") for m in metadata if m.get("file_id")]
        records = await Media.find({"file_id": {"$in": ids}}).to_list(length=len(ids))
        record_by_id = {x.file_id: x for x in records}
        entries = []
        for m in metadata:
            record = record_by_id.get(m.get("file_id"))
            if record:
                entries.append({**m, "file_name": record.file_name, "file_size_text": humanbytes(record.file_size),
                                "download_url": f"/download/{quote_plus(record.file_id)}"})
        if not entries:
            raise web.HTTPNotFound(text="No downloadable files are available for this title.")
        card = _catalogue_card(group_key, entries)
        seasons = {}
        if card["kind"] == "series":
            for item in entries:
                seasons.setdefault(item.get("season_number") or 1, []).append(item)
            for season in seasons:
                seasons[season].sort(key=lambda x: x.get("episode_number") or 0)
        entries.sort(key=lambda x: (x.get("quality_label") or "ZZZ", x.get("file_name") or ""))
        with open("template/title.html", encoding="utf-8") as f:
            template = jinja2.Environment(autoescape=jinja2.select_autoescape(["html", "xml"])).from_string(f.read())
        return web.Response(text=template.render(title=card["title"], year=card["year"], poster_url=card["poster_url"],
                            is_series=card["kind"] == "series", seasons=sorted(seasons.items()), files=entries),
                            content_type="text/html")
    except web.HTTPException:
        raise
    except Exception:
        logging.exception("Unable to render title page %s", group_key)
        raise web.HTTPInternalServerError(text="Unable to open this title right now.")


@routes.get(r"/download/{file_id}")
async def browser_download(request: web.Request):
    """Create a Telegram-backed download URL and redirect Chrome to the file."""
    file_id = request.match_info["file_id"]
    try:
        record = await Media.find_one({"file_id": file_id})
        if not record:
            raise web.HTTPNotFound(text="This file is no longer available.")

        index = min(work_loads, key=work_loads.get)
        client = multi_clients[index]
        log_message = await client.send_cached_media(
            chat_id=LOG_CHANNEL,
            file_id=record.file_id,
        )

        filename = get_name(log_message) or record.file_name or "download"
        file_hash = get_hash(log_message)
        if not file_hash:
            raise web.HTTPInternalServerError(text="Could not prepare this download.")

        download_url = (
            f"{URL}{log_message.id}/{quote_plus(filename)}?hash={file_hash}"
        )
        raise web.HTTPFound(location=download_url)
    except web.HTTPException:
        raise
    except Exception:
        logging.exception("Unable to prepare browser download")
        raise web.HTTPInternalServerError(
            text="Unable to prepare this download. Please try again."
        )


@routes.get(r"/watch/{path:\S+}", allow_head=True)
async def stream_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        match = re.search(r"^([a-zA-Z0-9_-]{6})(\d+)$", path)
        if match:
            secure_hash = match.group(1)
            id = int(match.group(2))
        else:
            id = int(re.search(r"(\d+)(?:/\S+)?", path).group(1))
            secure_hash = request.rel_url.query.get("hash")
        return web.Response(text=await render_page(id, secure_hash), content_type='text/html')
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message)
    except (AttributeError, BadStatusLine, ConnectionResetError):
        pass
    except Exception as e:
        logging.critical(e.with_traceback(None))
        raise web.HTTPInternalServerError(text=str(e))

@routes.get(r"/{path:\S+}", allow_head=True)
async def stream_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        match = re.search(r"^([a-zA-Z0-9_-]{6})(\d+)$", path)
        if match:
            secure_hash = match.group(1)
            id = int(match.group(2))
        else:
            id = int(re.search(r"(\d+)(?:/\S+)?", path).group(1))
            secure_hash = request.rel_url.query.get("hash")
        return await media_streamer(request, id, secure_hash)
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message)
    except (AttributeError, BadStatusLine, ConnectionResetError):
        pass
    except Exception as e:
        logging.critical(e.with_traceback(None))
        raise web.HTTPInternalServerError(text=str(e))

class_cache = {}

async def media_streamer(request: web.Request, id: int, secure_hash: str):
    range_header = request.headers.get("Range", 0)

    index = min(work_loads, key=work_loads.get)
    faster_client = multi_clients[index]

    if MULTI_CLIENT:
        logging.info(f"Client {index} is now serving {request.remote}")

    if faster_client in class_cache:
        tg_connect = class_cache[faster_client]
        logging.debug(f"Using cached ByteStreamer object for client {index}")
    else:
        logging.debug(f"Creating new ByteStreamer object for client {index}")
        tg_connect = ByteStreamer(faster_client)
        class_cache[faster_client] = tg_connect
    logging.debug("before calling get_file_properties")
    file_id = await tg_connect.get_file_properties(id)
    logging.debug("after calling get_file_properties")

    if file_id.unique_id[:6] != secure_hash:
        logging.debug(f"Invalid hash for message with ID {id}")
        raise InvalidHash

    file_size = file_id.file_size

    if range_header:
        from_bytes, until_bytes = range_header.replace("bytes=", "").split("-")
        from_bytes = int(from_bytes)
        until_bytes = int(until_bytes) if until_bytes else file_size - 1
    else:
        from_bytes = request.http_range.start or 0
        until_bytes = (request.http_range.stop or file_size) - 1

    if (until_bytes > file_size) or (from_bytes < 0) or (until_bytes < from_bytes):
        return web.Response(
            status=416,
            body="416: Range not satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    chunk_size = 1024 * 1024
    until_bytes = min(until_bytes, file_size - 1)

    offset = from_bytes - (from_bytes % chunk_size)
    first_part_cut = from_bytes - offset
    last_part_cut = until_bytes % chunk_size + 1

    req_length = until_bytes - from_bytes + 1
    part_count = math.ceil(until_bytes / chunk_size) - math.floor(offset / chunk_size)
    body = tg_connect.yield_file(
        file_id, index, offset, first_part_cut, last_part_cut, part_count, chunk_size
    )

    mime_type = file_id.mime_type
    file_name = file_id.file_name
    disposition = "attachment"

    if mime_type:
        if not file_name:
            try:
                file_name = f"{secrets.token_hex(2)}.{mime_type.split('/')[1]}"
            except (IndexError, AttributeError):
                file_name = f"{secrets.token_hex(2)}.unknown"
    else:
        if file_name:
            mime_type = mimetypes.guess_type(file_id.file_name)[0] or "application/octet-stream"
        else:
            mime_type = "application/octet-stream"
            file_name = f"{secrets.token_hex(2)}.unknown"

    return web.Response(
        status=206 if range_header else 200,
        body=body,
        headers={
            "Content-Type": mime_type,
            "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file_size}",
            "Content-Length": str(req_length),
            "Content-Disposition": f'attachment; filename="{file_name}"',
            "Accept-Ranges": "bytes",
        },
    )
