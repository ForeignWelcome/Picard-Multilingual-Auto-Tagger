from __future__ import annotations

import html
import os
import secrets
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


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
MAX_TITLE_LENGTH = 1000

_prefill_sessions: dict[str, dict[str, Any]] = {}


class PicardTrackInput(BaseModel):
    row_id: str
    title: str = ""


class PicardPrefillRequest(BaseModel):
    artist: str = ""
    album: str = ""
    titles: list[str]
    source: str = "unknown"
    tracks: list[PicardTrackInput] = Field(default_factory=list)


class SendTitleRequest(BaseModel):
    row_index: int
    title: str


class CommandAckRequest(BaseModel):
    status: str
    effective_title: str = ""
    error: str = ""


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


def _clean_title(value: Any) -> str:
    title = str(value or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title cannot be empty.")
    if len(title) > MAX_TITLE_LENGTH:
        raise HTTPException(status_code=400, detail="Title is too long.")
    if "\x00" in title:
        raise HTTPException(status_code=400, detail="Title contains an invalid character.")
    return title


def _get_session(session_id: str) -> dict[str, Any]:
    _clean_expired_sessions()
    session = _prefill_sessions.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail="This Picard session is invalid or has expired.",
        )
    return session


def _require_control_token(
    session: dict[str, Any],
    authorization: str | None,
) -> None:
    expected = session["control_token"]
    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid Picard control token.")


def _render_prefilled_form(
    *,
    session_id: str,
    artist: str,
    album: str,
    titles: list[str],
    source: str,
) -> HTMLResponse:
    artist_html = html.escape(artist, quote=True)
    album_html = html.escape(album, quote=True)
    titles_html = html.escape("\n".join(titles), quote=False)
    source_html = html.escape(source or "unknown", quote=True)
    session_html = html.escape(session_id, quote=True)

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
        <input type="hidden" name="picard_session" value="{session_html}">

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

    if source not in {"cluster", "album", "unknown"}:
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

    if payload.tracks and len(payload.tracks) != len(titles):
        raise HTTPException(
            status_code=400,
            detail="Track identity count does not match title count.",
        )

    rows: list[dict[str, str]] = []
    if payload.tracks:
        seen: set[str] = set()
        for position, track in enumerate(payload.tracks):
            row_id = _clean_text(track.row_id, f"row_id {position + 1}")
            if not row_id or row_id in seen:
                raise HTTPException(status_code=400, detail="Invalid or duplicate row ID.")
            seen.add(row_id)
            rows.append({"row_id": row_id, "title": titles[position]})
    else:
        rows = [
            {"row_id": secrets.token_urlsafe(16), "title": title}
            for title in titles
        ]

    session_id = secrets.token_urlsafe(24)
    control_token = secrets.token_urlsafe(32)
    _prefill_sessions[session_id] = {
        "created_at": time.time(),
        "artist": artist,
        "album": album,
        "titles": titles,
        "source": source,
        "rows": rows,
        "control_token": control_token,
        "commands": {},
        "next_command_id": 1,
        "last_picard_poll": 0.0,
    }

    path = f"/picard/{session_id}"
    return {
        "ok": True,
        "session_id": session_id,
        "picard_token": control_token,
        "url": f"{PUBLIC_BASE_URL}{path}",
        "path": path,
        "expires_in": PREFILL_TTL_SECONDS,
        "expires_at": int(time.time()) + PREFILL_TTL_SECONDS,
    }


@router.get("/picard/{session_id}", response_class=HTMLResponse)
async def open_picard_prefill(session_id: str) -> HTMLResponse:
    session = _get_session(session_id)
    return _render_prefilled_form(
        session_id=session_id,
        artist=session["artist"],
        album=session["album"],
        titles=session["titles"],
        source=session["source"],
    )


@router.post("/picard/{session_id}/send")
async def queue_title_for_picard(
    session_id: str,
    payload: SendTitleRequest,
) -> dict[str, Any]:
    session = _get_session(session_id)
    title = _clean_title(payload.title)

    if payload.row_index < 0 or payload.row_index >= len(session["rows"]):
        raise HTTPException(status_code=400, detail="Invalid track row.")

    command_id = session["next_command_id"]
    session["next_command_id"] += 1
    row = session["rows"][payload.row_index]

    command = {
        "command_id": command_id,
        "row_id": row["row_id"],
        "title": title,
        "status": "queued",
        "created_at": time.time(),
        "effective_title": "",
        "error": "",
    }
    session["commands"][command_id] = command

    connected = (time.time() - session["last_picard_poll"]) < 10
    return {
        "ok": True,
        "command_id": command_id,
        "status": "queued",
        "picard_connected": connected,
    }


@router.get("/picard/{session_id}/commands/{command_id}")
async def browser_command_status(
    session_id: str,
    command_id: int,
) -> dict[str, Any]:
    session = _get_session(session_id)
    command = session["commands"].get(command_id)
    if command is None:
        raise HTTPException(status_code=404, detail="Command not found.")
    return {
        "ok": True,
        "command_id": command_id,
        "status": command["status"],
        "effective_title": command["effective_title"],
        "error": command["error"],
    }


@router.get("/picard/control/{session_id}/commands")
async def poll_picard_commands(
    session_id: str,
    after: int = 0,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _get_session(session_id)
    _require_control_token(session, authorization)
    session["last_picard_poll"] = time.time()

    pending: list[dict[str, Any]] = []
    for command_id in sorted(session["commands"]):
        command = session["commands"][command_id]
        if command_id <= after:
            continue
        if command["status"] not in {"queued", "delivered"}:
            continue
        command["status"] = "delivered"
        pending.append(
            {
                "command_id": command_id,
                "row_id": command["row_id"],
                "title": command["title"],
            }
        )

    return {"ok": True, "commands": pending}


@router.post("/picard/control/{session_id}/commands/{command_id}/ack")
async def acknowledge_picard_command(
    session_id: str,
    command_id: int,
    payload: CommandAckRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    session = _get_session(session_id)
    _require_control_token(session, authorization)

    command = session["commands"].get(command_id)
    if command is None:
        raise HTTPException(status_code=404, detail="Command not found.")

    if payload.status not in {"applied", "failed"}:
        raise HTTPException(status_code=400, detail="Invalid command status.")

    command["status"] = payload.status
    command["effective_title"] = str(payload.effective_title or "").strip()
    command["error"] = str(payload.error or "").strip()[:1000]
    command["completed_at"] = time.time()
    return {"ok": True}
