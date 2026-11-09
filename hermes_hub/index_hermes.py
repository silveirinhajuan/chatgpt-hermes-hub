from __future__ import annotations

import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from .config import hermes_home, state_db_path
from .db import (
    connect,
    delete_source,
    delete_source_prefix,
    get_meta,
    init_db,
    set_meta,
    upsert_document,
    utc_now,
)

MAX_BODY_CHARS = 200_000
MESSAGE_BATCH = 500
COMMIT_EVERY = 1_000


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _session_titles(state_db: Path) -> dict[str, dict[str, Any]]:
    """Read session metadata once. Missing/older schemas degrade gracefully."""
    con = connect(state_db, readonly=True)
    try:
        rows = con.execute(
            "SELECT id, source, title, display_name, started_at, last_activity_at "
            "FROM sessions"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()

    sessions: dict[str, dict[str, Any]] = {}
    for row in rows:
        title = row["title"] or ""
        display = row["display_name"] or ""
        source = row["source"] or "hermes"
        if not title:
            title = f"{source} · {display}" if display else f"Hermes · {row['id']}"
        sessions[row["id"]] = {
            "title": title,
            "source": source,
            "display_name": display,
            "started_at": row["started_at"],
            "last_activity_at": row["last_activity_at"],
        }
    return sessions


def iter_messages(
    state_db: Path,
    *,
    since_ts: float = 0.0,
    start_id: int = 0,
    limit: int | None = None,
    batch_size: int = MESSAGE_BATCH,
) -> Iterator[sqlite3.Row]:
    """Yield user/assistant messages in primary-key order without loading the DB.

    start_id lets incremental runs skip the historical prefix instead of
    scanning from rowid 1 on every pass.
    """
    con = connect(state_db, readonly=True)
    con.row_factory = sqlite3.Row
    last_id = max(0, int(start_id))
    yielded = 0
    try:
        while True:
            rows = con.execute(
                """
                SELECT id, session_id, role, content, timestamp, display_kind, tool_name
                FROM messages
                WHERE id > ?
                  AND timestamp >= ?
                  AND role IN ('user', 'assistant')
                  AND content IS NOT NULL
                  AND length(content) > 0
                  AND active = 1
                ORDER BY id ASC
                LIMIT ?
                """,
                (last_id, since_ts, batch_size),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                yield row
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            last_id = int(rows[-1]["id"])
            if len(rows) < batch_size:
                break
    finally:
        con.close()


def index_messages(
    conn: sqlite3.Connection,
    *,
    state_db: Path | None = None,
    since_ts: float = 0.0,
    start_id: int = 0,
    limit: int | None = None,
    reset: bool = False,
    progress: bool = True,
) -> dict[str, Any]:
    state_db = state_db or state_db_path()
    if not state_db.exists():
        raise FileNotFoundError(f"state.db não encontrado: {state_db}")

    if reset:
        removed = delete_source(conn, "hermes_message")
        conn.commit()
        if progress:
            _log(f"  hermes_message: {removed} documentos antigos removidos")

    sessions = _session_titles(state_db)
    inserted = 0
    max_ts = 0.0
    max_id = int(start_id)
    started = time.time()

    for row in iter_messages(state_db, since_ts=since_ts, start_id=start_id, limit=limit):
        body = row["content"] or ""
        truncated = len(body) > MAX_BODY_CHARS
        if truncated:
            body = body[:MAX_BODY_CHARS]

        session = sessions.get(row["session_id"], {})
        title = session.get("title") or f"Hermes · {row['session_id']}"
        ts = float(row["timestamp"] or 0.0)
        max_ts = max(max_ts, ts)
        max_id = max(max_id, int(row["id"]))

        upsert_document(
            conn,
            source="hermes_message",
            source_id=str(row["id"]),
            title=title,
            body=body,
            created_at=ts,
            updated_at=utc_now(),
            metadata={
                "session_id": row["session_id"],
                "role": row["role"],
                "platform": session.get("source"),
                "session_title": title,
                "message_id": int(row["id"]),
                "truncated": truncated,
            },
        )
        inserted += 1
        if inserted % COMMIT_EVERY == 0:
            conn.commit()
            if progress:
                _log(f"  hermes_message: {inserted} indexadas...")

    conn.commit()
    if max_ts:
        previous = float(get_meta(conn, "hermes_last_ts", "0") or 0)
        set_meta(conn, "hermes_last_ts", str(max(max_ts, previous)))
    if max_id:
        previous_id = int(get_meta(conn, "hermes_last_message_id", "0") or 0)
        set_meta(conn, "hermes_last_message_id", str(max(max_id, previous_id)))
    conn.commit()

    elapsed = round(time.time() - started, 1)
    if progress:
        _log(f"  hermes_message: {inserted} indexadas em {elapsed}s")
    return {"messages": inserted, "elapsed_seconds": elapsed, "max_timestamp": max_ts}


def _split_memory(text: str) -> list[str]:
    parts = re.split(r"(?m)^\s*§\s*$", text)
    if len(parts) == 1:
        parts = re.split(r"\s*§\s*", text)
    return [p.strip() for p in parts if len(p.strip()) >= 20]


def index_memories(
    conn: sqlite3.Connection,
    *,
    home: Path | None = None,
    progress: bool = True,
) -> dict[str, Any]:
    home = home or hermes_home()
    candidates: list[Path] = []
    memories_dir = home / "memories"
    if memories_dir.exists():
        candidates.extend(sorted(memories_dir.glob("*.md")))
    soul = home / "SOUL.md"
    if soul.exists():
        candidates.append(soul)

    count = 0
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            if progress:
                _log(f"  memória ignorada ({path}): {exc}")
            continue

        prefix = f"memory:{path.name}:"
        delete_source_prefix(conn, "hermes_memory", prefix)
        chunks = _split_memory(text)
        mtime = path.stat().st_mtime
        for idx, chunk in enumerate(chunks, start=1):
            upsert_document(
                conn,
                source="hermes_memory",
                source_id=f"{prefix}{idx:04d}",
                title=f"{path.name} · {idx}",
                body=chunk,
                created_at=mtime,
                updated_at=utc_now(),
                metadata={"file": str(path), "chunk": idx},
            )
            count += 1
        conn.commit()
        if progress:
            _log(f"  hermes_memory: {path.name} → {len(chunks)} chunks")

    return {"memory_chunks": count, "files": len(candidates)}


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw = text[3:end].strip()
    meta: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line or line.startswith((" ", "\t")):
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, text[end + 4 :].lstrip("\n")


def index_skills(
    conn: sqlite3.Connection,
    *,
    home: Path | None = None,
    progress: bool = True,
) -> dict[str, Any]:
    home = home or hermes_home()
    skills_dir = home / "skills"
    if not skills_dir.exists():
        return {"skills": 0}

    delete_source(conn, "hermes_skill")
    count = 0
    for path in sorted(skills_dir.rglob("SKILL.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta, _body = _parse_frontmatter(text)
        rel = path.relative_to(skills_dir)
        title = meta.get("name") or path.parent.name
        upsert_document(
            conn,
            source="hermes_skill",
            source_id=str(rel),
            title=title,
            body=text[:MAX_BODY_CHARS],
            created_at=path.stat().st_mtime,
            updated_at=utc_now(),
            metadata={
                "path": str(path),
                "description": meta.get("description", ""),
                "truncated": len(text) > MAX_BODY_CHARS,
            },
        )
        count += 1
        if count % 100 == 0:
            conn.commit()
    conn.commit()
    if progress:
        _log(f"  hermes_skill: {count} skills indexadas")
    return {"skills": count}


def index_all_hermes(
    conn: sqlite3.Connection,
    *,
    since_ts: float = 0.0,
    start_id: int = 0,
    limit: int | None = None,
    reset: bool = False,
    include_memories: bool = True,
    include_skills: bool = True,
    progress: bool = True,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    result.update(
        index_messages(
            conn,
            since_ts=since_ts,
            start_id=start_id,
            limit=limit,
            reset=reset,
            progress=progress,
        )
    )
    if include_memories:
        result.update(index_memories(conn, progress=progress))
    if include_skills:
        result.update(index_skills(conn, progress=progress))
    return result


def default_since_ts(conn: sqlite3.Connection, days: int = 365) -> float:
    """Use the last indexed timestamp minus a 2-day overlap, or N days back."""
    last = float(get_meta(conn, "hermes_last_ts", "0") or 0)
    if last > 0:
        return max(0.0, last - 2 * 86400)
    return max(0.0, time.time() - days * 86400)
