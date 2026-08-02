import html
import json
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Arabic Sort Bridge")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b-instruct")
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "10m")
DATABASE_PATH = os.getenv("DATABASE_PATH", "/data/arabic_sort.db")

ARABIZI_RULES = {
    "2": ["ء", "أ", "ق"],
    "3": ["ع"],
    "3'": ["غ"],
    "5": ["خ"],
    "6": ["ط"],
    "6'": ["ظ"],
    "7": ["ح"],
    "7'": ["خ"],
    "8": ["ق"],
    "9": ["ص"],
    "9'": ["ض"],
    "'": ["ع", "ء", "ق", "غ", "ض"],
}

app_state: dict[str, Any] = {"db_ready": False}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_latin(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    value = value.replace("’", "'").replace("`", "'").replace("´", "'")
    value = re.sub(r"\s+", " ", value)
    return value


def normalize_scope(value: str) -> str:
    return normalize_latin(value) if value else ""


def latin_tokens(value: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9']+", normalize_latin(value))


def arabic_tokens(value: str) -> list[str]:
    return re.findall(r"[\u0600-\u06FF]+", value.strip())


def db_connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database() -> None:
    with db_connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS title_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                normalized_latin TEXT NOT NULL,
                original_latin TEXT NOT NULL,
                arabic TEXT NOT NULL,
                artist TEXT NOT NULL DEFAULT '',
                album TEXT NOT NULL DEFAULT '',
                dialect TEXT NOT NULL DEFAULT '',
                verified_at TEXT NOT NULL,
                UNIQUE(normalized_latin, artist, album, dialect)
            );

            CREATE TABLE IF NOT EXISTS word_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                latin_word TEXT NOT NULL,
                arabic_word TEXT NOT NULL,
                artist TEXT NOT NULL DEFAULT '',
                dialect TEXT NOT NULL DEFAULT '',
                confidence INTEGER NOT NULL DEFAULT 1,
                verified_at TEXT NOT NULL,
                UNIQUE(latin_word, arabic_word, artist, dialect)
            );

            CREATE INDEX IF NOT EXISTS idx_title_lookup
            ON title_mappings(normalized_latin, artist, album, dialect);

            CREATE INDEX IF NOT EXISTS idx_word_lookup
            ON word_mappings(latin_word, artist, dialect);
            """
        )
    app_state["db_ready"] = True


initialize_database()


def lookup_title(
    original: str,
    artist: str = "",
    album: str = "",
    dialect: str = "",
) -> sqlite3.Row | None:
    """Return the newest verified correction for this exact Latin title.

    Verified title corrections are global: artist, album and dialect do not
    restrict reuse. This also allows older scoped rows to be reused.
    """
    normalized = normalize_latin(original)

    with db_connect() as db:
        return db.execute(
            """
            SELECT arabic, artist, album, dialect
            FROM title_mappings
            WHERE normalized_latin = ?
            ORDER BY
                CASE
                    WHEN artist = '' AND album = '' AND dialect = '' THEN 0
                    ELSE 1
                END,
                verified_at DESC
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()


def get_relevant_word_mappings(
    titles: list[str],
    artist: str,
    dialect: str,
) -> list[dict[str, Any]]:
    words = sorted(set(token for title in titles for token in latin_tokens(title)))
    if not words:
        return []

    placeholders = ",".join("?" for _ in words)
    artist_n = normalize_scope(artist)
    dialect_n = normalize_scope(dialect)

    with db_connect() as db:
        rows = db.execute(
            f"""
            SELECT latin_word, arabic_word, artist, dialect, confidence
            FROM word_mappings
            WHERE latin_word IN ({placeholders})
              AND artist IN (?, '')
              AND dialect IN (?, '')
            ORDER BY
                CASE WHEN artist = ? THEN 0 ELSE 1 END,
                CASE WHEN dialect = ? THEN 0 ELSE 1 END,
                confidence DESC
            """,
            (*words, artist_n, dialect_n, artist_n, dialect_n),
        ).fetchall()

    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        key = (row["latin_word"], row["arabic_word"])
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(row))
    return result[:100]


MANDATORY_ARABIZI_MAP = {
    "3\'": "غ",
    "6\'": "ظ",
    "7\'": "خ",
    "9\'": "ض",
    "2": "ء",
    "3": "ع",
    "5": "خ",
    "6": "ط",
    "7": "ح",
    "8": "ق",
    "9": "ص",
}


def arabizi_rules_used(value: str) -> list[tuple[str, str]]:
    remaining = value
    found: list[tuple[str, str]] = []
    for latin, arabic in MANDATORY_ARABIZI_MAP.items():
        if latin in remaining:
            found.append((latin, arabic))
            remaining = remaining.replace(latin, "")
    return found


def expand_arabizi_hint(value: str) -> str:
    result = value
    for latin, arabic in MANDATORY_ARABIZI_MAP.items():
        result = result.replace(latin, arabic)
    return result


def validate_arabizi_output(original: str, arabic_result: str) -> tuple[bool, list[str]]:
    problems: list[str] = []
    for latin, required_arabic in arabizi_rules_used(original):
        if required_arabic not in arabic_result:
            problems.append(f"{latin} must produce {required_arabic}")
    if re.search(r"[A-Za-z0-9]", arabic_result):
        problems.append("output still contains Latin letters or digits")
    return not problems, problems


def force_replace_remaining_digits(value: str) -> str:
    result = value
    for latin, arabic in MANDATORY_ARABIZI_MAP.items():
        result = result.replace(latin, arabic)
    return result


def assemble_arabic_words(words: list[str]) -> str:
    result = " ".join(word.strip() for word in words if word.strip())
    # Join the Arabic definite article to the following word.
    result = re.sub(r"(?:^|\s)ال\s+", lambda m: ("" if m.start() == 0 else " ") + "ال", result)
    return re.sub(r"\s+", " ", result).strip()


def clean_json_response(value: str) -> dict[str, Any]:
    value = value.strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value)
    return json.loads(value)


def page(content: str, title: str = "Arabic Sort Bridge") -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{html.escape(title)}</title>
    <style>
        :root {{
            color-scheme: dark;
            font-family: system-ui, sans-serif;
        }}
        body {{
            max-width: 1200px;
            margin: 30px auto;
            padding: 0 20px 50px;
            background: #111;
            color: #eee;
        }}
        h1, h2 {{ margin-bottom: 0.4rem; }}
        .description {{ color: #bbb; line-height: 1.5; }}
        label {{
            display: block;
            margin-top: 18px;
            margin-bottom: 6px;
            font-weight: 650;
        }}
        input, textarea, select {{
            box-sizing: border-box;
            width: 100%;
            border: 1px solid #555;
            border-radius: 8px;
            background: #1c1c1c;
            color: #fff;
            padding: 11px;
            font-size: 16px;
        }}
        textarea {{ min-height: 260px; resize: vertical; }}
        button, .button {{
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
            text-decoration: none;
        }}
        button:disabled {{ opacity: 0.65; cursor: default; }}
        .secondary {{ background: #333; color: #fff; }}
        .verify {{ background: #2f5f43; color: #fff; }}
        .error {{
            padding: 14px;
            border: 1px solid #b54;
            border-radius: 8px;
            background: #341a17;
            white-space: pre-wrap;
        }}
        .notice {{
            padding: 14px;
            border: 1px solid #586;
            border-radius: 8px;
            background: #17271e;
        }}
        table {{
            width: 100%;
            margin-top: 20px;
            border-collapse: collapse;
        }}
        th, td {{
            border-bottom: 1px solid #444;
            padding: 10px 8px;
            vertical-align: top;
            text-align: left;
        }}
        th {{ color: #bbb; }}
        td input {{ margin: 0; }}
        .arabic {{
            direction: rtl;
            text-align: right;
            font-size: 19px;
        }}
        .arabic-cell {{
            display: grid;
            grid-template-columns: minmax(240px, 1fr) auto auto;
            align-items: center;
            gap: 8px;
        }}
        .arabic-cell input {{ min-width: 0; }}
        .copy-single, .verify-single {{
            width: auto;
            margin: 0;
            padding: 10px 14px;
            white-space: nowrap;
        }}
        .copy-single {{ background: #333; color: #fff; }}
        .verify-single {{ background: #2f5f43; color: #fff; }}
        .source {{
            display: inline-block;
            margin-top: 4px;
            padding: 2px 7px;
            border-radius: 999px;
            background: #2a2a2a;
            color: #ccc;
            font-size: 12px;
        }}
        .actions {{
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
        }}
        code {{
            background: #252525;
            padding: 2px 5px;
            border-radius: 4px;
        }}
        @media (max-width: 800px) {{
            .arabic-cell {{
                grid-template-columns: 1fr 1fr;
            }}
            .arabic-cell input {{
                grid-column: 1 / -1;
            }}
            table, thead, tbody, th, td, tr {{
                display: block;
            }}
            thead {{ display: none; }}
            tr {{ padding: 10px 0; border-bottom: 1px solid #444; }}
            td {{ border: 0; padding: 5px 0; }}
        }}
    </style>
</head>
<body>
{content}
</body>
</html>"""
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return page(
        """
<h1>Arabic Sort Bridge</h1>
<p class="description">
Paste romanized Arabic track titles. Verified corrections are saved locally
and reused before the language model is asked.
</p>
<form method="post" action="/generate">
    <label for="artist">Artist</label>
    <input id="artist" name="artist" placeholder="Example: Amr Diab">

    <label for="album">Album</label>
    <input id="album" name="album" placeholder="Example: Tamally Maak">

    <label for="titles">Track titles — one per line</label>
    <textarea id="titles" name="titles" required
        placeholder="Tamally Maak&#10;Enta Habibi&#10;Kol Youm Men Omry"></textarea>

    <button type="submit">Generate Arabic titles</button>
</form>
"""
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "ollama_url": OLLAMA_URL,
        "model": OLLAMA_MODEL,
        "database": DATABASE_PATH,
        "database_ready": app_state["db_ready"],
    }


@app.post("/verify")
async def verify(
    original: str = Form(...),
    arabic: str = Form(...),
    artist: str = Form(default=""),
    album: str = Form(default=""),
    dialect: str = Form(default=""),
) -> JSONResponse:
    original = original.strip()
    arabic = arabic.strip()
    if not original or not arabic:
        return JSONResponse(
            {"ok": False, "message": "Original and Arabic values are required."},
            status_code=400,
        )

    normalized = normalize_latin(original)
    # Verified mappings are global and reusable across all artists/albums.
    artist_n = ""
    album_n = ""
    dialect_n = ""
    now = utc_now()

    latin_words = latin_tokens(original)
    arabic_words = arabic_tokens(arabic)
    stored_words = 0

    with db_connect() as db:
        db.execute(
            """
            INSERT INTO title_mappings (
                normalized_latin, original_latin, arabic,
                artist, album, dialect, verified_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(normalized_latin, artist, album, dialect)
            DO UPDATE SET
                original_latin = excluded.original_latin,
                arabic = excluded.arabic,
                verified_at = excluded.verified_at
            """,
            (normalized, original, arabic, artist_n, album_n, dialect_n, now),
        )

        # Only store word pairs when token counts align exactly.
        if latin_words and len(latin_words) == len(arabic_words):
            for latin_word, arabic_word in zip(latin_words, arabic_words):
                db.execute(
                    """
                    INSERT INTO word_mappings (
                        latin_word, arabic_word, artist,
                        dialect, confidence, verified_at
                    )
                    VALUES (?, ?, ?, ?, 1, ?)
                    ON CONFLICT(latin_word, arabic_word, artist, dialect)
                    DO UPDATE SET
                        confidence = confidence + 1,
                        verified_at = excluded.verified_at
                    """,
                    (
                        normalize_latin(latin_word),
                        arabic_word,
                        artist_n,
                        dialect_n,
                        now,
                    ),
                )
                stored_words += 1

    message = "Stored exact title"
    if stored_words:
        message += f" and {stored_words} word mapping(s)"
    else:
        message += "; word mappings skipped because token counts did not align"

    return JSONResponse({"ok": True, "message": message})


@app.post("/generate", response_class=HTMLResponse)
async def generate(
    artist: str = Form(default=""),
    album: str = Form(default=""),
    profile: str = Form(default=""),
    titles: str = Form(...),
) -> HTMLResponse:
    original_titles = [line.strip() for line in titles.splitlines() if line.strip()]
    if not original_titles:
        return page(
            '<div class="error">Enter at least one track title.</div>'
            '<a class="button secondary" href="/">Go back</a>',
            "No titles",
        )

    results: dict[int, dict[str, Any]] = {}
    unresolved: list[tuple[int, str]] = []

    for index, original in enumerate(original_titles, start=1):
        stored = lookup_title(original, artist, album, profile)
        if stored:
            results[index] = {
                "arabic": stored["arabic"],
                "needs_review": False,
                "note": "",
                "source": "verified database",
            }
        else:
            unresolved.append((index, original))

    if unresolved:
        # Exact titles are handled above. For unresolved titles, verified word
        # mappings are locked in place and Qwen is asked only for unknown words.
        async with httpx.AsyncClient(timeout=240) as client:
            for index, original in unresolved:
                tokens = latin_tokens(original)
                known_rows = get_relevant_word_mappings([original], artist, "")

                # Keep the first row per Latin word. The SQL query already orders
                # artist/dialect specificity and confidence from best to worst.
                locked_words: dict[str, str] = {}
                for item in known_rows:
                    locked_words.setdefault(item["latin_word"], item["arabic_word"])

                output_words: list[str] = []
                llm_used = False
                notes: list[str] = []

                for token in tokens:
                    token_n = normalize_latin(token)
                    if token_n in locked_words:
                        output_words.append(locked_words[token_n])
                        continue

                    llm_used = True
                    mandatory_rules = arabizi_rules_used(token_n)
                    expanded_token = expand_arabizi_hint(token_n)
                    mandatory_rule_text = (
                        "\n".join(
                            f"- {latin!r} MUST produce {arabic!r}."
                            for latin, arabic in mandatory_rules
                        )
                        if mandatory_rules
                        else "- none"
                    )

                    system_prompt = f"""
You reconstruct ONE Arabic word from romanized Arabic.

Only convert the single token supplied. Do not output words from elsewhere.
This is back-transliteration, not semantic translation.

Full title context: {original}
Artist: {artist or "Unknown"}
Album: {album or "Unknown"}

Mandatory Arabizi mappings for this token:
{mandatory_rule_text}

Rule-expanded hint:
{expanded_token}

Requirements:
- Return Arabic script only in the arabic field.
- Do not leave Latin letters or digits in the result.
- Preserve dialect and likely original spelling.
- A standalone apostrophe is contextual and may represent ع, ء, ق, غ or ض.

Examples:
7ob -> حب
3alek -> عليك
9a7 -> صح
8amoli -> قامولي
ma'aya -> معايا
a'obak -> أحبك

Return JSON only:
{{
  "arabic": "Arabic word",
  "needs_review": true,
  "note": ""
}}
""".strip()

                    request_body = {
                        "model": OLLAMA_MODEL,
                        "stream": False,
        "think": False,
                        "keep_alive": OLLAMA_KEEP_ALIVE,
                        "format": "json",
                        "options": {
                            "temperature": 0,
                            "num_ctx": 2048,
                            "num_predict": 100,
                            "repeat_penalty": 1.05,
                        },
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": f"Single token: {token_n}"},
                        ],
                    }

                    try:
                        response = await client.post(
                            f"{OLLAMA_URL}/api/chat",
                            json=request_body,
                        )
                        response.raise_for_status()
                        parsed = clean_json_response(
                            response.json()["message"]["content"]
                        )
                        arabic_word = str(parsed.get("arabic", "")).strip()
                        valid, problems = validate_arabizi_output(token_n, arabic_word)

                        if not valid:
                            retry_prompt = f"""
Correct the previous result for the ONE token {token_n!r}.
Previous result: {arabic_word!r}
Problems: {'; '.join(problems)}
Mandatory mappings:
{mandatory_rule_text}
Return JSON only with one Arabic word and no Latin letters or digits.
""".strip()
                            retry_body = dict(request_body)
                            retry_body["messages"] = [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": retry_prompt},
                            ]
                            retry_response = await client.post(
                                f"{OLLAMA_URL}/api/chat",
                                json=retry_body,
                            )
                            retry_response.raise_for_status()
                            retry_parsed = clean_json_response(
                                retry_response.json()["message"]["content"]
                            )
                            arabic_word = str(
                                retry_parsed.get("arabic", "")
                            ).strip()

                        arabic_word = force_replace_remaining_digits(arabic_word)
                        final_valid, final_problems = validate_arabizi_output(
                            token_n, arabic_word
                        )
                        if not final_valid:
                            notes.append(
                                f"{token}: " + "; ".join(final_problems)
                            )
                        output_words.append(arabic_word)
                    except Exception as exc:
                        output_words.append("")
                        notes.append(f"{token}: generation failed: {exc}")

                arabic_result = assemble_arabic_words(output_words)
                results[index] = {
                    "arabic": arabic_result,
                    "needs_review": llm_used or bool(notes),
                    "note": "; ".join(notes),
                    "source": (
                        "verified words only"
                        if not llm_used
                        else "locked verified words + LLM word fallback"
                    ),
                }

    rows: list[str] = []
    for index, original in enumerate(original_titles, start=1):
        item = results.get(
            index,
            {
                "arabic": "",
                "needs_review": True,
                "note": "No model result returned",
                "source": "missing",
            },
        )
        arabic = item["arabic"]
        review_text = (
            "Review recommended"
            if item["needs_review"]
            else "Likely straightforward"
        )
        note_html = (
            "<br>" + html.escape(item["note"])
            if item["note"]
            else ""
        )

        rows.append(
            f"""
<tr>
    <td>{index}</td>
    <td>{html.escape(original)}</td>
    <td>
        <div class="arabic-cell">
            <input
                class="arabic-result arabic"
                value="{html.escape(arabic, quote=True)}"
                data-original="{html.escape(original, quote=True)}"
            >
            <button type="button" class="copy-single"
                    onclick="copySingle(this)">Copy</button>
            <button type="button" class="verify-single"
                    onclick="verifyAndStore(this)">Verify &amp; Store</button>
        </div>
    </td>
    <td>
        {html.escape(review_text)}{note_html}
        <br><span class="source">{html.escape(item["source"])}</span>
        <div class="row-status"></div>
    </td>
</tr>
"""
        )

    escaped_artist = html.escape(artist, quote=True)
    escaped_album = html.escape(album, quote=True)
    escaped_profile = html.escape(profile, quote=True)

    return page(
        f"""
<h1>Arabic title candidates</h1>
<p class="description">
Correct any mistakes, then press <strong>Verify &amp; Store</strong>.
Exact title matches are reused before the model is called.
</p>

<div id="page-context"
     data-artist="{escaped_artist}"
     data-album="{escaped_album}"
     data-dialect="{escaped_profile}"></div>

<table>
    <thead>
        <tr>
            <th>#</th>
            <th>Original title</th>
            <th>Arabic title</th>
            <th>Status</th>
        </tr>
    </thead>
    <tbody>
        {''.join(rows)}
    </tbody>
</table>

<div class="actions">
    <button type="button" onclick="copyResults()">Copy all Arabic titles</button>
    <a class="button secondary" href="/">Start another album</a>
</div>

<p id="copy-status" class="notice" style="display:none"></p>

<script>
async function writeToClipboard(copyText) {{
    if (navigator.clipboard && window.isSecureContext) {{
        await navigator.clipboard.writeText(copyText);
        return;
    }}

    const textarea = document.createElement("textarea");
    textarea.value = copyText;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    textarea.style.top = "0";
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    textarea.setSelectionRange(0, textarea.value.length);

    const copied = document.execCommand("copy");
    document.body.removeChild(textarea);

    if (!copied) {{
        throw new Error("Browser rejected the copy command.");
    }}
}}

async function copySingle(button) {{
    const field = button.closest(".arabic-cell")
        .querySelector(".arabic-result");
    const originalLabel = button.textContent.trim();

    try {{
        await writeToClipboard(field.value);
        button.textContent = "Copied!";
        button.disabled = true;
        setTimeout(() => {{
            button.textContent = originalLabel;
            button.disabled = false;
        }}, 1200);
    }} catch (error) {{
        field.focus();
        field.select();
        button.textContent = "Select manually";
        setTimeout(() => {{
            button.textContent = originalLabel;
        }}, 1800);
        console.error(error);
    }}
}}

async function copyResults() {{
    const fields = document.querySelectorAll(".arabic-result");
    const copyText = Array.from(fields)
        .map(field => field.value)
        .join("\\n");
    const status = document.getElementById("copy-status");

    try {{
        await writeToClipboard(copyText);
        status.textContent = "Copied all Arabic titles.";
        status.style.display = "block";
    }} catch (error) {{
        status.textContent = "Copying was blocked. Select manually or use HTTPS.";
        status.style.display = "block";
        console.error(error);
    }}
}}

async function verifyAndStore(button) {{
    const row = button.closest("tr");
    const field = row.querySelector(".arabic-result");
    const status = row.querySelector(".row-status");
    const context = document.getElementById("page-context");

    const form = new URLSearchParams();
    form.set("original", field.dataset.original);
    form.set("arabic", field.value);
    form.set("artist", context.dataset.artist);
    form.set("album", context.dataset.album);
    form.set("dialect", context.dataset.dialect);

    button.disabled = true;
    button.textContent = "Storing...";

    try {{
        const response = await fetch("/verify", {{
            method: "POST",
            headers: {{
                "Content-Type": "application/x-www-form-urlencoded"
            }},
            body: form.toString()
        }});

        const data = await response.json();

        if (!response.ok || !data.ok) {{
            throw new Error(data.message || "Store failed");
        }}

        button.textContent = "Stored ✓";
        status.textContent = data.message;
        status.className = "row-status notice";
    }} catch (error) {{
        button.disabled = false;
        button.textContent = "Verify & Store";
        status.textContent = error.message;
        status.className = "row-status error";
    }}
}}
</script>
""",
        "Arabic title candidates",
    )

# --- Picard prefill integration ---
from app.picard_prefill import router as picard_prefill_router
app.include_router(picard_prefill_router)

