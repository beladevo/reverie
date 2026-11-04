import multiprocessing
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT  # noqa: F401
from reverie.store import Actor, ConflictError, Store, hash_bytes

ACTOR = Actor(kind="agent", session="s-1", tool="Write")


def _writer(root: str, worker: int) -> None:
    store = Store(Path(root))
    for i in range(10):
        store.write(f"w{worker}/f{i}.md", f"{worker}-{i}".encode(), Actor(kind="agent", session=f"s-{worker}"))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.create(Path(self._tmp.name) / "s", "s", "notes", "user")

    def tearDown(self):
        self._tmp.cleanup()

    def test_every_write_is_a_new_immutable_version(self):
        first = self.store.write("a.md", b"one", ACTOR)
        second = self.store.write("a.md", b"two", ACTOR)
        self.assertEqual(second["parent"], first["hash"])
        self.assertEqual(self.store.get_blob(first["hash"]), b"one")
        self.assertEqual((self.store.tree / "a.md").read_bytes(), b"two")
        self.assertEqual([e["seq"] for e in self.store.history("a.md")], [1, 2])
        self.assertEqual(second["actor"], {"kind": "agent", "session": "s-1", "tool": "Write"})

    def test_identical_write_is_not_versioned(self):
        self.store.write("a.md", b"one", ACTOR)
        self.assertEqual(self.store.write("a.md", b"one", ACTOR), {})
        self.assertEqual(self.store.head_seq(), 1)

    def test_if_match_rejects_stale_base(self):
        self.store.write("a.md", b"one", ACTOR)
        with self.assertRaises(ConflictError):
            self.store.write("a.md", b"two", ACTOR, if_match=hash_bytes(b"other"))
        with self.assertRaises(ConflictError):
            self.store.write("a.md", b"two", ACTOR, if_match=None)
        self.store.write("a.md", b"two", ACTOR, if_match=hash_bytes(b"one"))

    def test_delete_and_rollback_append_history(self):
        self.store.write("x/a.md", b"one", ACTOR)
        self.store.write("x/a.md", b"two", ACTOR)
        self.store.delete("x/a.md", ACTOR)
        self.assertFalse((self.store.tree / "x").exists())
        self.store.rollback_path("x/a.md", 1, ACTOR)
        self.assertEqual((self.store.tree / "x/a.md").read_bytes(), b"one")
        self.assertEqual(self.store.head_seq(), 4)
        self.assertEqual(self.store.history("x/a.md")[-1]["reason"], "rollback to seq 1")

    def test_rollback_store_restores_whole_tree(self):
        self.store.write("a.md", b"a1", ACTOR)
        self.store.write("b.md", b"b1", ACTOR)
        self.store.write("a.md", b"a2", ACTOR)
        self.store.write("c.md", b"c1", ACTOR)
        self.store.rollback_store(2, ACTOR)
        self.assertEqual(self.store.tree_files(), ["a.md", "b.md"])
        self.assertEqual((self.store.tree / "a.md").read_bytes(), b"a1")

    def test_record_flags_conflict_when_session_saw_old_version(self):
        first = self.store.write("a.md", b"one", ACTOR)
        self.store.write("a.md", b"two", Actor(kind="agent", session="s-2"))
        (self.store.tree / "a.md").write_bytes(b"three")
        entry = self.store.record("a.md", ACTOR, parent_seen=first["hash"])
        self.assertEqual(entry["conflict"], {"seen": first["hash"]})
        self.assertEqual(self.store.get_blob(self.store.state()["a.md"]), b"three")

    def test_sync_external_versions_out_of_band_edits(self):
        self.store.write("a.md", b"one", ACTOR)
        (self.store.tree / "a.md").write_bytes(b"edited in vim")
        (self.store.tree / "b.md").write_bytes(b"new")
        entries = self.store.sync_external(Actor(kind="human"))
        self.assertEqual(sorted(e["path"] for e in entries), ["a.md", "b.md"])
        self.assertEqual(self.store.sync_external(Actor(kind="human")), [])

    def test_writes_never_drop_an_unversioned_external_edit(self):
        first = self.store.write("a.md", b"one", ACTOR)
        (self.store.tree / "a.md").write_bytes(b"edited in vim")
        with self.assertRaises(ConflictError):
            self.store.write("a.md", b"dream", ACTOR, if_match=first["hash"])
        self.assertEqual((self.store.tree / "a.md").read_bytes(), b"edited in vim")
        self.store.write("a.md", b"forced", ACTOR)
        blobs = [self.store.get_blob(e["hash"]) for e in self.store.history("a.md")]
        self.assertEqual(blobs, [b"one", b"edited in vim", b"forced"])
        self.assertEqual(self.store.history("a.md")[1]["actor"]["kind"], "external")

    def test_torn_log_line_is_skipped_and_not_extended(self):
        self.store.write("a.md", b"one", ACTOR)
        with open(self.store.log_path, "a") as f:
            f.write('{"seq": 2, "trunc')
        self.store.write("b.md", b"two", ACTOR)
        self.assertEqual(sorted(self.store.state()), ["a.md", "b.md"])

    def test_paths_cannot_escape_store(self):
        with self.assertRaises(ValueError):
            self.store.write("../evil.md", b"x", ACTOR)

    def test_concurrent_writers_produce_a_consistent_log(self):
        procs = [multiprocessing.Process(target=_writer, args=(str(self.store.root), w)) for w in range(6)]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
        seqs = [e["seq"] for e in self.store.log()]
        self.assertEqual(seqs, list(range(1, 61)))
        self.assertEqual(len(self.store.state()), 60)


if __name__ == "__main__":
    unittest.main()
