from __future__ import annotations

import html
import os
import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel


router = APIRouter(tags=["Picard integration"])

PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    "http://192.168.50.195:8787",
).rstrip("/")

try:
    PREFILL_TTL_SECONDS = max(
        60,
        int(os.getenv("PREFILL_TTL_SECONDS", "1800")),
    )
except ValueError:
    PREFILL_TTL_SECONDS = 1800

MAX_TITLES = 500
MAX_FIELD_LENGTH = 1000
MAX_TOTAL_TITLE_LENGTH = 100_000

_prefill_sessions: dict[str, dict[str, Any]] = {}


class PicardPrefillRequest(BaseModel):
    artist: str = ""
    album: str = ""
    titles: list[str]
    source: str = "unknown"


def _clean_expired_sessions() -> None:
    cutoff = time.time() - PREFILL_TTL_SECONDS
    expired = [
        token
        for token, session in _prefill_sessions.items()
        if session["created_at"] < cutoff
    ]
    for token in expired:
        _prefill_sessions.pop(token, None)


def _clean_text(value: Any, field_name: str) -> str:
    cleaned = str(value or "").strip()
    if len(cleaned) > MAX_FIELD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} is too long.",
        )
    return cleaned


def _render_prefilled_form(
    *,
    artist: str,
    album: str,
    titles: list[str],
    source: str,
) -> HTMLResponse:
    artist_html = html.escape(artist, quote=True)
    album_html = html.escape(album, quote=True)
    titles_html = html.escape("\n".join(titles), quote=False)
    source_html = html.escape(source or "unknown", quote=True)

    content = f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="robots" content="noindex,nofollow">
    <title>Arabic Sort — Picard prefill</title>
    <style>
        :root {{
            color-scheme: dark;
            font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
        }}
        body {{
            max-width: 1000px;
            margin: 30px auto;
            padding: 0 20px 50px;
            background: #111;
            color: #eee;
        }}
        h1 {{ margin-bottom: 0.4rem; }}
        .description {{ color: #bbb; line-height: 1.5; }}
        .notice {{
            margin: 18px 0;
            padding: 12px 14px;
            border: 1px solid #586;
            border-radius: 8px;
            background: #17271e;
        }}
        label {{
            display: block;
            margin-top: 18px;
            margin-bottom: 6px;
            font-weight: 650;
        }}
        input, textarea {{
            box-sizing: border-box;
            width: 100%;
            border: 1px solid #555;
            border-radius: 8px;
            background: #1c1c1c;
            color: #fff;
            padding: 11px;
            font-size: 16px;
        }}
        textarea {{
            min-height: 320px;
            resize: vertical;
        }}
        button {{
            display: inline-block;
            margin-top: 18px;
            padding: 11px 18px;
            border: 0;
            border-radius: 8px;
            background: #eee;
            color: #111;
            font-size: 15px;
            font-weight: 700;
            cursor: pointer;
        }}
        code {{
            background: #252525;
            padding: 2px 5px;
            border-radius: 4px;
        }}
    </style>
</head>
<body>
    <h1>Arabic Sort Bridge</h1>
    <p class="description">
        Picard supplied the artist, album and ordered track titles.
        Review them before generating Arabic titles.
    </p>

    <div class="notice">
        Source: <code>{source_html}</code> ·
        Tracks: <code>{len(titles)}</code> ·
        This temporary page expires automatically.
    </div>

    <form method="post" action="/generate">
        <label for="artist">Artist</label>
        <input id="artist" name="artist" value="{artist_html}"
               placeholder="Example: Amr Diab">

        <label for="album">Album</label>
        <input id="album" name="album" value="{album_html}"
               placeholder="Example: Tamally Maak">

        <label for="titles">Track titles — one per line</label>
        <textarea id="titles" name="titles" required>{titles_html}</textarea>

        <button type="submit">Generate Arabic titles</button>
    </form>
</body>
</html>"""

    return HTMLResponse(
        content=content,
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
        },
    )


@router.post("/picard/prefill")
async def create_picard_prefill(payload: PicardPrefillRequest) -> dict[str, Any]:
    _clean_expired_sessions()

    artist = _clean_text(payload.artist, "artist")
    album = _clean_text(payload.album, "album")
    source = _clean_text(payload.source, "source") or "unknown"

    if source not in {"cluster", "album", "file", "unknown"}:
        raise HTTPException(status_code=400, detail="Unsupported Picard source.")

    if not payload.titles:
        raise HTTPException(status_code=400, detail="At least one title is required.")

    if len(payload.titles) > MAX_TITLES:
        raise HTTPException(
            status_code=400,
            detail=f"Too many titles; maximum is {MAX_TITLES}.",
        )

    titles: list[str] = []
    for position, raw_title in enumerate(payload.titles, start=1):
        title = _clean_text(raw_title, f"title {position}")
        if not title:
            raise HTTPException(
                status_code=400,
                detail=f"Title {position} is empty.",
            )
        titles.append(title)

    if sum(len(title) for title in titles) > MAX_TOTAL_TITLE_LENGTH:
        raise HTTPException(status_code=400, detail="Combined title data is too large.")

    token = secrets.token_urlsafe(24)
    _prefill_sessions[token] = {
        "created_at": time.time(),
        "artist": artist,
        "album": album,
        "titles": titles,
        "source": source,
    }

    path = f"/picard/{token}"
    return {
        "ok": True,
        "url": f"{PUBLIC_BASE_URL}{path}",
        "path": path,
        "expires_in": PREFILL_TTL_SECONDS,
    }


@router.get("/picard/{token}", response_class=HTMLResponse)
async def open_picard_prefill(token: str) -> HTMLResponse:
    _clean_expired_sessions()
    session = _prefill_sessions.get(token)

    if session is None:
        raise HTTPException(
            status_code=404,
            detail="This Picard prefill link is invalid or has expired.",
        )

    return _render_prefilled_form(
        artist=session["artist"],
        album=session["album"],
        titles=session["titles"],
        source=session["source"],
    )
