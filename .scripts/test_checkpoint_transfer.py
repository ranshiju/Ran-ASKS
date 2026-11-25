#!/usr/bin/env python3
"""Real two-machine sealed-pack transfer using isolated, tiny repositories."""
import json
from pathlib import Path
import shutil
import unittest

import checkpoint_transfer as transfer
import workspace_checkpoint as cp
import test_workspace_checkpoint as fixtures


@unittest.skipUnless(shutil.which("restic"), "restic required")
class TransferTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.CheckpointTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root, self.source = fixture.root, fixture.source
        self.password = self.root / "key"
        self.password.write_text("fixture-key")
        self.password.chmod(0o600)
        self.sync = self.root / "sync"
        self.a = self.machine("a")
        self.a.run("init")
        self.a.save(self.source, True)
        self.first = transfer.export_pack(self.a, self.sync)

    def machine(self, name):
        path = self.root / name / "config.json"
        cp.write_json(path, {"repository": str(self.root / (name + "-repo")),
                             "password_file": str(self.password), "workspace_id": "fixture"})
        return cp.Store(path)

    def test_two_machine_incremental_handoff(self):
        self.assertEqual(len(list(self.sync.iterdir())), 2)
        original_hash = cp.digest(self.sync / self.first["archive"])
        b = self.machine("b")
        transfer.import_packs(b, self.sync)
        target = self.root / "working-b"
        b.restore("latest", target)
        (target / "projects/demo/notes/status.md").write_text("edited on B")
        b.save(target, True)
        second = transfer.export_pack(b, self.sync)
        self.assertEqual(second["sequence"], 2)
        self.assertLess(second["bytes"], self.first["bytes"])
        self.assertEqual(cp.digest(self.sync / self.first["archive"]), original_hash)
        self.assertEqual(len(list(self.sync.iterdir())), 4)
        transfer.import_packs(self.a, self.sync)
        with self.assertRaisesRegex(ValueError, "Stale"):
            self.a.save(self.source, True)
        restored = self.root / "back-to-a"
        self.a.restore("latest", restored)
        self.assertEqual((restored / "projects/demo/notes/status.md").read_text(), "edited on B")
        self.assertEqual(transfer.import_packs(self.a, self.sync)["imported_sequences"], [])

    def test_truncated_and_unsafe_pack_rejected_before_import(self):
        archive = self.sync / self.first["archive"]
        original = archive.read_bytes()
        archive.write_bytes(original[:100])
        b = self.machine("b")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            transfer.import_packs(b, self.sync)
        self.assertFalse(Path(b.config["repository"]).exists())
        archive.write_bytes(original)
        receipt = next(self.sync.glob("*.ready.json"))
        doc = json.loads(receipt.read_text())
        doc["objects"]["../escape"] = {"size": 1, "sha256": "a" * 64}
        cp.write_json(receipt, doc)
        with self.assertRaisesRegex(ValueError, "object path"):
            transfer.import_packs(b, self.sync)
        self.assertFalse(Path(b.config["repository"]).exists())

    def test_unsent_local_branch_and_missing_parent_refused(self):
        b = self.machine("b")
        transfer.import_packs(b, self.sync)
        target = self.root / "working-b"
        b.restore("latest", target)
        (target / "projects/demo/notes/status.md").write_text("B changes")
        b.save(target, True)
        (self.source / "projects/demo/notes/status.md").write_text("A changes")
        self.a.save(self.source, True)
        transfer.export_pack(self.a, self.sync)
        with self.assertRaisesRegex(ValueError, "unsent or conflicting"):
            transfer.import_packs(b, self.sync)
        with self.assertRaisesRegex(ValueError, "different head"):
            transfer.export_pack(b, self.sync)
        receipt = sorted(self.sync.glob("*.ready.json"))[0]
        receipt.unlink()
        with self.assertRaisesRegex(ValueError, "incomplete or branched"):
            transfer.import_packs(self.machine("c"), self.sync)


if __name__ == "__main__":
    unittest.main(verbosity=2)
