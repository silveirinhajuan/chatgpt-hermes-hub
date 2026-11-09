from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hermes_hub.db import connect, init_db, search
from hermes_hub.index_hermes import index_memories, index_skills


class HermesIndexTest(unittest.TestCase):
    def test_memories_and_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "hermes"
            (home / "memories").mkdir(parents=True)
            (home / "memories" / "MEMORY.md").write_text(
                "Fato A: o Juan usa Caucaia-CE.\n§\nFato B: prefere respostas curtas.\n",
                encoding="utf-8",
            )
            skill_dir = home / "skills" / "exemplo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: exemplo\ndescription: Skill de teste\n---\n\nConteúdo da skill.\n",
                encoding="utf-8",
            )

            conn = connect(Path(tmp) / "hub.db")
            init_db(conn)
            mem = index_memories(conn, home=home, progress=False)
            self.assertEqual(mem["memory_chunks"], 2)
            skills = index_skills(conn, home=home, progress=False)
            self.assertEqual(skills["skills"], 1)

            self.assertGreaterEqual(len(search(conn, "Caucaia")), 1)
            self.assertGreaterEqual(len(search(conn, "Skill de teste")), 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
