from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import ensure_parent, hub_db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    created_at REAL,
    updated_at REAL,
    content_hash TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source);
CREATE INDEX IF NOT EXISTS idx_documents_created ON documents(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_hash
    ON documents(source, content_hash)
    WHERE content_hash IS NOT NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    title,
    body,
    content='documents',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, title, body)
    VALUES (new.id, new.title, new.body);
END;

CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, body)
    VALUES ('delete', old.id, old.title, old.body);
END;

CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, body)
    VALUES ('delete', old.id, old.title, old.body);
    INSERT INTO documents_fts(rowid, title, body)
    VALUES (new.id, new.title, new.body);
END;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(path: str | Path | None = None, readonly: bool = False) -> sqlite3.Connection:
    """Open the hub database (or any SQLite database) with sane pragmas."""
    db_path = Path(path) if path is not None else hub_db_path()
    if readonly:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30)
    else:
        ensure_parent(db_path)
        conn = sqlite3.connect(db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def utc_now() -> float:
    return time.time()


def iso_utc(ts: float | int | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _metadata(row: sqlite3.Row) -> dict[str, Any]:
    raw = row["metadata"] if "metadata" in row.keys() else "{}"
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def upsert_document(
    conn: sqlite3.Connection,
    *,
    source: str,
    source_id: str,
    body: str,
    title: str = "",
    created_at: float | None = None,
    updated_at: float | None = None,
    content_hash: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Insert or update one document and return its id."""
    now = utc_now()
    conn.execute(
        """
        INSERT INTO documents
            (source, source_id, title, body, created_at, updated_at, content_hash, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, source_id) DO UPDATE SET
            title = excluded.title,
            body = excluded.body,
            created_at = excluded.created_at,
            updated_at = excluded.updated_at,
            content_hash = excluded.content_hash,
            metadata = excluded.metadata
        """,
        (
            source,
            source_id,
            title or "",
            body,
            created_at if created_at is not None else now,
            updated_at if updated_at is not None else now,
            content_hash,
            _json(metadata or {}),
        ),
    )
    row = conn.execute(
        "SELECT id FROM documents WHERE source = ? AND source_id = ?",
        (source, source_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("document was not persisted")
    return int(row["id"])


def delete_source(conn: sqlite3.Connection, source: str) -> int:
    cur = conn.execute("DELETE FROM documents WHERE source = ?", (source,))
    return cur.rowcount


def delete_source_prefix(conn: sqlite3.Connection, source: str, source_id_prefix: str) -> int:
    cur = conn.execute(
        "DELETE FROM documents WHERE source = ? AND source_id LIKE ?",
        (source, source_id_prefix + "%"),
    )
    return cur.rowcount


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 query.

    Every token is quoted and prefix-matched, joined with OR. bm25 ranks the
    results, so recall stays high without letting FTS5 syntax errors through.
    """
    tokens = [t for t in _TOKEN_RE.findall(query or "") if len(t) > 0]
    if not tokens:
        return ""
    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique = []
    for token in tokens:
        key = token.lower()
        if key not in seen:
            seen.add(key)
            unique.append(token)
    return " OR ".join(f'"{t}"*' for t in unique[:24])


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 10,
    source: str | None = None,
) -> list[dict[str, Any]]:
    match = fts_query(query)
    if not match:
        return []
    limit = max(1, min(int(limit), 50))
    sql = """
        SELECT d.id, d.source, d.source_id, d.title, d.created_at, d.metadata,
               snippet(documents_fts, 1, '«', '»', '…', 28) AS snippet,
               -bm25(documents_fts, 8.0, 1.0) AS rank
        FROM documents_fts
        JOIN documents d ON d.id = documents_fts.rowid
        WHERE documents_fts MATCH ?
    """
    params: list[Any] = [match]
    if source:
        sql += " AND d.source = ?"
        params.append(source)
    sql += " ORDER BY rank DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        results.append(
            {
                "id": int(row["id"]),
                "source": row["source"],
                "source_id": row["source_id"],
                "title": row["title"],
                "text": row["snippet"],
                "url": f"hub://{row['source']}/{row['source_id']}",
                "created_at": iso_utc(row["created_at"]),
                "snippet": row["snippet"],
                "rank": round(float(row["rank"] or 0.0), 4),
                "metadata": _metadata(row),
            }
        )
    return results


def fetch(
    conn: sqlite3.Connection,
    *,
    doc_id: int | None = None,
    source: str | None = None,
    source_id: str | None = None,
) -> dict[str, Any] | None:
    if doc_id:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (int(doc_id),)).fetchone()
    elif source and source_id:
        row = conn.execute(
            "SELECT * FROM documents WHERE source = ? AND source_id = ?",
            (source, source_id),
        ).fetchone()
    else:
        raise ValueError("informe doc_id ou source + source_id")
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "source": row["source"],
        "source_id": row["source_id"],
        "title": row["title"],
        "created_at": iso_utc(row["created_at"]),
        "updated_at": iso_utc(row["updated_at"]),
        "body": row["body"],
        "metadata": _metadata(row),
    }


def recent(
    conn: sqlite3.Connection,
    *,
    limit: int = 10,
    days: int | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 50))
    sql = """
        SELECT d.id, d.source, d.source_id, d.title, d.created_at, d.metadata,
               substr(d.body, 1, 400) AS preview
        FROM documents d
        WHERE 1 = 1
    """
    params: list[Any] = []
    if days is not None and days > 0:
        sql += " AND COALESCE(d.created_at, d.updated_at, 0) >= ?"
        params.append(utc_now() - int(days) * 86400)
    if source:
        sql += " AND d.source = ?"
        params.append(source)
    sql += " ORDER BY COALESCE(d.created_at, d.updated_at, 0) DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": int(row["id"]),
            "source": row["source"],
            "source_id": row["source_id"],
            "title": row["title"],
            "created_at": iso_utc(row["created_at"]),
            "preview": row["preview"],
            "metadata": _metadata(row),
        }
        for row in rows
    ]


def remember(
    conn: sqlite3.Connection,
    text: str,
    *,
    title: str = "",
    tags: Iterable[str] | None = None,
    source: str = "chatgpt_note",
) -> dict[str, Any]:
    body = (text or "").strip()
    if not body:
        raise ValueError("texto vazio")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    existing = conn.execute(
        "SELECT id FROM documents WHERE source = ? AND content_hash = ?",
        (source, digest),
    ).fetchone()
    if existing:
        return {"id": int(existing["id"]), "deduplicated": True}

    now = utc_now()
    if not title:
        first_line = body.splitlines()[0].strip()
        title = first_line[:100] + ("…" if len(first_line) > 100 else "")
    tag_list = [t.strip() for t in (tags or []) if str(t).strip()]
    source_id = f"note:{int(now)}:{digest[:12]}"
    doc_id = upsert_document(
        conn,
        source=source,
        source_id=source_id,
        title=title,
        body=body,
        created_at=now,
        updated_at=now,
        content_hash=digest,
        metadata={"tags": tag_list, "origin": source, "hash": digest},
    )
    conn.commit()
    return {"id": doc_id, "deduplicated": False}


def stats(conn: sqlite3.Connection, db_path: str | Path | None = None) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT source, COUNT(*) AS n,
               MIN(created_at) AS first_ts,
               MAX(created_at) AS last_ts
        FROM documents
        GROUP BY source
        ORDER BY n DESC
        """
    ).fetchall()
    total = sum(int(r["n"]) for r in rows)
    by_source = {
        r["source"]: {
            "documents": int(r["n"]),
            "first": iso_utc(r["first_ts"]),
            "last": iso_utc(r["last_ts"]),
        }
        for r in rows
    }
    path = Path(db_path) if db_path is not None else hub_db_path()
    try:
        size_bytes = os.path.getsize(path)
    except OSError:
        size_bytes = 0
    meta = {
        row["key"]: row["value"]
        for row in conn.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
    }
    return {
        "documents": total,
        "by_source": by_source,
        "db_path": str(path),
        "db_size_bytes": size_bytes,
        "db_size_mb": round(size_bytes / (1024 * 1024), 2),
        "meta": meta,
    }


def rebuild_fts(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO documents_fts(documents_fts) VALUES('rebuild')")
    conn.commit()
