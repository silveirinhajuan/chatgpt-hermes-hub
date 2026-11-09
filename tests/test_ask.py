from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hermes_hub import ask
from hermes_hub.ask import AskError, ask_hermes_answer, redact_secrets, _clean_answer


class RedactionTest(unittest.TestCase):
    def test_redacts_sk_key(self):
        text = "a key sk-abcdefghijklmnopqrstuvwxyz123456 end"
        self.assertIn("[REDACTED_SK_KEY]", redact_secrets(text))
        self.assertNotIn("sk-abcdefghijklmnop", redact_secrets(text))

    def test_redacts_assignment(self):
        text = "api_key = supersecretvalue123"
        self.assertIn("api_key: [REDACTED]", redact_secrets(text))

    def test_redacts_bearer(self):
        text = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"
        self.assertIn("Bearer [REDACTED]", redact_secrets(text))

    def test_redacts_private_key(self):
        text = "-----BEGIN PRIVATE KEY-----\nSECRET\n-----END PRIVATE KEY-----"
        self.assertEqual("[REDACTED_PRIVATE_KEY]", redact_secrets(text))


class CleanAnswerTest(unittest.TestCase):
    def test_strips_session_id_line(self):
        text = "Resposta aqui.\n\nsession_id: abc123\n"
        self.assertEqual("Resposta aqui.", _clean_answer(text))

    def test_collapses_blanks(self):
        text = "a\n\n\n\nb"
        self.assertEqual("a\n\nb", _clean_answer(text))


class ValidationTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("HERMES_HUB_ASK_DISABLED", None)
        os.environ.pop("HERMES_HUB_ASK_ENABLED", None)
        os.environ.pop("HERMES_HUB_ASK_DEPTH", None)

    def test_empty_question(self):
        with self.assertRaises(AskError):
            ask_hermes_answer("   ")

    def test_recursion_guard(self):
        with mock.patch.dict(os.environ, {"HERMES_HUB_ASK_DEPTH": "1"}):
            with self.assertRaises(AskError):
                ask_hermes_answer("oi")

    def test_disabled(self):
        with mock.patch.dict(os.environ, {"HERMES_HUB_ASK_DISABLED": "1"}):
            with self.assertRaises(AskError):
                ask_hermes_answer("oi")

    def test_too_long(self):
        question = "a" * (ask._max_question_chars() + 1)
        with self.assertRaises(AskError):
            ask_hermes_answer(question)


class AskSuccessTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("HERMES_HUB_ASK_DISABLED", None)
        os.environ.pop("HERMES_HUB_ASK_ENABLED", None)
        os.environ.pop("HERMES_HUB_ASK_DEPTH", None)
        # avoid rate limit delays
        os.environ["HERMES_HUB_ASK_MIN_INTERVAL"] = "0"

    def test_success_path(self):
        canned = {"ok": True, "rc": 0, "answer": "Resposta em português.\n\nsession_id: x"}
        with mock.patch("hermes_hub.ask._invoke_worker", return_value=(canned, "", 0)):
            result = ask_hermes_answer("Qual é a capital da França?", max_chars=100)
        self.assertEqual("Resposta em português.", result["answer"])
        self.assertFalse(result["truncated"])
        self.assertIn("elapsed_ms", result)

    def test_truncation(self):
        canned = {"ok": True, "rc": 0, "answer": "A" * 500}
        with mock.patch("hermes_hub.ask._invoke_worker", return_value=(canned, "", 0)):
            result = ask_hermes_answer("oi", max_chars=250)
        self.assertEqual(250, len(result["answer"]))
        self.assertTrue(result["truncated"])

    def test_error_from_worker(self):
        canned = {"ok": False, "error": "boom"}
        with mock.patch("hermes_hub.ask._invoke_worker", return_value=(canned, "", 1)):
            with self.assertRaises(AskError):
                ask_hermes_answer("oi")

    def test_suspicious_marker_aborts(self):
        canned = {"ok": True, "rc": 0, "answer": "aqui <longcat_tool_call>x</longcat_tool_call>"}
        with mock.patch("hermes_hub.ask._invoke_worker", return_value=(canned, "", 0)):
            with self.assertRaises(AskError):
                ask_hermes_answer("oi")

    def test_redaction_applied(self):
        canned = {"ok": True, "rc": 0, "answer": "chave sk-abcdefghijklmnopqrstuvwxyz123456"}
        with mock.patch("hermes_hub.ask._invoke_worker", return_value=(canned, "", 0)):
            result = ask_hermes_answer("oi")
        self.assertIn("[REDACTED_SK_KEY]", result["answer"])


class WorkerFileTest(unittest.TestCase):
    def test_worker_exists(self):
        self.assertTrue(ask.WORKER_PATH.is_file())


if __name__ == "__main__":
    unittest.main()
