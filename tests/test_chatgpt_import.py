from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from hermes_hub.db import connect, fetch, init_db, search
from hermes_hub.index_chatgpt import flatten_conversation, index_chatgpt


SAMPLE = {
    "id": "conv-1",
    "title": "Plano de estudos ITA",
    "create_time": 1000.0,
    "update_time": 2000.0,
    "current_node": "n2",
    "mapping": {
        "n1": {
            "id": "n1",
            "parent": None,
            "message": {
                "author": {"role": "user"},
                "content": {"parts": ["Quero estudar no ITA."]},
                "create_time": 1001.0,
            },
        },
        "n2": {
            "id": "n2",
            "parent": "n1",
            "message": {
                "author": {"role": "assistant"},
                "content": {"parts": ["Vamos montar um cronograma semanal."]},
                "create_time": 1002.0,
            },
        },
        "n3": {
            "id": "n3",
            "parent": "n1",
            "message": {
                "author": {"role": "assistant"},
                "content": {"parts": ["Resposta de um branch antigo."]},
                "create_time": 1003.0,
            },
        },
    },
}


class ChatGptImportTest(unittest.TestCase):
    def test_flatten_uses_active_branch(self) -> None:
        doc = flatten_conversation(SAMPLE)
        self.assertIsNotNone(doc)
        assert doc is not None
        self.assertEqual(doc["title"], "Plano de estudos ITA")
        self.assertIn("cronograma semanal", doc["body"])
        self.assertNotIn("branch antigo", doc["body"])
        self.assertEqual(doc["metadata"]["message_count"], 2)

    def test_index_json_and_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            json_path = tmp_path / "conversations.json"
            json_path.write_text(json.dumps([SAMPLE], ensure_ascii=False), encoding="utf-8")

            db_path = tmp_path / "hub.db"
            conn = connect(db_path)
            init_db(conn)
            result = index_chatgpt(conn, json_path, progress=False)
            self.assertEqual(result["conversations"], 1)

            results = search(conn, "cronograma semanal")
            self.assertEqual(len(results), 1)
            doc = fetch(conn, doc_id=results[0]["id"])
            assert doc is not None
            self.assertIn("Quero estudar no ITA", doc["body"])

            zip_path = tmp_path / "export.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("conversations.json", json_path.read_text(encoding="utf-8"))
            result = index_chatgpt(conn, zip_path, reset=True, progress=False)
            self.assertEqual(result["conversations"], 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
