#!/usr/bin/env python3
"""Regression tests for the isolated Experiment 1 harness."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import e1_experiment as e1


def build_formal_fixture(root: Path) -> dict:
    workspace = (root / "workspace").resolve()
    workspace.mkdir(parents=True)
    manifest_path = workspace / "config/manifest-frozen.json"
    manifest = {
        "schema": e1.MANIFEST_SCHEMA,
        "status": "frozen",
        "entries": [{
            "entry_id": "paper",
            "work_id": "work",
            "decision": "include",
            "sequence_index": 1,
            "publication_date": "2020",
            "canonical_pdf": str(workspace / "source.pdf"),
        }],
    }
    (workspace / "source.pdf").write_bytes(b"%PDF-fixture")
    e1.atomic_write_json(manifest_path, manifest)
    run_lock_path = workspace / "config/run-lock.json"
    e1.atomic_write_json(run_lock_path, {
        "schema": e1.RUN_LOCK_SCHEMA,
        "manifest_sha256": e1.sha256_file(manifest_path),
    })
    run_lock_sha256 = e1.sha256_file(run_lock_path)
    artifact_root = workspace / "runs" / run_lock_sha256[:12] / "local-artifacts"
    entry = manifest["entries"][0]
    artifact_dir = e1._local_artifact_dir(workspace, entry, artifact_root)
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "local-units.json").write_text("{}", encoding="utf-8")
    e1.atomic_write_json(artifact_dir / "bundle.json", {
        "schema": "wikigraph.e1.local-bundle.v1",
        "entry_id": "paper",
        "work_id": "work",
        "sequence_index": 1,
        "files": [{
            "path": "local-units.json",
            "sha256": e1.sha256_file(artifact_dir / "local-units.json"),
        }],
    })
    steps = {
        phase: {
            "paper": {
                "status": "pending", "attempts": 0, "updated_at": "",
                "error": "", "artifacts": [],
            }
        }
        for phase in e1.RUN_PHASES
    }
    for phase in e1.RUN_PHASES[:e1.RUN_PHASES.index("local_validate") + 1]:
        phase_artifact = artifact_dir / f"{phase}.checkpoint"
        phase_artifact.write_text(phase, encoding="utf-8")
        steps[phase]["paper"] = {
            "status": "completed", "attempts": 1, "updated_at": "now", "error": "",
            "artifacts": [{
                "path": str(phase_artifact),
                "sha256": e1.sha256_file(phase_artifact),
            }],
        }
    state = {
        "schema": e1.RUN_STATE_SCHEMA,
        "created_at": "now",
        "mode": "formal",
        "manifest": str(manifest_path),
        "manifest_sha256": e1.sha256_file(manifest_path),
        "run_lock": str(run_lock_path),
        "run_lock_sha256": run_lock_sha256,
        "artifact_root": str(artifact_root),
        "entry_order": ["paper"],
        "steps": steps,
    }
    state_path = workspace / "state/formal-run-state.json"
    e1.atomic_write_json(state_path, state)
    audit_path = workspace / "logs/formal-summary.json"
    semantic_audit_path = workspace / "logs/formal-semantic-audit.json"
    e1.atomic_write_json(audit_path, {
        "schema": "wikigraph.e1.local-audit.v1",
        "passed": True,
        "entry_count": 1,
        "manifest_sha256": state["manifest_sha256"],
    })
    e1.atomic_write_json(semantic_audit_path, {
        "schema": "wikigraph.e1.semantic-audit.v1",
        "passed": True,
        "entry_count": 1,
        "manifest_sha256": state["manifest_sha256"],
        "run_lock_sha256": run_lock_sha256,
    })
    return {
        "workspace": workspace,
        "state": state,
        "state_path": state_path,
        "manifest": manifest,
        "audit_path": audit_path,
        "semantic_audit_path": semantic_audit_path,
        "artifact_dir": artifact_dir,
    }


class E1ExperimentTests(unittest.TestCase):
    def test_inventory_classifies_related_and_missing_pdf_without_writing_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "2020-main"
            canonical.mkdir()
            (canonical / "paper.md").write_text("# A canonical scientific paper\n", encoding="utf-8")
            (canonical / "paper.pdf").write_bytes(b"%PDF-fixture")
            related = root / "2020-main-supplementary"
            related.mkdir()
            (related / "paper.md").write_text("# Supplementary material for paper\n", encoding="utf-8")

            entries = e1.inventory_entries(root)
            self.assertEqual(2, len(entries))
            by_id = {item["entry_id"]: item for item in entries}
            self.assertEqual("review", by_id["2020-main"]["decision"])
            self.assertIn("formal-publication-evidence-not-detected", by_id["2020-main"]["review_reasons"])
            self.assertEqual("review", by_id["2020-main-supplementary"]["decision"])
            self.assertIn("canonical-pdf-missing", by_id["2020-main-supplementary"]["review_reasons"])
            self.assertEqual(b"%PDF-fixture", (canonical / "paper.pdf").read_bytes())

    def test_manifest_requires_resolved_reviews_unique_works_and_pdf(self):
        manifest = {
            "schema": e1.MANIFEST_SCHEMA,
            "entries": [
                {"entry_id": "a", "work_id": "w", "decision": "include", "canonical_pdf": "a.pdf"},
                {"entry_id": "b", "work_id": "w", "decision": "include", "canonical_pdf": "b.pdf"},
                {"entry_id": "c", "work_id": "c", "decision": "review", "canonical_pdf": ""},
            ],
        }
        errors = e1.validate_manifest(manifest, expected_publications=2)
        self.assertTrue(any("work_id 重复" in item for item in errors))
        self.assertTrue(any("尚未完成人工裁决" in item for item in errors))
        self.assertTrue(any("预期 2" in item for item in errors))

    def test_apply_decisions_only_changes_named_entries(self):
        manifest = {
            "schema": e1.MANIFEST_SCHEMA,
            "status": "candidate",
            "entries": [
                {"entry_id": "main", "work_id": "main", "decision": "include", "canonical_pdf": "main.pdf"},
                {"entry_id": "supp", "work_id": "supp", "decision": "review", "canonical_pdf": "supp.pdf"},
            ],
        }
        reviewed = e1.apply_decisions(manifest, {
            "supp": {
                "decision": "related", "related_to": "main",
                "adjudication_note": "supplement",
                "publication_date": "2021",
                "publication_date_evidence": "PDF publication header",
            }
        })
        self.assertEqual("reviewed", reviewed["status"])
        self.assertEqual("include", reviewed["entries"][0]["decision"])
        self.assertEqual("related", reviewed["entries"][1]["decision"])
        self.assertEqual("2021", reviewed["entries"][1]["publication_date"])

    def test_identity_metrics_excludes_scaffolding(self):
        metrics = e1.identity_metrics([
            {"eligible": True, "action": "reuse_unique"},
            {"eligible": True, "action": "create_local"},
            {"eligible": True, "action": "abstain_ambiguous"},
            {"eligible": False, "action": "reuse_canonical_id"},
        ])
        self.assertEqual(1, metrics["reuse"])
        self.assertEqual(1, metrics["create"])
        self.assertEqual(1, metrics["abstain"])
        self.assertEqual(0.5, metrics["reuse_fraction"])

    def test_membership_churn_only_uses_old_nodes(self):
        result = e1.membership_churn(
            {"a": ["h1"], "b": []},
            {"a": ["h1"], "b": ["h2"], "new": ["h3"]},
        )
        self.assertEqual(2, result["old_nodes"])
        self.assertEqual(1, result["changed_nodes"])
        self.assertEqual(0.5, result["membership_churn"])

    def test_sqlite_snapshot_and_jsonl_include_lineage_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.db"
            copy = root / "copy.db"
            jsonl = root / "copy.jsonl"
            with sqlite3.connect(source) as conn:
                conn.execute("CREATE TABLE nodes(path TEXT PRIMARY KEY, title TEXT, description TEXT)")
                conn.execute("CREATE TABLE edges(id INTEGER PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT, score REAL)")
                conn.execute("CREATE TABLE edge_origins(edge_id INTEGER, origin_page TEXT, source TEXT)")
                conn.execute("INSERT INTO nodes VALUES('n', 'Node', 'Description')")
                conn.execute("INSERT INTO edges VALUES(1, 'n', '聚类于', 'h', 0.75)")
                conn.execute("INSERT INTO edge_origins VALUES(1, 'p', 'raw#L1')")
            e1.snapshot_sqlite(source, copy)
            counts = e1.export_graph_jsonl(copy, jsonl)
            self.assertEqual(1, counts["nodes"])
            self.assertEqual(1, counts["edge_origins"])
            rows = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
            edge = next(item for item in rows if item.get("_table") == "edges")
            self.assertEqual(0.75, edge["score"])

    def test_checkpoint_resume_requires_completed_artifact_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "manifest.json"
            pdf_a = root / "a.pdf"
            pdf_b = root / "b.pdf"
            pdf_a.write_bytes(b"pdf-a")
            pdf_b.write_bytes(b"pdf-b")
            manifest = {
                "schema": e1.MANIFEST_SCHEMA,
                "entries": [
                    {"entry_id": "a", "work_id": "a", "decision": "include", "canonical_pdf": str(pdf_a), "publication_date": "2020", "sequence_index": 1},
                    {"entry_id": "b", "work_id": "b", "decision": "include", "canonical_pdf": str(pdf_b), "publication_date": "2021", "sequence_index": 2},
                ],
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            manifest["status"] = "frozen"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            state = e1.init_run_state(manifest_path, manifest)
            state_path = root / "state.json"
            artifact = root / "a.out"
            artifact.write_text("v1", encoding="utf-8")
            e1.checkpoint_step(state_path, state, "extract", "a", "running")
            e1.checkpoint_step(state_path, state, "extract", "a", "completed", artifacts=[artifact])
            self.assertEqual(["b"], e1.resume_queue(state, "extract"))
            self.assertEqual(1, e1.highest_contiguous_completed(state, "extract"))
            artifact.write_text("changed", encoding="utf-8")
            self.assertEqual(["a", "b"], e1.resume_queue(state, "extract"))
            self.assertEqual(0, e1.highest_contiguous_completed(state, "extract"))

    def test_local_artifact_id_uses_frozen_sequence(self):
        self.assertEqual(
            "D007-2020-paper-测试",
            e1.local_artifact_id({"sequence_index": 7, "entry_id": "2020 paper 测试"}),
        )

    def test_verify_local_bundle_detects_hash_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            artifact = artifact_dir / "wiki.md"
            artifact.write_text("v1", encoding="utf-8")
            (artifact_dir / "bundle.json").write_text(json.dumps({
                "schema": "wikigraph.e1.local-bundle.v1",
                "files": [{"path": "wiki.md", "sha256": e1.sha256_file(artifact)}],
            }), encoding="utf-8")
            self.assertEqual([], e1.verify_local_bundle(artifact_dir))
            artifact.write_text("v2", encoding="utf-8")
            self.assertTrue(any("哈希漂移" in item for item in e1.verify_local_bundle(artifact_dir)))

    def test_embedding_cache_can_be_isolated(self):
        import embed_helper
        import numpy as np
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "embeddings.db"
            embed_helper.configure_cache(target)
            original = embed_helper.embed_batch
            embed_helper.embed_batch = lambda texts: np.array([[1.0, 0.0] for _ in texts])
            try:
                self.assertEqual(target.resolve(), embed_helper._get_embed_db().resolve())
                values = embed_helper.embed_cached_batch(["isolated"])
                self.assertEqual((1, 2), values.shape)
                with sqlite3.connect(target) as conn:
                    self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0])
            finally:
                embed_helper.embed_batch = original
                embed_helper.configure_cache(None)

    def test_hub_birth_gate_persists_input_and_requires_hash_bound_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            entry = {"entry_id": "paper", "work_id": "work"}
            candidates = [{"members": ["a", "b", "c", "d"], "cohesion": 0.8}]
            with self.assertRaises(e1.AgentRequired):
                e1._load_hub_birth_gate(workspace, entry, 4, candidates)
            request_path = workspace / "logs/hub-gate-inputs/G004.json"
            request = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual("wikigraph.e1.hub-gate-input.v1", request["schema"])
            expected = e1.sha256_json(e1._gate_hash_basis({"step": 4, "candidates": candidates}))
            self.assertEqual(expected, request["candidate_input_sha256"])
            jittered = [{"members": ["a", "b", "c", "d"], "cohesion": 0.8004}]
            self.assertEqual(
                expected,
                e1.sha256_json(e1._gate_hash_basis({"step": 4, "candidates": jittered})),
            )

            gate_path = workspace / "config/hub-gates/G004.json"
            gate_path.parent.mkdir(parents=True)
            gate_path.write_text(json.dumps({
                "schema": "wikigraph.e1.hub-gate.v1",
                "candidate_input_sha256": request["candidate_input_sha256"],
                "decisions": [{"candidate_index": 1, "decision": "reject_birth"}],
            }), encoding="utf-8")
            actual_hash, actual_path, gate = e1._load_hub_birth_gate(
                workspace, entry, 4, candidates,
            )
            self.assertEqual(request["candidate_input_sha256"], actual_hash)
            self.assertEqual(gate_path, actual_path)
            self.assertEqual("reject_birth", gate["decisions"][0]["decision"])

            reused_hash, reused_path, reused_gate = e1._load_hub_birth_gate(
                workspace, entry, 5, candidates,
            )
            self.assertEqual(
                e1.sha256_json(e1._gate_hash_basis({"step": 5, "candidates": candidates})),
                reused_hash,
            )
            self.assertEqual("wikigraph.e1.hub-gate-reuse.v1", json.loads(
                reused_path.read_text(encoding="utf-8"),
            )["schema"])
            self.assertEqual(4, reused_gate["decisions"][0]["reused_from_step"])
            with self.assertRaisesRegex(e1.ContractError, "actual="):
                e1._load_hub_birth_gate(
                    workspace, entry, 4,
                    [{"members": ["a", "b", "c", "changed"], "cohesion": 0.8}],
                )
            with self.assertRaises(e1.AgentRequired):
                e1._load_hub_birth_gate(
                    workspace, entry, 6,
                    [{"members": ["a", "b", "c", "changed"], "cohesion": 0.8}],
                )

    def test_formal_artifact_references_extraction_without_reusing_shakedown_compile(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            entry = {"sequence_index": 1, "entry_id": "paper", "work_id": "paper"}
            source = e1._local_artifact_dir(workspace, entry)
            source.mkdir(parents=True)
            (source / "paper.md").write_text("fresh extraction", encoding="utf-8")
            (source / "paper.pdf").write_bytes(b"%PDF-fresh")
            (source / "wiki.md").write_text("shakedown output", encoding="utf-8")
            formal_root = workspace / "runs/locked/local-artifacts"
            target = e1._local_artifact_dir(workspace, entry, formal_root)
            ref_path = e1._prepare_formal_artifact_reference(workspace, entry, target)
            ref = json.loads(ref_path.read_text(encoding="utf-8"))
            self.assertEqual("wikigraph.e1.extraction-ref.v1", ref["schema"])
            self.assertFalse((target / "wiki.md").exists())
            self.assertEqual(
                e1.sha256_file(source / "paper.md"),
                next(item["sha256"] for item in ref["files"] if item["path"].endswith("paper.md")),
            )

    def test_run_lock_validation_detects_extraction_hash_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            artifact = root / "paper.md"
            lock = root / "run-lock.json"
            manifest.write_text("{}", encoding="utf-8")
            artifact.write_text("v1", encoding="utf-8")
            lock.write_text(json.dumps({
                "schema": e1.RUN_LOCK_SCHEMA,
                "manifest_sha256": e1.sha256_file(manifest),
                "code_hashes": {},
                "sources": [{
                    "extraction_artifacts": [{
                        "path": str(artifact), "sha256": e1.sha256_file(artifact),
                    }],
                }],
            }), encoding="utf-8")
            self.assertEqual([], e1.validate_run_lock(lock, manifest))
            artifact.write_text("v2", encoding="utf-8")
            self.assertTrue(any(
                "extraction 哈希漂移" in item
                for item in e1.validate_run_lock(lock, manifest)
            ))

    def test_semantics_compiler_retries_parseable_output_within_checkpoint(self):
        import ingest_paper as ip
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            (artifact_dir / "wiki.md").write_text("# Wiki\n", encoding="utf-8")
            responses = iter([
                {"ok": True, "text": "malformed"},
                {"ok": True, "text": "parseable"},
            ])
            originals = {
                "call_text": ip.call_text,
                "build_slots_prompt": ip.build_slots_prompt,
                "parse_delimited": ip.parse_delimited,
                "salvage": ip.salvage_slots_without_delimiter,
                "normalize": ip.normalize_slots,
                "validate": e1._validate_local_semantics,
            }
            try:
                ip.call_text = lambda *args, **kwargs: next(responses)
                ip.build_slots_prompt = lambda _text: "prompt"
                ip.parse_delimited = lambda text, _delimiter: "本论文 | 涉及 | 张量网络" if text == "parseable" else ""
                ip.salvage_slots_without_delimiter = lambda _text: ""
                ip.normalize_slots = lambda text: text + "\n"
                e1._validate_local_semantics = lambda *_args: ({"errors": []}, {})
                artifacts = e1._compile_semantics(
                    {"entry_id": "paper"}, artifact_dir,
                )
            finally:
                ip.call_text = originals["call_text"]
                ip.build_slots_prompt = originals["build_slots_prompt"]
                ip.parse_delimited = originals["parse_delimited"]
                ip.salvage_slots_without_delimiter = originals["salvage"]
                ip.normalize_slots = originals["normalize"]
                e1._validate_local_semantics = originals["validate"]
            self.assertEqual(2, len(list(artifact_dir.glob("semantics-call*.json"))))
            self.assertTrue((artifact_dir / "semantic.txt").is_file())
            self.assertGreaterEqual(len(artifacts), 4)

    def test_bibliography_compiler_applies_frozen_manifest_adjudication(self):
        import ingest_paper as ip
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            (artifact_dir / "paper.md").write_text("# Paper\n", encoding="utf-8")
            (artifact_dir / "paper.pdf").write_bytes(b"%PDF")
            review = {
                "doc_type": "paper", "review_status": "manual_required",
                "bibliographic": {"authors": {"rejected": []}},
            }
            originals = {
                "extract": ip.extract_pdf_bibliography,
                "candidates": ip.build_bibliographic_candidates,
                "prompt": ip.build_bibliographic_review_prompt,
                "call": ip.call_json,
                "validate": ip.validate_bibliographic_review,
                "merge": ip.merge_bibliographic_review,
            }
            try:
                ip.extract_pdf_bibliography = lambda _path: {}
                ip.build_bibliographic_candidates = lambda *_args: {"authors": []}
                ip.build_bibliographic_review_prompt = lambda *_args: "prompt"
                ip.call_json = lambda *_args, **_kwargs: {"ok": True, "parsed": review}
                ip.validate_bibliographic_review = lambda *_args: []
                ip.merge_bibliographic_review = lambda *_args: {
                    "title": "Paper", "authors": ["Author"], "year": "2025",
                }
                e1._compile_bibliography({
                    "entry_id": "paper", "work_id": "paper", "publication_date": "2026",
                    "adjudication_note": "Frozen human decision",
                    "publication_date_evidence": "PDF journal header says 2026",
                }, artifact_dir)
            finally:
                ip.extract_pdf_bibliography = originals["extract"]
                ip.build_bibliographic_candidates = originals["candidates"]
                ip.build_bibliographic_review_prompt = originals["prompt"]
                ip.call_json = originals["call"]
                ip.validate_bibliographic_review = originals["validate"]
                ip.merge_bibliographic_review = originals["merge"]
            payload = json.loads((artifact_dir / "bibliography.json").read_text())
            self.assertEqual("2026", payload["bibliographic"]["year"])
            self.assertEqual(
                ["review_status", "year"],
                [item["field"] for item in payload["normalizations"]],
            )

    def test_run_state_preserves_frozen_sequence_not_work_id_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_a, pdf_z = root / "a.pdf", root / "z.pdf"
            pdf_a.write_bytes(b"a")
            pdf_z.write_bytes(b"z")
            manifest = {
                "schema": e1.MANIFEST_SCHEMA,
                "status": "frozen",
                "entries": [
                    {"entry_id": "a", "work_id": "a", "decision": "include", "canonical_pdf": str(pdf_a), "publication_date": "2021", "sequence_index": 2},
                    {"entry_id": "z", "work_id": "z", "decision": "include", "canonical_pdf": str(pdf_z), "publication_date": "2020", "sequence_index": 1},
                ],
            }
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            state = e1.init_run_state(path, manifest)
            self.assertEqual(["z", "a"], state["entry_order"])

    def test_fusion_lock_binds_verified_phase_l_bundles_and_detects_stale_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_formal_fixture(Path(tmp))
            workspace = fixture["workspace"]
            lock_path = workspace / "config/fusion-lock.json"
            with mock.patch.object(e1, "FUSION_LOCK_FILES", ()):
                payload = e1.build_fusion_lock_payload(
                    workspace,
                    fixture["state_path"],
                    audit_path=fixture["audit_path"],
                    semantic_audit_path=fixture["semantic_audit_path"],
                    seed=0,
                )
                self.assertEqual(1, len(payload["phase_l"]["bundles"]))
                e1.atomic_write_json(lock_path, payload)
                self.assertEqual([], e1.validate_fusion_lock(
                    lock_path, workspace, fixture["state"],
                ))
                payload["code_hashes"] = {".scripts/e1_experiment.py": "0" * 64}
                e1.atomic_write_json(lock_path, payload)
                errors = e1.validate_fusion_lock(lock_path, workspace, fixture["state"])
                self.assertTrue(any("代码/配置哈希漂移" in item for item in errors))
            missing = workspace / "config/missing-fusion-lock.json"
            self.assertTrue(any("不存在" in item for item in e1.validate_fusion_lock(
                missing, workspace, fixture["state"],
            )))

    def test_new_fusion_lock_does_not_invalidate_phase_l_checkpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_formal_fixture(Path(tmp))
            workspace = fixture["workspace"]
            with mock.patch.object(e1, "FUSION_LOCK_FILES", ()):
                first = e1.build_fusion_lock_payload(
                    workspace,
                    fixture["state_path"],
                    audit_path=fixture["audit_path"],
                    semantic_audit_path=fixture["semantic_audit_path"],
                    seed=0,
                )
                first_path = workspace / "config/fusion-lock-a.json"
                second_path = workspace / "config/fusion-lock-b.json"
                e1.atomic_write_json(first_path, first)
                second = json.loads(json.dumps(first))
                second["created_at"] = "later"
                e1.atomic_write_json(second_path, second)
                first_fusion = e1._clone_state_for_fusion(
                    fixture["state"], mode="formal", fusion_lock_path=first_path,
                )
                second_fusion = e1._clone_state_for_fusion(
                    fixture["state"], mode="formal", fusion_lock_path=second_path,
                )
                self.assertNotEqual(
                    first_fusion["fusion_lock_sha256"], second_fusion["fusion_lock_sha256"],
                )
                self.assertEqual(
                    first_fusion["steps"]["local_validate"],
                    second_fusion["steps"]["local_validate"],
                )
                self.assertTrue(e1.completed_phase_chain_is_valid(
                    second_fusion, "local_validate", "paper",
                ))
                self.assertEqual([], e1.validate_fusion_lock(
                    second_path, workspace, second_fusion,
                ))

    def test_formal_fusion_layout_is_run_specific_and_rejects_shakedown_artifact_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_formal_fixture(Path(tmp))
            workspace = fixture["workspace"]
            state = fixture["state"]
            layout = e1._fusion_layout(workspace, state, shakedown=False)
            run_root = workspace / "runs" / state["run_lock_sha256"][:12]
            self.assertEqual(run_root, layout["runtime_root"])
            for key in ("db", "state", "graph_run", "snapshots", "fusion_logs"):
                self.assertIn(run_root, Path(layout[key]).parents)
            self.assertNotIn("shakedown", str(layout["snapshots"]))
            wrong = json.loads(json.dumps(state))
            wrong["artifact_root"] = str(workspace / "local-artifacts")
            with self.assertRaisesRegex(e1.ContractError, "run-specific root"):
                e1._fusion_layout(workspace, wrong, shakedown=False)


if __name__ == "__main__":
    unittest.main()
