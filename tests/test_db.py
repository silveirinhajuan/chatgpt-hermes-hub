from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hermes_hub.db import (
    connect,
    fetch,
    init_db,
    recent,
    remember,
    search,
    stats,
    upsert_document,
)


class HubDbTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "hub.db"
        self.conn = connect(self.db_path)
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_remember_search_fetch_and_dedup(self) -> None:
        first = remember(
            self.conn,
            "O Juan está estudando para o ITA e prefere respostas curtas.",
            title="Perfil do Juan",
            tags=["perfil", "ita"],
        )
        second = remember(
            self.conn,
            "O Juan está estudando para o ITA e prefere respostas curtas.",
            title="Perfil do Juan",
        )
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["deduplicated"])

        results = search(self.conn, "ITA respostas curtas")
        self.assertGreaterEqual(len(results), 1)
        doc_id = results[0]["id"]
        doc = fetch(self.conn, doc_id=doc_id)
        self.assertIsNotNone(doc)
        assert doc is not None
        self.assertIn("ITA", doc["body"])
        self.assertEqual(doc["source"], "chatgpt_note")

        recents = recent(self.conn, limit=5)
        self.assertEqual(recents[0]["id"], first["id"])

        info = stats(self.conn, self.db_path)
        self.assertEqual(info["documents"], 1)
        self.assertEqual(info["by_source"]["chatgpt_note"]["documents"], 1)

    def test_search_special_characters_do_not_crash(self) -> None:
        upsert_document(
            self.conn,
            source="hermes_message",
            source_id="1",
            title="Sessão",
            body="Cálculo diferencial e integral, física quântica.",
            created_at=1000.0,
        )
        self.conn.commit()
        for query in ["cálculo", "física quântica", "C++", "AND OR NOT", '"aspas"']:
            results = search(self.conn, query)
            self.assertIsInstance(results, list)

    def test_fetch_by_source_id(self) -> None:
        upsert_document(
            self.conn,
            source="hermes_memory",
            source_id="memory:MEMORY.md:0001",
            title="MEMORY.md · 1",
            body="Preferência: respostas curtas.",
            created_at=1000.0,
        )
        self.conn.commit()
        doc = fetch(
            self.conn,
            source="hermes_memory",
            source_id="memory:MEMORY.md:0001",
        )
        self.assertIsNotNone(doc)
        assert doc is not None
        self.assertIn("respostas curtas", doc["body"])


if __name__ == "__main__":
    unittest.main()
