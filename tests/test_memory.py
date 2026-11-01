import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from memory import Memory, MemoryFault

REPO = Path(__file__).resolve().parent.parent
CONFIG = json.loads((REPO / "agents.example.json").read_text())
BINARY = os.environ.get("AURORA_MEMORY_BINARY", CONFIG["memory"]["binary"])
SOURCE = "a" * 48


@unittest.skipUnless(shutil.which(BINARY), "Install Aurora's native MEM8 CLI to run persistence checks")
class NativeMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="8c-native-memory-", dir=os.environ.get("TMPDIR", "/tmp/opencode"))
        self.root = Path(self.tmp.name)
        self.config = {**CONFIG["memory"], "binary": BINARY}
        self.memory = self.open()

    def open(self):
        return Memory(self.root, self.config, CONFIG["agents"], REPO)

    def tearDown(self):
        if self.memory:
            self.memory.close()
        self.tmp.cleanup()

    def page(self, events, after=0):
        return {"ok": True, "source": SOURCE, "after": after, "next": after + len(events) + 1,
                "events": [{"cursor": after + i + 1, "event": event} for i, event in enumerate(events)], "caught_up": True}

    def message(self, identifier, text, room="model-lab", visitor="eva", agent="aye"):
        return {"type": "message", "agent": agent, "requester": visitor,
                "message": {"id": identifier, "room": room, "sender_name": visitor, "sender_handle": visitor,
                            "sender_kind": "human", "text": text, "origin": "participant", "timestamp_secs": 1}}

    def job(self, agent="aye", visitor="eva", room="model-lab", query="What keepsake did I tell you about earlier? Name the object and material in one short sentence."):
        return {"id": 80, "parent_id": 999, "agent": agent, "room": room, "requester": visitor, "prompt": query, "query": query}

    def test_native_process_restart_old_recall_and_index_rebuild(self):
        page = self.page([self.message(1, "My keepsake phrase is amber otter telescope."), self.message(2, "What is my name and what do I enjoy making? Answer in one sentence.")])
        self.memory.ingest(page)
        for i in range(3, 75):
            self.memory.ingest(self.page([self.message(i, "A newer unrelated note about lunch.")], self.memory.cursor))
        self.memory.ingest(self.page([self.message(999, self.job()["query"])], self.memory.cursor))
        self.memory.ingest(self.page([self.message(1000, "A future visitor's keepsake is ruby squirrel", visitor="milo")], self.memory.cursor))
        count = self.memory.db.execute("SELECT count(*) FROM records").fetchone()[0]
        self.memory.close()
        self.memory = None
        (self.root / "catalog.sqlite3").rename(self.root / "catalog.before-rebuild.sqlite3")
        # Independent Python process plus independent native CLI reader processes.
        program = """import json,sys
from memory import Memory
p=json.loads(sys.stdin.read())
m=Memory(p['root'],p['config'],p['agents'],p['repo'])
prepared,receipt=m.prepare(p['job'])
print(json.dumps({'prompt':prepared['prompt'],'receipt':receipt,'count':m.db.execute('SELECT count(*) FROM records').fetchone()[0]}))
m.close()
"""
        raw = subprocess.check_output([sys.executable, "-c", program], input=json.dumps({"root": str(self.root), "config": self.config, "agents": CONFIG["agents"], "repo": str(REPO), "job": self.job()}), text=True, cwd=REPO, timeout=30)
        result = json.loads(raw)
        self.assertIn("amber otter telescope", result["prompt"])
        self.assertNotIn("ruby squirrel", result["prompt"])
        self.assertEqual(result["count"], count)
        self.assertEqual(result["receipt"]["format"], "MEM8EV02")
        self.assertTrue(all(p.read_bytes()[:8] == b"MEM8EV02" for p in self.root.glob("records/*/*.m8")))

    def test_scope_isolation_family_identity_and_latest_visitor_profile(self):
        events = [{"type": "profile", "profile": {"handle": "eva", "name": "Eva", "bio": "I build boats", "favorites": []}},
                  self.message(1, "Aye-only keepsake is violet narwhal", "aye-partner"),
                  self.message(2, "Other visitor keepsake is jade parrot", "aye-partner", "milo")]
        self.memory.ingest(self.page(events))
        self.memory.ingest(self.page([{"type": "profile", "profile": {"handle": "eva", "name": "Eva", "bio": "I now make kites", "favorites": []}}], self.memory.cursor))
        prepared, receipt = self.memory.prepare(self.job("cheet", "eva", "tokyo-lab"))
        self.assertIn("You are Cheet", prepared["prompt"])
        self.assertIn("Life Long Learning", prepared["prompt"])
        self.assertIn("I now make kites", prepared["prompt"])
        self.assertNotIn("I build boats", prepared["prompt"])
        self.assertNotIn("jade parrot", prepared["prompt"])
        self.assertNotIn("agent:aye", prepared["prompt"])
        self.assertIn("violet narwhal", prepared["prompt"])  # Eva's own public history follows Eva.
        prepared, _ = self.memory.prepare(self.job("cheet", "nobody", "tokyo-lab"))
        self.assertNotIn("violet narwhal", prepared["prompt"])
        self.assertEqual({r["scope"] for r in receipt["records"]}, {"family", "agent:cheet", "visitor:eva"})

    def test_crash_after_native_write_replay_deduplicates_and_source_is_pinned(self):
        page = self.page([self.message(1, "Remember the copper kite")])
        with patch.object(self.memory, "index", side_effect=OSError("simulated crash after native fsync")):
            with self.assertRaises(OSError):
                self.memory.ingest(page)
        self.assertEqual(self.memory.cursor, 0)
        self.memory.close()
        self.memory = self.open()
        self.memory.ingest(page)
        count = self.memory.db.execute("SELECT count(*) FROM records").fetchone()[0]
        # Simulate loss of just the derived cursor, then replay the same page.
        self.memory.cursor = 0
        self.memory.ingest(page)
        self.assertEqual(self.memory.db.execute("SELECT count(*) FROM records").fetchone()[0], count)
        with self.assertRaises(MemoryFault):
            self.memory.ingest({**self.page([], self.memory.cursor), "source": "b" * 48})

    def test_corruption_missing_file_and_second_writer_fail_without_replacement(self):
        with self.assertRaises(BlockingIOError):
            self.open()
        self.memory.ingest(self.page([]))
        ref = self.memory.identities["aye"]
        path = self.memory.path(ref)
        good = path.read_bytes()
        damaged = good[:25] + bytes([good[25] ^ 1]) + good[26:]
        path.write_bytes(damaged)
        with self.assertRaises(MemoryFault):
            self.memory.prepare(self.job())
        self.assertEqual(path.read_bytes(), damaged)
        path.rename(path.with_suffix(".damaged"))
        with self.assertRaises(MemoryFault):
            self.memory.prepare(self.job())

    def test_generated_turn_is_saved_but_not_recalled_as_delivered(self):
        self.memory.ingest(self.page([]))
        job = self.job()
        _, receipt = self.memory.prepare(job)
        result = {"text": "Unacknowledged silver pangolin", "digest": "c" * 64}
        written = self.memory.remember_generation(CONFIG["agents"][0], job, result, receipt)
        self.assertEqual(self.memory.read(written["ref"])["provenance"]["state"], "generated")
        prepared, _ = self.memory.prepare(self.job(query="silver pangolin"))
        self.assertNotIn("Unacknowledged silver pangolin", prepared["prompt"])


if __name__ == "__main__":
    unittest.main()
