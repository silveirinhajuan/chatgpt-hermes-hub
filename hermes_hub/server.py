from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__
from .config import hub_db_path
from .db import connect, fetch as db_fetch, init_db, recent as db_recent, remember as db_remember, search as db_search, stats as db_stats
from .ask import AskError, ask_hermes_answer, ask_job_status, start_ask_job

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("hermes-hub")
READONLY = os.environ.get("HERMES_HUB_READONLY", "0").strip().lower() in {"1", "true", "yes"}

server = MCPServer(
    "hermes-chatgpt-hub",
    version=__version__,
    instructions=(
        "Shared knowledge hub between Hermes Agent and ChatGPT. "
        "Use search/fetch to retrieve notes, Hermes memories, skills and indexed "
        "ChatGPT conversations. Use remember to persist a fact, decision or note "
        "that should be visible to both assistants. The hub stores and retrieves "
        "text only; it does not call any language model. ask_hermes is the only tool that calls a language model (Hermes, in no-tools mode)."
    ),
)


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _with_db(fn):
    conn = connect(hub_db_path())
    try:
        init_db(conn)
        return fn(conn)
    finally:
        conn.close()


@server.tool()
def search(query: str, limit: int = 10, source: str = "") -> str:
    """Search the shared hub. Use Portuguese or English keywords.

    Args:
        query: Free-text query.
        limit: Maximum number of results (1-50).
        source: Optional filter: hermes_message, hermes_memory, hermes_skill,
            chatgpt_conversation or chatgpt_note.
    """
    try:
        results = _with_db(lambda conn: db_search(conn, query, limit=limit, source=source or None))
        return _json({"query": query, "count": len(results), "results": results})
    except Exception as exc:
        log.exception("search failed")
        return _json({"error": str(exc)})


@server.tool()
def fetch(
    doc_id: int = 0,
    id: str = "",
    source: str = "",
    source_id: str = "",
    max_chars: int = 20000,
) -> str:
    """Fetch one document by id, or by source + source_id.

    Args:
        doc_id: Numeric document id returned by search/recent.
        id: Optional string id: a numeric id, a hub:// URL, or "source:source_id".
        source: Document source (used with source_id).
        source_id: Stable id inside the source (used with source).
        max_chars: Truncate the body to this many characters (default 20000).
    """
    try:
        max_chars = max(500, min(int(max_chars), 100_000))
        if id:
            raw = id.removeprefix("hub://")
            if raw.isdigit():
                doc_id = int(raw)
            elif "/" in raw:
                source, source_id = raw.split("/", 1)
            elif ":" in raw:
                source, source_id = raw.split(":", 1)

        def run(conn):
            doc = db_fetch(
                conn,
                doc_id=doc_id or None,
                source=source or None,
                source_id=source_id or None,
            )
            if doc is None:
                return None
            body = doc["body"]
            doc["url"] = f"hub://{doc['source']}/{doc['source_id']}"
            doc["text"] = body
            if len(body) > max_chars:
                doc["body"] = body[:max_chars]
                doc["text"] = doc["body"]
                doc["truncated"] = True
                doc["original_length"] = len(body)
            else:
                doc["truncated"] = False
            return doc

        doc = _with_db(run)
        if doc is None:
            return _json({"error": "documento não encontrado"})
        return _json(doc)
    except Exception as exc:
        log.exception("fetch failed")
        return _json({"error": str(exc)})


@server.tool()
def remember(text: str, title: str = "", tags: str = "") -> str:
    """Persist a fact, decision or note in the shared hub.

    Use this when the user asks you to remember something that Hermes should
    also see. Duplicate notes are deduplicated by content hash.

    Args:
        text: The note body. Markdown is fine.
        title: Optional short title.
        tags: Optional comma-separated tags.
    """
    try:
        if READONLY:
            return _json(
                {
                    "error": "hub em modo somente leitura",
                    "hint": "remova HERMES_HUB_READONLY=1 para permitir escritas",
                }
            )
        tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
        result = _with_db(lambda conn: db_remember(conn, text, title=title, tags=tag_list))
        log.info("remember source=chatgpt_note id=%s title=%r", result.get("id"), title)
        return _json(result)
    except Exception as exc:
        log.exception("remember failed")
        return _json({"error": str(exc)})


@server.tool()
def recent(limit: int = 10, days: int = 0, source: str = "") -> str:
    """List the most recent documents in the hub.

    Args:
        limit: Maximum number of results (1-50).
        days: Only include documents from the last N days (0 = no limit).
        source: Optional source filter.
    """
    try:
        results = _with_db(
            lambda conn: db_recent(
                conn,
                limit=limit,
                days=days or None,
                source=source or None,
            )
        )
        return _json({"count": len(results), "results": results})
    except Exception as exc:
        log.exception("recent failed")
        return _json({"error": str(exc)})


@server.tool()
def stats() -> str:
    """Show hub statistics: document counts by source, database size and watermarks."""
    try:
        return _json(_with_db(lambda conn: db_stats(conn, hub_db_path())))
    except Exception as exc:
        log.exception("stats failed")
        return _json({"error": str(exc)})



@server.tool()
def ask_hermes(question: str, max_chars: int = 8000) -> str:
    """Start a Hermes Q&A (asynchronous) and return a job_id immediately.

    Hermes answers using its own model, in a restricted no-tools mode (no
    terminal, files, browser or MCP). Because a turn can take >30s, this
    tool does NOT block: it starts the job and returns {job_id, status}. Poll
    ask_hermes_status(job_id) every ~4s until status is 'done' or 'error',
    then read result.answer.

    Args:
        question: The question, in any language Hermes handles.
        max_chars: Truncate the final answer to this many characters (default 8000).
    """
    try:
        result = start_ask_job(question, max_chars=max_chars)
        return _json(result)
    except AskError as exc:
        log.warning("ask_hermes failed: %s", exc)
        return _json({"error": str(exc)})
    except Exception as exc:
        log.exception("ask_hermes failed")
        return _json({"error": str(exc)})


@server.tool()
def ask_hermes_status(job_id: str) -> str:
    """Poll the status and result of an ask_hermes job.

    Args:
        job_id: The id returned by ask_hermes.
    """
    try:
        result = ask_job_status(job_id)
        return _json(result)
    except AskError as exc:
        log.warning("ask_hermes_status failed: %s", exc)
        return _json({"error": str(exc)})
    except Exception as exc:
        log.exception("ask_hermes_status failed")
        return _json({"error": str(exc)})


def main() -> None:
    log.info("starting hermes-chatgpt-hub (db=%s)", hub_db_path())
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
