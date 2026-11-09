from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from .db import delete_source, init_db, upsert_document, utc_now

MAX_BODY_CHARS = 500_000
COMMIT_EVERY = 100


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


@contextmanager
def _open_conversations_json(path: str | Path) -> Iterator[BinaryIO]:
    source = Path(path).expanduser()
    if source.is_dir():
        source = source / "conversations.json"
    if not source.exists():
        raise FileNotFoundError(f"export não encontrado: {source}")

    if source.suffix.lower() == ".zip":
        zf = zipfile.ZipFile(source)
        names = [n for n in zf.namelist() if n.endswith("conversations.json")]
        if not names:
            zf.close()
            raise FileNotFoundError("conversations.json não está dentro do ZIP")
        fh = zf.open(names[0])
        try:
            yield fh
        finally:
            fh.close()
            zf.close()
    else:
        with source.open("rb") as fh:
            yield fh


def iter_conversations(path: str | Path) -> Iterator[dict[str, Any]]:
    """Stream a ChatGPT export, preferring ijson for large archives."""
    with _open_conversations_json(path) as fh:
        first = fh.read(1)
        fh.seek(0)
        prefix = "item" if first == b"[" else "conversations.item" if first == b"{" else "item"

        try:
            import ijson  # type: ignore
        except ImportError:
            ijson = None  # type: ignore

        if ijson is not None:
            count = 0
            try:
                for item in ijson.items(fh, prefix):
                    count += 1
                    if isinstance(item, dict):
                        yield item
                return
            except Exception as exc:
                if count:
                    raise RuntimeError(
                        f"falha no meio do streaming após {count} conversas: {exc}"
                    ) from exc
                fh.seek(0)

        data = json.load(fh)
        if isinstance(data, dict):
            data = data.get("conversations", [])
        if not isinstance(data, list):
            raise ValueError("formato inesperado: esperava uma lista de conversas")
        for item in data:
            if isinstance(item, dict):
                yield item


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content") or {}
    parts = content.get("parts")
    texts: list[str] = []
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if isinstance(text, str):
                    texts.append(text)
    if not texts:
        text = content.get("text")
        if isinstance(text, str):
            texts.append(text)
    return "\n".join(t.strip() for t in texts if t and t.strip())


def flatten_conversation(conv: dict[str, Any]) -> dict[str, Any] | None:
    """Turn a conversation tree into one searchable document.

    The active branch (current_node walking up through parent links) is used
    when available; otherwise all text messages are sorted by create_time.
    """
    mapping = conv.get("mapping") or {}
    if not isinstance(mapping, dict):
        return None

    chain: list[dict[str, Any]] = []
    current = conv.get("current_node")
    seen: set[str] = set()
    while current and current in mapping and current not in seen:
        seen.add(current)
        node = mapping[current]
        if isinstance(node, dict):
            chain.append(node)
            current = node.get("parent")
        else:
            break
    chain.reverse()

    if not chain:
        chain = sorted(
            (n for n in mapping.values() if isinstance(n, dict)),
            key=lambda n: (n.get("message") or {}).get("create_time") or 0,
        )

    messages: list[tuple[str, str, float]] = []
    for node in chain:
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        role = ((message.get("author") or {}).get("role") or "").lower()
        if role not in ("user", "assistant"):
            continue
        text = _message_text(message)
        if not text:
            continue
        ts = message.get("create_time") or 0.0
        messages.append((role, text, float(ts)))

    if not messages:
        return None

    title = (conv.get("title") or "").strip()
    if not title:
        first_user = next((t for role, t, _ in messages if role == "user"), "")
        title = first_user.splitlines()[0][:100] if first_user else "Conversa sem título"

    body_lines: list[str] = []
    for role, text, _ts in messages:
        label = "Você" if role == "user" else "ChatGPT"
        body_lines.append(f"## {label}\n{text}")
    body = "\n\n".join(body_lines)

    created_at = conv.get("create_time")
    if not created_at:
        created_at = min((ts for _, _, ts in messages if ts), default=0.0)
    updated_at = conv.get("update_time") or max((ts for _, _, ts in messages), default=0.0)
    conv_id = conv.get("id")
    if not conv_id:
        seed = f"{title}|{created_at}|{messages[0][1][:200]}"
        conv_id = "sha1:" + hashlib.sha1(seed.encode("utf-8")).hexdigest()

    truncated = len(body) > MAX_BODY_CHARS
    if truncated:
        body = body[:MAX_BODY_CHARS]

    return {
        "source_id": str(conv_id),
        "title": title,
        "body": body,
        "created_at": float(created_at or 0.0),
        "updated_at": float(updated_at or 0.0),
        "metadata": {
            "conversation_id": str(conv_id),
            "message_count": len(messages),
            "update_time": float(updated_at or 0.0),
            "source": "chatgpt",
            "truncated": truncated,
        },
    }


def index_chatgpt(
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    since_ts: float = 0.0,
    reset: bool = False,
    limit: int | None = None,
    progress: bool = True,
) -> dict[str, Any]:
    if reset:
        removed = delete_source(conn, "chatgpt_conversation")
        conn.commit()
        if progress:
            _log(f"  chatgpt_conversation: {removed} documentos antigos removidos")

    started = time.time()
    inserted = 0
    skipped = 0
    seen = 0
    for conv in iter_conversations(path):
        seen += 1
        doc = flatten_conversation(conv)
        if doc is None:
            skipped += 1
            continue
        if since_ts and doc["updated_at"] and doc["updated_at"] < since_ts:
            skipped += 1
            continue
        upsert_document(
            conn,
            source="chatgpt_conversation",
            source_id=doc["source_id"],
            title=doc["title"],
            body=doc["body"],
            created_at=doc["created_at"] or None,
            updated_at=utc_now(),
            metadata=doc["metadata"],
        )
        inserted += 1
        if inserted % COMMIT_EVERY == 0:
            conn.commit()
            if progress:
                _log(f"  chatgpt_conversation: {inserted} indexadas...")
        if limit is not None and inserted >= limit:
            break

    conn.commit()
    elapsed = round(time.time() - started, 1)
    if progress:
        _log(
            f"  chatgpt_conversation: {inserted} indexadas, "
            f"{skipped} ignoradas de {seen} lidas em {elapsed}s"
        )
    return {
        "conversations": inserted,
        "skipped": skipped,
        "read": seen,
        "elapsed_seconds": elapsed,
    }
