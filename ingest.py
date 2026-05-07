"""
services/reddit_capture/ingest.py
──────────────────────────────────
Flask service that fetches your Reddit saved posts and ingests them.

Endpoint:  POST /fetch-saved
Body:      {} (empty — all credentials come from env)
Returns:
    {
        "ok": true,
        "results": [
            {"inbox_id": "<uuid>", "url": "...", "was_new": true},
            ...
        ]
    }
    {"ok": false, "error": "..."}   — credentials missing or Reddit unreachable

What it does for each saved submission:
  1. Self-post  → url = Reddit permalink, raw_content = post body text.
  2. Link post  → url = linked URL, raw_content = article text extracted by
                  trafilatura (non-fatal if extraction fails — filter scores on
                  title alone in that case).
  3. Saved comments are skipped — only Submission objects are processed.
  4. Duplicate URL check (idempotent): already-seen URLs return was_new=false.
  5. Inserts a row into the inbox table (platform='reddit', status='pending').

Using the linked URL (not the Reddit permalink) as the canonical URL for link
posts means that if YouTube capture or another workflow already captured the
same article/video, the duplicate check fires and we skip it cleanly.

Environment variables:
    REDDIT_CLIENT_ID      — from https://www.reddit.com/prefs/apps  (script app)
    REDDIT_CLIENT_SECRET  — from the same registration
    REDDIT_USERNAME       — your Reddit username (without u/)
    REDDIT_PASSWORD       — your Reddit password
    REDDIT_USER_AGENT     — e.g. "ai-vault/1.0 (by /u/yourname)"
    REDDIT_SAVED_LIMIT    — max saved items to fetch per run (default 100)
    REDDIT_INGEST_PORT    — port this service listens on (default 5003)

One-time setup:
  1. Go to https://www.reddit.com/prefs/apps → "create another app"
  2. Choose "script", set redirect URI to http://localhost:8080
  3. Copy the client ID (under the app name) and secret into .env
  4. Run: python -m services.reddit_capture.ingest
  5. Verify with: curl -X POST http://localhost:5003/fetch-saved

Start:
    python -m services.reddit_capture.ingest
"""

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

import httpx
import praw
import praw.models
import trafilatura
from flask import Flask, jsonify

app = Flask(__name__)

_SAVED_LIMIT = int(os.environ.get("REDDIT_SAVED_LIMIT", "100"))


# ── Path helpers ───────────────────────────────────────────────────────────────

def _db_path() -> Path:
    repo_root = Path(__file__).parent.parent.parent
    return Path(os.environ.get("DB_PATH", str(repo_root / "db" / "vault.db")))


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


# ── Reddit client ──────────────────────────────────────────────────────────────

def _get_reddit() -> praw.Reddit:
    """Build a PRAW Reddit instance from env vars. Raises KeyError if vars missing."""
    return praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        username=os.environ["REDDIT_USERNAME"],
        password=os.environ["REDDIT_PASSWORD"],
        user_agent=os.environ.get("REDDIT_USER_AGENT", "ai-vault/1.0"),
    )


# ── Content extraction ─────────────────────────────────────────────────────────

def _fetch_article(url: str) -> str | None:
    """
    Download a URL and extract its main readable text via trafilatura.

    Returns None (not an error) if the page can't be fetched or yields no text.
    The ingest still proceeds — the filter will score on the post title alone.
    """
    try:
        response = httpx.get(
            url,
            follow_redirects=True,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ai-vault/1.0)"},
        )
        response.raise_for_status()
        return trafilatura.extract(
            response.text,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
        )
    except Exception:
        return None


# ── Per-post ingestion ─────────────────────────────────────────────────────────

def _ingest_post(conn: sqlite3.Connection, post: praw.models.Submission) -> dict:
    """
    Resolve content for a single submission and insert into inbox if new.

    Returns {"inbox_id": str, "url": str, "was_new": bool}.
    """
    if post.is_self:
        url = f"https://www.reddit.com{post.permalink}"
        body = post.selftext
        raw_content = body if body and body != "[deleted]" else None
    else:
        url = post.url
        raw_content = _fetch_article(url)
        if raw_content is None:
            print(f"[ingest] No article text for {url} — will filter on title only.")

    # Idempotency: skip if this URL is already in the inbox.
    existing = conn.execute("SELECT id FROM inbox WHERE url = ?", (url,)).fetchone()
    if existing:
        return {"inbox_id": existing["id"], "url": url, "was_new": False}

    inbox_id = str(uuid.uuid4())
    source_date = datetime.utcfromtimestamp(post.created_utc).date().isoformat()
    author = f"u/{post.author.name}" if post.author else None

    conn.execute(
        """
        INSERT INTO inbox
            (id, url, platform, title, raw_content, author, source_date,
             captured_at, status)
        VALUES (?, ?, 'reddit', ?, ?, ?, ?, ?, 'pending')
        """,
        (
            inbox_id,
            url,
            post.title,
            raw_content,
            author,
            source_date,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    print(f"[ingest] Inserted inbox row {inbox_id} for {url}")
    return {"inbox_id": inbox_id, "url": url, "was_new": True}


# ── Endpoint ───────────────────────────────────────────────────────────────────

@app.post("/fetch-saved")
def fetch_saved():
    try:
        reddit = _get_reddit()
    except KeyError as exc:
        return jsonify({"ok": False, "error": f"Missing env var: {exc}"}), 500

    conn = _get_db()
    results = []
    try:
        me = reddit.user.me()
        for item in me.saved(limit=_SAVED_LIMIT):
            if not isinstance(item, praw.models.Submission):
                continue  # skip saved comments
            results.append(_ingest_post(conn, item))
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        conn.close()

    return jsonify({"ok": True, "results": results})


@app.get("/health")
def health():
    return jsonify({"ok": True})


# ── CLI entry point ────────────────────────────────────────────────────────────

def main() -> None:
    port = int(os.environ.get("REDDIT_INGEST_PORT", "5003"))
    print(f"[reddit_capture.ingest] Starting on port {port}")
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
