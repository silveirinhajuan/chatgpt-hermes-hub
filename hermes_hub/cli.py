from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import hub_db_path, state_db_path
from .db import (
    connect,
    delete_source,
    fetch as db_fetch,
    get_meta,
    init_db,
    recent as db_recent,
    rebuild_fts,
    search as db_search,
    stats as db_stats,
)
from .index_chatgpt import index_chatgpt
from .index_hermes import default_since_ts, index_all_hermes
from .ask import AskError, ask_hermes_answer


def _out(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _parse_since(value: str | None) -> float | None:
    if not value:
        return None
    raw = value.strip()
    try:
        if raw.isdigit():
            return float(raw)
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError as exc:
        raise SystemExit(f"--since inválido: {value!r} (use YYYY-MM-DD ou epoch)") from exc


def _open(args: argparse.Namespace):
    conn = connect(args.db or hub_db_path())
    init_db(conn)
    return conn


def cmd_init(args: argparse.Namespace) -> int:
    conn = _open(args)
    conn.close()
    _out({"ok": True, "db": str(args.db or hub_db_path()), "state_db": str(state_db_path())})
    return 0


def cmd_index_hermes(args: argparse.Namespace) -> int:
    conn = _open(args)
    try:
        explicit_since = _parse_since(args.since)
        if explicit_since is not None:
            since_ts = explicit_since
        elif args.all:
            since_ts = 0.0
        elif args.reset:
            since_ts = max(0.0, time.time() - args.days * 86400)
        else:
            since_ts = default_since_ts(conn, args.days)

        incremental = explicit_since is None and not args.all and not args.reset
        start_id = 0
        if incremental:
            start_id = max(0, int(get_meta(conn, "hermes_last_message_id", "0") or 0) - 100)

        print(
            f"indexando Hermes desde {datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat()} "
            f"(state.db={state_db_path()}, start_id={start_id})",
            file=sys.stderr,
        )
        result = index_all_hermes(
            conn,
            since_ts=since_ts,
            start_id=start_id,
            limit=args.limit,
            reset=args.reset,
            include_memories=not args.no_memories,
            include_skills=not args.no_skills,
            progress=True,
        )
        result["since"] = datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat()
        result["db"] = str(args.db or hub_db_path())
        _out(result)
        return 0
    finally:
        conn.close()


def cmd_index_chatgpt(args: argparse.Namespace) -> int:
    conn = _open(args)
    try:
        since_ts = _parse_since(args.since) or 0.0
        result = index_chatgpt(
            conn,
            args.export,
            since_ts=since_ts,
            reset=args.reset,
            limit=args.limit,
            progress=True,
        )
        result["export"] = str(Path(args.export).expanduser())
        result["db"] = str(args.db or hub_db_path())
        _out(result)
        return 0
    finally:
        conn.close()


def cmd_index_all(args: argparse.Namespace) -> int:
    conn = _open(args)
    try:
        explicit_since = _parse_since(args.since)
        since_ts = explicit_since
        if since_ts is None:
            if args.all:
                since_ts = 0.0
            elif args.reset:
                since_ts = max(0.0, time.time() - args.days * 86400)
            else:
                since_ts = default_since_ts(conn, args.days)
        incremental = explicit_since is None and not args.all and not args.reset
        start_id = 0
        if incremental:
            start_id = max(0, int(get_meta(conn, "hermes_last_message_id", "0") or 0) - 100)
        result: dict[str, Any] = {"hermes": index_all_hermes(
            conn,
            since_ts=since_ts,
            start_id=start_id,
            limit=args.limit,
            reset=args.reset,
            include_memories=not args.no_memories,
            include_skills=not args.no_skills,
            progress=True,
        )}
        if args.chatgpt_export:
            result["chatgpt"] = index_chatgpt(
                conn,
                args.chatgpt_export,
                since_ts=0.0,
                reset=args.reset,
                progress=True,
            )
        result["since"] = datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat()
        result["db"] = str(args.db or hub_db_path())
        _out(result)
        return 0
    finally:
        conn.close()


def cmd_search(args: argparse.Namespace) -> int:
    conn = _open(args)
    try:
        results = db_search(conn, args.query, limit=args.limit, source=args.source)
        _out({"query": args.query, "count": len(results), "results": results})
        return 0
    finally:
        conn.close()


def cmd_fetch(args: argparse.Namespace) -> int:
    conn = _open(args)
    try:
        doc = db_fetch(
            conn,
            doc_id=args.id or None,
            source=args.source or None,
            source_id=args.source_id or None,
        )
        if doc is None:
            _out({"error": "documento não encontrado"})
            return 1
        if args.max_chars and len(doc["body"]) > args.max_chars:
            doc["original_length"] = len(doc["body"])
            doc["body"] = doc["body"][: args.max_chars]
            doc["truncated"] = True
        _out(doc)
        return 0
    finally:
        conn.close()


def cmd_recent(args: argparse.Namespace) -> int:
    conn = _open(args)
    try:
        results = db_recent(conn, limit=args.limit, days=args.days or None, source=args.source)
        _out({"count": len(results), "results": results})
        return 0
    finally:
        conn.close()


def cmd_stats(args: argparse.Namespace) -> int:
    conn = _open(args)
    try:
        _out(db_stats(conn, args.db or hub_db_path()))
        return 0
    finally:
        conn.close()


def cmd_drop_source(args: argparse.Namespace) -> int:
    conn = _open(args)
    try:
        removed = delete_source(conn, args.source)
        conn.commit()
        _out({"source": args.source, "removed": removed})
        return 0
    finally:
        conn.close()


def cmd_rebuild_fts(args: argparse.Namespace) -> int:
    conn = _open(args)
    try:
        rebuild_fts(conn)
        _out({"ok": True, "message": "FTS reconstruído"})
        return 0
    finally:
        conn.close()


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import main as server_main

    server_main()
    return 0

def cmd_ask(args: argparse.Namespace) -> int:
    try:
        result = ask_hermes_answer(
            args.question,
            max_chars=args.max_chars,
            model=args.model,
            provider=args.provider,
            timeout=args.timeout,
        )
    except AskError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        _out(result)
    else:
        print(result["answer"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-hub",
        description="Hub de conhecimento compartilhado entre Hermes Agent e ChatGPT.",
    )
    parser.add_argument("--db", help="caminho do hub.db (default: data/hub.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="cria o banco e o schema FTS5")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("index-hermes", help="indexa mensagens, memórias e skills do Hermes")
    p.add_argument("--since", help="data inicial (YYYY-MM-DD) ou epoch")
    p.add_argument("--days", type=int, default=365, help="janela inicial em dias (default: 365)")
    p.add_argument("--all", action="store_true", help="indexa todo o histórico")
    p.add_argument("--reset", action="store_true", help="apaga os documentos Hermes antes de reindexar")
    p.add_argument("--limit", type=int, help="limite de mensagens (para teste)")
    p.add_argument("--no-memories", action="store_true", help="não indexar MEMORY.md/USER.md/SOUL.md")
    p.add_argument("--no-skills", action="store_true", help="não indexar skills")
    p.set_defaults(func=cmd_index_hermes)

    p = sub.add_parser("index-chatgpt", help="indexa um export do ChatGPT (ZIP ou conversations.json)")
    p.add_argument("export", help="caminho do ZIP, conversations.json ou diretório do export")
    p.add_argument("--since", help="data mínima de atualização (YYYY-MM-DD) ou epoch")
    p.add_argument("--reset", action="store_true", help="apaga conversas ChatGPT antes de reindexar")
    p.add_argument("--limit", type=int, help="limite de conversas (para teste)")
    p.set_defaults(func=cmd_index_chatgpt)

    p = sub.add_parser("index-all", help="indexa Hermes e, opcionalmente, o export do ChatGPT")
    p.add_argument("--chatgpt-export", help="ZIP ou conversations.json do ChatGPT")
    p.add_argument("--since", help="data inicial (YYYY-MM-DD) ou epoch")
    p.add_argument("--days", type=int, default=365, help="janela inicial em dias (default: 365)")
    p.add_argument("--all", action="store_true", help="indexa todo o histórico do Hermes")
    p.add_argument("--reset", action="store_true", help="apaga os documentos antes de reindexar")
    p.add_argument("--limit", type=int, help="limite de mensagens (para teste)")
    p.add_argument("--no-memories", action="store_true", help="não indexar MEMORY.md/USER.md/SOUL.md")
    p.add_argument("--no-skills", action="store_true", help="não indexar skills")
    p.set_defaults(func=cmd_index_all)

    p = sub.add_parser("search", help="busca no hub")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--source", help="filtra por fonte")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("fetch", help="lê um documento inteiro")
    p.add_argument("--id", type=int)
    p.add_argument("--source")
    p.add_argument("--source-id")
    p.add_argument("--max-chars", type=int, default=20000)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("recent", help="lista documentos recentes")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--days", type=int)
    p.add_argument("--source")
    p.set_defaults(func=cmd_recent)

    p = sub.add_parser("stats", help="estatísticas do hub")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("drop-source", help="remove todos os documentos de uma fonte")
    p.add_argument("source", help="hermes_message, hermes_memory, hermes_skill, chatgpt_conversation, chatgpt_note")
    p.set_defaults(func=cmd_drop_source)

    p = sub.add_parser("rebuild-fts", help="reconstrói o índice FTS5")
    p.set_defaults(func=cmd_rebuild_fts)

    p = sub.add_parser("serve", help="inicia o servidor MCP via stdio")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("ask", help="faz uma pergunta ao Hermes (modo sem ferramentas)")
    p.add_argument("question", help="pergunta para o Hermes")
    p.add_argument("--max-chars", type=int, default=8000, dest="max_chars")
    p.add_argument("--json", action="store_true", dest="json", help="saída em JSON")
    p.add_argument("--timeout", type=int, help="timeout em segundos (default: env ou 180)")
    p.add_argument("--model", help="modelo do Hermes a usar (default: config)")
    p.add_argument("--provider", help="provider do Hermes a usar (default: config)")
    p.set_defaults(func=cmd_ask)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.db:
        os.environ["HERMES_HUB_DB"] = str(Path(args.db).expanduser())
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
