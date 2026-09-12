#!/usr/bin/env python3
"""ingest_inbox.py Agent/API 分发边界回归测试。"""
import builtins
import contextlib
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("ingest_inbox.py")
spec = importlib.util.spec_from_file_location("ingest_inbox", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_extract_last_json_ignores_domain_status():
    content = (
        '{"hub_scope_route":{"candidates":[{"status":"active"}]}}\n'
        '{"status":"completed","paper_id":"p1","quality_status":"complete"}\n'
        'progress log'
    )
    parsed = module._extract_last_json(content)
    assert parsed["status"] == "completed"
    assert parsed["paper_id"] == "p1"


def test_managed_external_file_staging_and_inbox_boundary():
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "attachment.txt"
        source.write_text("managed intake", encoding="utf-8")
        target = None
        receipt_paths = []
        try:
            target, receipt = module.stage_external_file(
                str(source), "test-managed-intake-20260911.txt"
            )
            receipt_paths.append(module.REPO / receipt["receipt_path"])
            assert target.parent.resolve() == module.INBOX.resolve()
            assert target.read_text(encoding="utf-8") == "managed intake"
            assert receipt["schema"] == "inbox-intake-v1"
            assert receipt["binary_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
            reused_target, reused = module.stage_external_file(
                str(source), "test-managed-intake-20260911.txt"
            )
            receipt_paths.append(module.REPO / reused["receipt_path"])
            assert reused_target == target
            assert reused["action"] == "reused_exact"
            assert receipt_paths[0] != receipt_paths[1]
            assert module._resolve_inbox_file(str(target.relative_to(module.REPO))) == target
            try:
                module._resolve_inbox_file(str(source))
                raise AssertionError("仓库外文件不应被 --file 接受")
            except ValueError as exc:
                assert "--import-file" in str(exc)
        finally:
            if target and target.exists():
                target.unlink()
            for receipt_path in receipt_paths:
                if receipt_path.exists():
                    receipt_path.unlink()


def test_managed_external_file_rejects_symlink_target():
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "source.txt"
        source.write_text("source", encoding="utf-8")
        link = module.INBOX / "test-managed-intake-symlink-20260911.txt"
        try:
            link.symlink_to(source)
            try:
                module.stage_external_file(str(source), link.name)
                raise AssertionError("受管暂存不得复用指向 inbox 外的符号链接")
            except ValueError as exc:
                assert "符号链接" in str(exc)
        finally:
            if link.is_symlink():
                link.unlink()


def test_document_dispatch_marks_inbox_entrypoint():
    command = module.dispatch_command("document", "inbox/a.md", "admin")
    assert command[-2:] == ["--entrypoint", "inbox"]
    tool, args = module.dsi_tool("document", "inbox/a.md", "admin")
    assert tool == "ingest_document_file"
    assert args["entrypoint"] == "inbox"


def test_agent_resume_uses_same_document_transaction():
    txn = "20260911-120000-测试图片"
    completed = types.SimpleNamespace(
        stdout=json.dumps({"status": "prepared", "transaction_id": txn,
                           "semantic_backend": "agent", "ocr_backend": None}),
        stderr="", returncode=0,
    )
    with patch.object(module.inbox_state, "load", return_value={
            "pipeline_script": "ingest_document.py", "semantic_backend": "agent"}), \
            patch.object(module.subprocess, "run", return_value=completed) as run:
        result = module.resume_transaction(txn, "agent")
    command = run.call_args.args[0]
    assert command[-2:] == ["--resume", txn]
    assert result["entrypoint"] == "inbox"
    assert result["semantic_backend"] == "agent"


def test_resume_ocr_parameters_only_apply_to_image_document_transaction():
    txn = "20260911-120000-notes"
    with patch.object(module.inbox_state, "load", return_value={
            "pipeline_script": "ingest_document.py",
            "semantic_backend": "agent",
            "source": "inbox/notes.md",
    }):
        try:
            module.resume_transaction(txn, "agent", allow_remote_ocr=True)
            raise AssertionError("非图片事务不应接受 OCR 参数")
        except ValueError as exc:
            assert "图片文档事务" in str(exc)


def test_extract_last_json_preserves_top_level_batch_envelope():
    payload = {
        "status": "partial",
        "phase": "paper_batch",
        "items": [{"file": "review.pdf", "status": "bibliographic_review_required"}],
    }
    parsed = module._extract_last_json("progress\n" + json.dumps(payload) + "\ndone")
    assert parsed == payload
    assert len(parsed["items"]) == 1


def test_scan_inbox_includes_only_nonempty_facts_pending():
    old_inbox = module.INBOX
    try:
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory) / "inbox"
            inbox.mkdir()
            facts = inbox / "facts-pending.md"
            facts.write_text("# Pending facts\n", encoding="utf-8")
            module.INBOX = inbox
            assert module.scan_inbox() == []

            facts.write_text(
                "# Pending facts\n\n- [2026-09-05] Alice advises Bob.\n",
                encoding="utf-8",
            )
            assert module.scan_inbox() == [facts]
    finally:
        module.INBOX = old_inbox


def test_facts_pending_run_returns_agent_task_without_raw_write():
    originals = (module.REPO, module.INBOX, module.sf.ensure_index, sys.argv)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inbox = root / "inbox"
            inbox.mkdir()
            facts = inbox / "facts-pending.md"
            original_text = (
                "# Pending facts\n\n"
                "- [2026-09-05] **Alice advises Bob.** {: #fact-alice-bob-20260905}\n"
                "- [2026-09-05] **Bob belongs to Lab C.** {: #fact-bob-lab-c-20260905}\n"
            )
            facts.write_text(original_text, encoding="utf-8")
            module.REPO = root
            module.INBOX = inbox
            module.sf.ensure_index = lambda: None
            sys.argv = ["ingest_inbox.py", "--run"]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                module.main()

            payload = json.loads(stdout.getvalue().splitlines()[-1])
            assert payload["status"] == "prepared"
            assert payload["awaiting_agent"] == 1
            assert payload["failed"] == 0
            assert payload["files"][0]["status"] == "prepared"
            assert payload["files"][0]["fact_entries"] == 2
            assert payload["files"][0]["next_action"] == \
                "complete_agent_task"
            assert payload["files"][0]["transaction_id"].startswith("user-assertions-")
            assert payload["files"][0]["write_to"].endswith("proposal.json")
            assert payload["files"][0]["agent_task"]["schema"] == "agent-task-v1"
            assert facts.read_text(encoding="utf-8") == original_text
            assert not (root / "cross-domain" / "raw").exists()
    finally:
        module.REPO, module.INBOX, module.sf.ensure_index, sys.argv = originals


def test_agent_run_directly_dispatches_without_importing_dsh(fail_publication=False):
    originals = (
        module.REPO, module.INBOX, module.sf.ensure_index, module.sf.lookup_exact,
        module.agent_task.ingest_backend, module.dispatch_command, module.subprocess.run,
        module.run_post_ingest_maintenance, sys.argv, builtins.__import__, module._write_json_atomic,
    )
    dispatched = []
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / "inbox"
            inbox.mkdir()
            source = inbox / "notice.md"
            source.write_text("Routine administrative notice.\n", encoding="utf-8")
            module.REPO = root
            module.INBOX = inbox
            module.sf.ensure_index = lambda: None
            module.sf.lookup_exact = lambda _path: None
            module.agent_task.ingest_backend = lambda: "agent"

            def fake_dispatch(file_type, rel_path, subproject, **kwargs):
                dispatched.append((file_type, rel_path, subproject, kwargs))
                return ["deterministic-ingest", rel_path]

            module.dispatch_command = fake_dispatch
            module.subprocess.run = lambda command, **_kwargs: types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"status": "completed", "admin_id": "notice"}),
                stderr="",
            )
            module.run_post_ingest_maintenance = lambda *_args: {"status": "no_action"}
            if fail_publication:
                def fail_report_write(path, value):
                    raise OSError("simulated report persistence failure")
                module._write_json_atomic = fail_report_write

            original_import = builtins.__import__

            def reject_dsh_import(name, *args, **kwargs):
                if name == "dsh.agent_loop":
                    raise AssertionError("Agent inbox must not import DSH")
                return original_import(name, *args, **kwargs)

            builtins.__import__ = reject_dsh_import
            sys.argv = ["ingest_inbox.py", "--run", "--subproject", "admin"]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                module.main()

            payload = json.loads(stdout.getvalue().splitlines()[-1])
            if fail_publication:
                assert payload["file_status"] == "completed"
                assert payload["failed"] == 0
                assert payload["maintenance"]["status"] == "error"
                assert payload["maintenance"]["publication"]["report_persisted"] is False
                return
            report = json.loads((root / payload["report_path"]).read_text(encoding="utf-8"))
            assert payload["status"] == "completed"
            assert report["backend"] == "agent"
            assert report["dsh_log"] == ""
            assert dispatched == [(
                "document", "inbox/notice.md", "admin",
                {"document_type": None, "source_kind": "ordinary"},
            )]
            assert report["tool_outputs"][0]["command"] == [
                "deterministic-ingest", "inbox/notice.md",
            ]
    finally:
        (
            module.REPO, module.INBOX, module.sf.ensure_index, module.sf.lookup_exact,
            module.agent_task.ingest_backend, module.dispatch_command, module.subprocess.run,
            module.run_post_ingest_maintenance, sys.argv, builtins.__import__, module._write_json_atomic,
        ) = originals


def test_api_run_uses_dsh_loop_without_direct_dispatch():
    originals = (
        module.REPO, module.INBOX, module.sf.ensure_index, module.sf.lookup_exact,
        module.agent_task.ingest_backend, module.dispatch_command, module.subprocess.run,
        module.run_post_ingest_maintenance, sys.argv,
    )
    old_agent_loop = sys.modules.get("dsh.agent_loop")
    calls = []

    class FakeSessionLog:
        session_id = "api-session"

        @staticmethod
        def append(event, payload):
            calls.append(("audit", event, payload))

        @staticmethod
        def to_jsonl():
            return ""

    class FakeLoop:
        def __init__(self, mode):
            calls.append(("init", mode))
            self.session_log = FakeSessionLog()
            self.last_structured = None

        def execute(self, tool_name, tool_args):
            calls.append(("execute", tool_name, tool_args))
            self.last_structured = {"status": "completed", "admin_id": "notice"}
            return json.dumps(self.last_structured)

    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / "inbox"
            inbox.mkdir()
            (inbox / "notice.md").write_text(
                "Routine administrative notice.\n", encoding="utf-8"
            )
            module.REPO = root
            module.INBOX = inbox
            module.sf.ensure_index = lambda: None
            module.sf.lookup_exact = lambda _path: None
            module.agent_task.ingest_backend = lambda: "api"
            module.dispatch_command = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("API inbox must not use direct dispatch")
            )
            module.subprocess.run = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("API inbox must not invoke ingest subprocess directly")
            )
            module.run_post_ingest_maintenance = lambda *_args: {"status": "no_action"}
            fake_module = types.ModuleType("dsh.agent_loop")
            fake_module.IngestAgentLoop = FakeLoop
            sys.modules["dsh.agent_loop"] = fake_module
            sys.argv = ["ingest_inbox.py", "--run", "--subproject", "admin"]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                module.main()

            payload = json.loads(stdout.getvalue().splitlines()[-1])
            report = json.loads((root / payload["report_path"]).read_text(encoding="utf-8"))
            assert payload["status"] == "completed"
            assert report["backend"] == "api"
            assert report["dsh_log"].startswith("temp/inbox-dsh/")
            assert ("init", "api") in calls
            assert (
                "execute", "ingest_document_file",
                {
                    "file": "inbox/notice.md",
                    "subproject": "admin",
                    "entrypoint": "inbox",
                },
            ) in calls
    finally:
        (
            module.REPO, module.INBOX, module.sf.ensure_index, module.sf.lookup_exact,
            module.agent_task.ingest_backend, module.dispatch_command, module.subprocess.run,
            module.run_post_ingest_maintenance, sys.argv,
        ) = originals
        if old_agent_loop is None:
            sys.modules.pop("dsh.agent_loop", None)
        else:
            sys.modules["dsh.agent_loop"] = old_agent_loop


def test_agent_low_confidence_classification_is_one_batch_task():
    originals = (
        module.REPO, module.INBOX, module.sf.ensure_index, module.sf.lookup_exact,
        module.agent_task.ingest_backend, module.review_low_confidence_classification,
        module.subprocess.run, sys.argv, builtins.__import__,
    )
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / "inbox"
            inbox.mkdir()
            (inbox / "agenda.txt").write_text("会议安排\n", encoding="utf-8")
            (inbox / "attendees.txt").write_text("参会名单\n", encoding="utf-8")
            module.REPO = root
            module.INBOX = inbox
            module.sf.ensure_index = lambda: None
            module.sf.lookup_exact = lambda _path: None
            module.agent_task.ingest_backend = lambda: "agent"
            module.review_low_confidence_classification = lambda *_args: (
                (_ for _ in ()).throw(AssertionError("Agent classification must not call API"))
            )
            module.subprocess.run = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("Classification handoff must precede dispatch")
            )
            original_import = builtins.__import__

            def reject_dsh_import(name, *args, **kwargs):
                if name == "dsh.agent_loop":
                    raise AssertionError("Agent classification must not import DSH")
                return original_import(name, *args, **kwargs)

            builtins.__import__ = reject_dsh_import
            sys.argv = ["ingest_inbox.py", "--run", "--subproject", "admin"]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                module.main()

            payload = json.loads(stdout.getvalue())
            task = payload["agent_task"]
            assert payload["status"] == "prepared"
            assert payload["total"] == 2
            assert task["schema"] == "agent-task-v1"
            assert task["kind"] == "inbox_classification"
            assert len(task["inputs"]) == 1
            input_payload = json.loads(
                (root / task["inputs"][0]["path"]).read_text(encoding="utf-8")
            )
            assert [item["file"] for item in input_payload["items"]] == [
                "inbox/agenda.txt", "inbox/attendees.txt",
            ]
            assert len(task["outputs"]) == 1
            assert "--classification-file" in task["commands"]["resume"]
    finally:
        (
            module.REPO, module.INBOX, module.sf.ensure_index, module.sf.lookup_exact,
            module.agent_task.ingest_backend, module.review_low_confidence_classification,
            module.subprocess.run, sys.argv, builtins.__import__,
        ) = originals


def test_exact_duplicate_cleanup_reverifies_and_writes_receipt():
    old_repo, old_trash = module.REPO, module.trash_util.trash_path
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "inbox" / "duplicate.pdf"
            raw = root / "academic" / "raw" / "references" / "paper" / "paper.pdf"
            source.parent.mkdir(parents=True)
            raw.parent.mkdir(parents=True)
            content = b"identical-pdf"
            source.write_bytes(content)
            raw.write_bytes(content)
            expected_hash = hashlib.sha256(content).hexdigest()
            trashed = root / "recoverable-trash" / source.name
            module.REPO = root

            def fake_trash(path):
                trashed.parent.mkdir(parents=True)
                Path(path).replace(trashed)

            module.trash_util.trash_path = fake_trash
            cleanup = module.cleanup_exact_duplicate(source, {
                "raw_path": "academic/raw/references/paper/paper.pdf",
                "binary_sha256": expected_hash,
            })
            receipt = json.loads(
                (root / cleanup["receipt_path"]).read_text(encoding="utf-8")
            )
            assert not source.exists()
            assert trashed.read_bytes() == content
            assert raw.read_bytes() == content
            assert receipt["status"] == "trashed"
            assert receipt["binary_sha256"] == expected_hash
    finally:
        module.REPO, module.trash_util.trash_path = old_repo, old_trash


def test_exact_duplicate_cleanup_refuses_sha_mismatch():
    old_repo, old_trash = module.REPO, module.trash_util.trash_path
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "inbox" / "duplicate.pdf"
            raw = root / "academic" / "raw" / "references" / "paper" / "paper.pdf"
            source.parent.mkdir(parents=True)
            raw.parent.mkdir(parents=True)
            source.write_bytes(b"inbox-version")
            raw.write_bytes(b"raw-version")
            expected_hash = hashlib.sha256(b"inbox-version").hexdigest()
            module.REPO = root
            module.trash_util.trash_path = lambda _path: (_ for _ in ()).throw(
                AssertionError("SHA mismatch must not invoke Trash")
            )
            try:
                module.cleanup_exact_duplicate(source, {
                    "raw_path": "academic/raw/references/paper/paper.pdf",
                    "binary_sha256": expected_hash,
                })
                raise AssertionError("SHA mismatch must fail duplicate cleanup")
            except ValueError as exc:
                assert "SHA-256" in str(exc)
            assert source.read_bytes() == b"inbox-version"
            assert raw.read_bytes() == b"raw-version"
            assert not (root / "temp" / "inbox-duplicate-receipts").exists()
    finally:
        module.REPO, module.trash_util.trash_path = old_repo, old_trash


def test_report_counts_separate_duplicates_agent_waits_and_failures():
    counts = module._report_counts([
        {"ok": True, "status": "completed"},
        {"ok": True, "status": "duplicate_found"},
        {"ok": False, "status": "agent_required"},
        {"ok": False, "status": "failed"},
        {"ok": False, "skipped": True, "status": "classification_required"},
    ])
    assert counts == {
        "completed": 1,
        "duplicates": 1,
        "awaiting_agent": 1,
        "pending": 1,
        "degraded": 0,
        "failed": 1,
        "skipped": 1,
    }


def test_duplicate_only_report_has_duplicate_terminal_status():
    report = {
        "total": 1,
        **module._report_counts([{"ok": True, "status": "duplicate_found"}]),
        "files": [{"file": "duplicate.pdf", "ok": True, "status": "duplicate_found"}],
    }
    report_path = module.REPO / "cross-domain" / "ingest-reports" / "duplicate.json"
    compact = module._compact_summary(report, report_path)
    assert compact["status"] == "duplicate_found"
    assert compact["duplicates"] == 1
    assert compact["failed"] == 0


def test_dsi_tool_routes_file_types():
    assert module.dsi_tool("paper", "inbox/a.pdf", "academic") == \
        ("ingest_paper_pdf", {"pdf": "inbox/a.pdf"})
    assert module.dsi_tool("meeting", "inbox/a.txt", "academic") == \
        ("ingest_meeting_txt", {"file": "inbox/a.txt", "subproject": "academic"})
    assert module.dsi_tool("document", "inbox/a.md", "academic", "editorial") == \
        ("ingest_document_file", {"file": "inbox/a.md", "subproject": "academic",
                                  "entrypoint": "inbox", "document_type": "editorial"})
    assert module.dsi_tool("document", "inbox/a.md", "teaching") == \
        ("ingest_document_file", {"file": "inbox/a.md", "subproject": "teaching",
                                  "entrypoint": "inbox"})
    assert module.dsi_tool(
        "document", "inbox/部署会速记.docx", "admin", source_kind="meeting") == (
            "ingest_document_file",
            {"file": "inbox/部署会速记.docx", "subproject": "admin",
             "entrypoint": "inbox", "source_kind": "meeting"},
        )


def test_academic_document_classification_gate():
    assert module.classify_academic_document(Path("inbox/CCCF专题导言初排版-张鹏.pdf")) == "editorial"
    assert module.classify_academic_document(
        Path("inbox/第三届量子智能计算研讨会信息整理.md")) == "conference-summary"
    assert module.classify_academic_document(Path("inbox/量子计算背景资料.docx")) is None
    try:
        module.dsi_tool("document", "inbox/a.md", "academic")
        raise AssertionError("academic 文档缺少类型时应阻断")
    except ValueError as exc:
        assert "classification_required" in str(exc)
    command = module.dispatch_command(
        "document", "inbox/a.md", "academic", "academic-reference")
    assert command[-6:] == ["--subproject", "academic", "--entrypoint", "inbox",
                            "--document-type", "academic-reference"]
    command = module.dispatch_command(
        "document", "inbox/研讨会信息整理.md", "academic", "conference-summary")
    assert command[-6:] == ["--subproject", "academic", "--entrypoint", "inbox",
                            "--document-type", "conference-summary"]


def test_academic_conference_classification_uses_first_h1():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "材料.md"
        path.write_text(
            "# 第二届量子物理与智能计算交叉研讨会资料汇总\n\n正文。\n",
            encoding="utf-8",
        )
        assert module.classify_academic_document(path) == "conference-summary"


def test_explicit_document_type_forces_complete_agent_dispatch():
    originals = (
        module.REPO, module.INBOX, module.sf.ensure_index, module.sf.lookup_exact,
        module.agent_task.ingest_backend, module.classify_file_details,
        module.dispatch_command, module.subprocess.run,
        module.run_post_ingest_maintenance, sys.argv,
    )
    dispatched = []
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inbox = root / "inbox"
            inbox.mkdir()
            source = inbox / "ambiguous.pdf"
            source.write_bytes(b"not-a-real-pdf")
            module.REPO = root
            module.INBOX = inbox
            module.sf.ensure_index = lambda: None
            module.sf.lookup_exact = lambda _path: None
            module.agent_task.ingest_backend = lambda: "agent"
            module.classify_file_details = lambda _path: {
                "file_type": "paper", "score": 4, "threshold": 3,
                "confidence": "high", "needs_api_review": False,
                "source_kind": "ordinary", "markers": ["abstract"],
            }

            def fake_dispatch(file_type, rel_path, subproject, **kwargs):
                dispatched.append((file_type, rel_path, subproject, kwargs))
                return ["deterministic-ingest", rel_path]

            module.dispatch_command = fake_dispatch
            module.subprocess.run = lambda command, **_kwargs: types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"status": "completed", "admin_id": "summary"}),
                stderr="",
            )
            module.run_post_ingest_maintenance = lambda *_args: {"status": "no_action"}
            sys.argv = [
                "ingest_inbox.py", "--run", "--file", "inbox/ambiguous.pdf",
                "--subproject", "academic", "--document-type", "conference-summary",
            ]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                module.main()

            payload = json.loads(stdout.getvalue().splitlines()[-1])
            report = json.loads((root / payload["report_path"]).read_text(encoding="utf-8"))
            assert payload["status"] == "completed"
            assert dispatched == [(
                "document", "inbox/ambiguous.pdf", "academic",
                {"document_type": "conference-summary", "source_kind": "ordinary"},
            )]
            decision = report["classification_decisions"]["inbox/ambiguous.pdf"]
            assert decision["program_file_type"] == "paper"
            assert decision["file_type"] == "document"
            assert decision["document_type"] == "conference-summary"
            assert decision["document_type_source"] == "explicit"
    finally:
        (
            module.REPO, module.INBOX, module.sf.ensure_index, module.sf.lookup_exact,
            module.agent_task.ingest_backend, module.classify_file_details,
            module.dispatch_command, module.subprocess.run,
            module.run_post_ingest_maintenance, sys.argv,
        ) = originals


def test_abbreviation_todo_binds_field_and_filters_page_identity():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        page = "academic/wiki/conferences/20210924-QCAI-2021-summary"
        state = {
            "transaction_id": "txn",
            "wiki_path": page,
            "graph_report": {"descriptive_warnings": [
                {
                    "subject": page, "predicate": "涉及", "object": "量子算法",
                    "issue": "bare_abbreviation", "field": "subject", "value": page,
                },
                {
                    "subject": "QCAI", "predicate": "涉及", "object": "量子算法",
                    "issue": "bare_abbreviation", "field": "subject", "value": "QCAI",
                    "locator": "wiki.md#Navigation",
                },
                {
                    "subject": "研究团队", "predicate": "涉及", "object": "QCAI",
                    "issue": "bare_abbreviation", "field": "object", "value": "QCAI",
                    "locator": "wiki.md#Navigation",
                },
                {
                    "subject": "研究团队", "predicate": "涉及", "object": "QCAI",
                    "issue": "bare_abbreviation", "field": "object", "value": "QCAI",
                    "locator": "wiki.md#Navigation",
                },
            ]},
        }
        module.ic._record_abbreviation_warnings(state, root)
        todo = root / "cross-domain" / "abbreviation-todo.jsonl"
        entries, errors = module.ic._read_abbreviation_todo(todo)
        assert errors == []
        assert len(entries) == 2
        assert {(entry["field"], module.ic._abbreviation_occurrence_target(entry))
                for entry in entries} == {("subject", "QCAI"), ("object", "QCAI")}
        assert all(entry["subject"] != page for entry in entries)

        spec = importlib.util.spec_from_file_location(
            "resolve_abbreviations_field_test",
            Path(__file__).parent / "resolve_abbreviations.py",
        )
        resolver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(resolver)
        resolver_entries, resolver_errors = resolver._read_todo(todo)
        assert resolver_errors == []
        occurrences = [resolver._todo_occurrence(entry) for entry in resolver_entries]
        assert {(item["field"], item["path"]) for item in occurrences} == {
            ("subject", "QCAI"), ("object", "QCAI"),
        }


def test_uncertain_scores_request_api_review_without_changing_program_type():
    original_read = module.read_pdf_text
    original_metadata = module._is_academic_by_metadata
    module._is_academic_by_metadata = lambda _path: False
    try:
        module.read_pdf_text = lambda *_args, **_kwargs: "Abstract\n" + ("正文 " * 100)
        decision = module.classify_file_details(Path("inbox/uncertain.pdf"))
        assert decision["file_type"] == "document"
        assert decision["score"] == 1
        assert decision["confidence"] == "low"
        assert decision["source_kind"] == "ordinary"
        assert decision["needs_api_review"]

        module.read_pdf_text = lambda *_args, **_kwargs: "Abstract\nReferences\n" + ("正文 " * 100)
        decision = module.classify_file_details(Path("inbox/borderline.pdf"))
        assert decision["file_type"] == "document"
        assert decision["score"] == 2
        assert decision["needs_api_review"]

        module.read_pdf_text = lambda *_args, **_kwargs: "Abstract\nReferences\nIntroduction\n" + ("正文 " * 100)
        decision = module.classify_file_details(Path("inbox/borderline.pdf"))
        assert decision["file_type"] == "paper"
        assert decision["score"] == 3
        assert decision["needs_api_review"]

        module.read_pdf_text = lambda *_args, **_kwargs: "普通文档\n" + ("正文 " * 100)
        decision = module.classify_file_details(Path("inbox/document.pdf"))
        assert decision["score"] == 0
        assert decision["confidence"] == "high"
        assert not decision["needs_api_review"]

        module.read_pdf_text = lambda *_args, **_kwargs: (
            "Abstract\nReferences\nIntroduction\nKeywords\n" + ("正文 " * 100)
        )
        decision = module.classify_file_details(Path("inbox/paper.pdf"))
        assert decision["score"] == 4
        assert decision["confidence"] == "high"
        assert not decision["needs_api_review"]
    finally:
        module.read_pdf_text = original_read
        module._is_academic_by_metadata = original_metadata

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "notes.txt"
        path.write_text("会议安排", encoding="utf-8")
        decision = module.classify_file_details(path)
        assert decision["file_type"] == "document"
        assert decision["score"] == 1
        assert decision["needs_api_review"]


def test_docx_meeting_transcript_keeps_document_extractor_and_explicit_source_kind():
    original_preview = module.read_document_preview
    module.read_document_preview = lambda _path: "方复全校长讲话\n部署新学期工作。"
    try:
        decision = module.classify_file_details(Path("inbox/新学期工作部署会 速记.docx"))
    finally:
        module.read_document_preview = original_preview
    assert decision["file_type"] == "document"
    assert decision["source_kind"] == "meeting"
    assert decision["confidence"] == "high"
    assert {"filename_meeting", "filename_transcript"} <= set(decision["markers"])
    command = module.dispatch_command(
        "document", "inbox/新学期工作部署会 速记.docx", "admin",
        source_kind=decision["source_kind"],
    )
    assert command[-2:] == ["--source-kind", "meeting"]


def test_api_classification_review_resolves_uncertain_program_decision():
    decision = {
        "file_type": "paper", "score": 3, "threshold": 3,
        "markers": ["abstract", "references", "introduction"],
        "review_text": "Abstract. Introduction. References.",
    }
    captured = []

    def fake_call(prompt, schema_check, **kwargs):
        captured.append((prompt, kwargs))
        parsed = {
            "doc_type": "paper", "confidence": "medium",
            "reasons": ["具有论文结构"], "evidence_quotes": ["Abstract"],
        }
        assert schema_check(parsed)
        return {"ok": True, "parsed": parsed}

    original_call = module.call_json
    original_backend = module.agent_task.ingest_backend
    module.call_json = fake_call
    module.agent_task.ingest_backend = lambda: "api"
    try:
        review = module.review_low_confidence_classification(
            Path("inbox/borderline.pdf"), decision)
    finally:
        module.call_json = original_call
        module.agent_task.ingest_backend = original_backend

    assert captured[0][1]["operation"] == "ingest_type_review"
    assert captured[0][1]["reasoning"] == "fast"
    assert module.reconcile_classification(decision, review) == ("paper", "")
    mismatch = review | {"doc_type": "document"}
    assert module.reconcile_classification(decision, mismatch) == ("document", "")
    low = review | {"confidence": "low"}
    assert module.reconcile_classification(decision, low)[0] is None
    ambiguous = review | {"doc_type": "ambiguous"}
    assert module.reconcile_classification(decision, ambiguous)[0] is None

    module.call_json = lambda *_args, **_kwargs: {
        "ok": True,
        "parsed": review | {"doc_type": "meeting"},
    }
    module.agent_task.ingest_backend = lambda: "api"
    try:
        invalid = module.review_low_confidence_classification(
            Path("inbox/borderline.pdf"), decision)
    finally:
        module.call_json = original_call
        module.agent_task.ingest_backend = original_backend
    assert invalid["status"] == "review_error"
    assert "不适用于 .pdf" in invalid["error"]


def test_agent_classification_evidence_allows_pdf_line_wrapping():
    decision = {
        "review_text": "A Simple Tensor Network\nAlgorithm for Two-Dimensional Systems",
    }
    review = {
        "doc_type": "paper",
        "evidence_quotes": [
            "A Simple Tensor Network Algorithm for Two-Dimensional Systems",
        ],
    }
    module._validate_agent_classification(Path("inbox/paper.pdf"), decision, review)

    review["evidence_quotes"] = ["A Different Tensor Network Algorithm"]
    try:
        module._validate_agent_classification(Path("inbox/paper.pdf"), decision, review)
    except ValueError as exc:
        assert "evidence_quotes" in str(exc)
    else:
        raise AssertionError("Changed lexical evidence must still be rejected")


def test_plan_ingest_order_versions_last():
    from pathlib import Path
    primary = Path("inbox/contract.txt")
    version = Path("inbox/contract-盖章扫描版.txt")
    other = Path("inbox/paper.pdf")
    classified = [
        (version, "document", "inbox/contract-盖章扫描版.txt"),
        (other, "paper", "inbox/paper.pdf"),
        (primary, "document", "inbox/contract.txt"),
    ]
    ordered, notes = module._plan_ingest_order(classified)
    names = [item[0].name for item in ordered]
    assert names.index("contract.txt") < names.index("contract-盖章扫描版.txt")
    assert "paper.pdf" in names
    assert any("盖章扫描版" in note for note in notes)


def test_map_paper_batch_results():
    parsed = {
        "status": "completed",
        "items": [
            {"source": "inbox/a.pdf", "status": "completed",
             "paper_id": "a-2024", "transaction_id": "t1",
             "proposition_status": "sparse: 2 propositions", "quality_status": "complete",
             "bibliographic_worker": {"api_called": False, "skip_reason": "deterministic_fast_path"},
             "relationship_worker": {"api_called": False, "skip_reason": "deterministic_fast_path"},
             "semantic_repair_worker": {"api_called": False, "skip_reason": "non_blocking_only"},
             "graph_report": {"hub_dynamics": {"membership": {"nodes": [{"candidates": [1, 2]}]}}},
             "quality_warnings": ["proposition: mock"]},
            {"source": "inbox/b.pdf", "status": "duplicate_found",
             "transaction_id": "t2"},
            {"source": "inbox/c.pdf", "status": "failed",
             "errors": ["extraction_failed"]},
        ],
    }
    mapped = module._map_paper_batch_results(parsed)
    assert [r["file"] for r in mapped] == ["a.pdf", "b.pdf", "c.pdf"]
    assert mapped[0]["ok"] is True and mapped[0]["paper_id"] == "a-2024"
    assert mapped[0]["quality_status"] == "complete"
    assert mapped[0]["proposition_status"] == "sparse: 2 propositions"
    assert mapped[0]["bibliographic_worker"]["skip_reason"] == "deterministic_fast_path"
    assert mapped[0]["relationship_worker"]["skip_reason"] == "deterministic_fast_path"
    assert mapped[0]["semantic_repair_worker"]["skip_reason"] == "non_blocking_only"
    assert mapped[0]["graph_report"]["hub_dynamics"], "完整图诊断必须保留给持久化报告"
    assert mapped[1]["ok"] is True and mapped[1]["status"] == "duplicate_found"
    assert mapped[2]["ok"] is False and mapped[2]["reason"] == "extraction_failed"


def test_map_paper_batch_preserves_bibliographic_review_contract():
    parsed = {"status": "partial", "items": [{
        "source": "inbox/review.pdf",
        "status": "bibliographic_review_required",
        "transaction_id": "txn-review",
        "errors": ["authors 候选校验失败"],
        "retryable": False,
        "next_action": "repair_bibliographic_review_then_resume",
        "bibliographic_review": {"status": "validation_error"},
    }]}
    entry = module._map_paper_batch_results(parsed)[0]
    assert entry["status"] == "bibliographic_review_required"
    assert entry["transaction_id"] == "txn-review"
    assert entry["retryable"] is False
    assert entry["next_action"] == "repair_bibliographic_review_then_resume"
    assert entry["bibliographic_review"]["status"] == "validation_error"


def test_result_entry_names_api_host_recovery_not_legacy_handoff():
    parsed = {
        "status": "agent_required",
        "execution_backend": "api",
        "transaction_id": "txn-api-repair",
        "next_action": "repair_api_workspace_then_resume",
        "workspace_worker": {
            "operation": "ingest_paper_workspace", "api_called": True,
        },
    }
    entry = module._result_entry("paper.pdf", "paper", parsed)
    assert entry["reason"] == "API worker 自动恢复耗尽，等待宿主 Agent 修正暂存产物"
    assert "legacy" not in entry["reason"]


def test_auto_resolve_abbr_key_contract():
    import json
    from types import SimpleNamespace

    old_repo = module.REPO
    old_resolve = module.ic.lightweight_abbr_resolve
    old_run = module.subprocess.run
    try:
        with tempfile.TemporaryDirectory() as tmp:
            module.REPO = Path(tmp)
            module.ic.lightweight_abbr_resolve = lambda _repo: {
                "status": "completed", "resolved": 0, "remaining": 1, "details": [],
            }
            module.subprocess.run = lambda *args, **kwargs: SimpleNamespace(
                returncode=0, stderr="", stdout=json.dumps({
                    "status": "agent_required", "resolved_count": 0,
                    "warning_count": 1,
                    "candidates": [{
                        "token": "CONFLICTBANK", "suggested_kind": "canonical_name",
                        "allowed_kinds": ["canonical_name", "ambiguous"],
                    }],
                }),
            )
            summary = module._auto_resolve_abbreviations("session/unsafe")
            assert summary["status"] == "agent_required"
            assert summary["prop_resolved"] == 0
            assert summary["warning_count"] == 1
            assert summary["remaining_tokens"] == 1
            assert summary["remaining_occurrences"] == 1
            review = module.REPO / summary["review_file"]
            payload = json.loads(review.read_text(encoding="utf-8"))
            assert payload["candidates"][0]["token"] == "CONFLICTBANK"
            assert payload["candidate_token_count"] == 1
            assert review.name == "session-unsafe.json"
    finally:
        module.REPO = old_repo
        module.ic.lightweight_abbr_resolve = old_resolve
        module.subprocess.run = old_run


def test_compact_summary_excludes_graph_diagnostics_and_returns_report_path():
    report = {
        "total": 1, "completed": 1, "degraded": 0, "failed": 0, "skipped": 0,
        "backend": "api",
        "files": [{
            "file": "a.pdf", "status": "completed", "quality_status": "complete",
            "transaction_id": "t1", "graph_report": {"huge": [1, 2, 3]},
        }],
    }
    report_path = module.REPO / "cross-domain" / "ingest-reports" / "test.json"
    compact = module._compact_summary(report, report_path)
    assert compact["status"] == "completed"
    assert compact["report_path"] == "cross-domain/ingest-reports/test.json"
    assert "graph_report" not in compact["files"][0]
    assert compact["backend"] == "api"
    warnings = [{"issue": "ambiguous", "detail": "long" * 100, "candidates": ["large"]}] * 8
    warned = {**report, "files": [{**report["files"][0], "quality_warnings": warnings}]}
    warning_summary = module._compact_summary(warned, report_path)["files"][0]
    assert warning_summary["quality_warning_count"] == 8
    assert len(warning_summary["quality_warnings"]) == 5
    assert len(warning_summary["quality_warnings"][0]["detail"]) == 240
    assert "candidates" not in warning_summary["quality_warnings"][0]
    blocked = {**report, "completed": 0, "failed": 0, "awaiting_agent": 1,
               "pending": 1,
               "files": [{"file": "b.pdf", "status": "agent_required", "reason": "agent 接管"}]}
    assert module._compact_summary(blocked, report_path)["status"] == "agent_required"
    classification_blocked = {
        **report, "completed": 0, "skipped": 1,
        "files": [{"file": "c.docx", "status": "classification_required",
                   "reason": "需要显式分类"}],
    }
    assert module._compact_summary(
        classification_blocked, report_path)["status"] == "classification_required"
    bibliographic_blocked = {
        **report, "completed": 0, "failed": 0, "pending": 1,
        "files": [{
            "file": "review.pdf", "status": "bibliographic_review_required",
            "transaction_id": "txn-review", "retryable": False,
            "next_action": "repair_bibliographic_review_then_resume",
        }],
    }
    compact_review = module._compact_summary(bibliographic_blocked, report_path)
    assert compact_review["status"] == "bibliographic_review_required"
    assert compact_review["files"][0]["retryable"] is False
    assert compact_review["files"][0]["next_action"] == "repair_bibliographic_review_then_resume"
    unrelated_hubs = {**report, "maintenance": {
        "status": "agent_required", "receipt_path": "temp/maintenance.json",
        "actions": [{"component": "hubs", "next_action": "agent_define_and_apply_hub_maintenance"}],
        "errors": [], "components": {"hubs": {
            "status": "agent_required", "eligible_count": 82, "split_count": 1,
            "candidates_file": "temp/hubs.json",
            "split_candidates_file": "temp/splits.json",
            "next_action": "agent_define_and_apply_hub_maintenance",
        }},
    }}
    hub_compact = module._compact_summary(unrelated_hubs, report_path)
    assert hub_compact["status"] == "completed"
    assert hub_compact["file_status"] == "completed"
    assert "hub_auto_create" not in hub_compact, \
        "全库维护候选不得覆盖当前摄入终态"
    assert hub_compact["maintenance"]["components"]["hubs"] == {
        "status": "agent_required", "eligible_count": 82, "split_count": 1,
        "candidates_file": "temp/hubs.json",
        "split_candidates_file": "temp/splits.json",
        "next_action": "agent_define_and_apply_hub_maintenance",
    }
    import inspect
    assert "print(content)" not in inspect.getsource(module.main), \
        "--run 不应把底层完整 stdout 回显给 Agent"


def test_auto_create_hubs_writes_split_handoff():
    import json
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace

    old_repo = module.REPO
    old_run = module.subprocess.run
    check = {
        "status": "agent_required",
        "eligible": [],
        "split_candidates": [{
            "decision": "agent_definition_required",
            "hub": "academic/wiki/hubs/big",
            "clusters": [{"members": ["a"]}, {"members": ["b"]}],
        }],
        "redistribution_candidates": [{
            "decision": "redistribution_required",
            "hub": "academic/wiki/hubs/parent",
            "children": ["academic/wiki/hubs/child"],
        }],
        "backlog_count": 0,
        "split_backlog_count": 2,
    }
    try:
        with tempfile.TemporaryDirectory() as tmp:
            module.REPO = Path(tmp)
            module.subprocess.run = lambda *args, **kwargs: SimpleNamespace(
                returncode=0, stdout=json.dumps(check), stderr="",
            )
            results = [{"graph_report": {"hub_dynamics": {"affected_nodes": ["a", "b"]}}}]
            summary = module._auto_create_hubs("session", results)
            assert summary["status"] == "agent_required"
            assert summary["eligible_count"] == 0
            assert summary["split_count"] == 1
            assert summary["redistribution_count"] == 1
            assert summary["affected_node_count"] == 2
            split_path = module.REPO / summary["split_candidates_file"]
            assert json.loads(split_path.read_text(encoding="utf-8")) == check["split_candidates"]
            redistribution_path = module.REPO / summary["redistribution_candidates_file"]
            assert json.loads(redistribution_path.read_text(encoding="utf-8")) == check["redistribution_candidates"]
    finally:
        module.REPO = old_repo
        module.subprocess.run = old_run


def test_low_margin_hub_route_writes_agent_handoff_without_maintenance_scan():
    import json
    import tempfile
    from pathlib import Path

    old_repo = module.REPO
    old_run = module.subprocess.run
    try:
        with tempfile.TemporaryDirectory() as tmp:
            module.REPO = Path(tmp)
            module.subprocess.run = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("route-only handoff must not run Hub maintenance scan")
            )
            results = [{
                "file": "paper.pdf",
                "wiki_path": "academic/wiki/papers/paper",
                "transaction_id": "txn",
                "graph_report": {"hub_scope_route": {
                    "decision": "candidates",
                    "reason": "scope_margin_too_small",
                    "top_score": 0.52,
                    "margin": 0.01,
                    "profile": {"text": "quantum profile", "locator": "paper#scope"},
                    "candidates": [
                        {"path": "hub-a", "title": "A", "scope": "Scope A", "score": 0.52, "canonical": True},
                        {"path": "legacy", "title": "Legacy", "scope": "Legacy", "score": 0.9, "canonical": False},
                        {"path": "hub-b", "title": "B", "scope": "Scope B", "score": 0.51, "canonical": True},
                    ],
                }},
            }]
            summary = module._auto_create_hubs("route-session", results)
            assert summary["status"] == "agent_required"
            assert summary["route_review_count"] == 1
            assert summary["affected_node_count"] == 0
            payload = json.loads(
                (module.REPO / summary["route_review_file"]).read_text(encoding="utf-8")
            )
            assert payload[0]["wiki_path"] == "academic/wiki/papers/paper"
            assert [item["path"] for item in payload[0]["canonical_candidates"]] == [
                "hub-a", "hub-b",
            ]
            assert "route-apply" in payload[0]["apply_command_template"]
            assert "academic/wiki/papers/paper" in payload[0]["apply_command_template"]
            assert "--transaction-id 'txn'" in payload[0]["apply_command_template"]
            results[0]["graph_report"]["hub_scope_route"]["reason"] = \
                "child_specificity_unsupported"
            assert len(module._hub_route_reviews(results)) == 1
            results[0]["graph_report"]["hub_scope_route_current"] = {
                "decision": "resolved",
                "node_id": "hub-b",
                "reason": "agent_confirmed_override",
            }
            assert module._hub_route_reviews(results) == []
    finally:
        module.REPO = old_repo
        module.subprocess.run = old_run


def test_hub_timeout_is_deferred_and_retryable():
    import subprocess
    import tempfile
    from pathlib import Path

    old_repo = module.REPO
    old_run = module.subprocess.run
    try:
        with tempfile.TemporaryDirectory() as tmp:
            module.REPO = Path(tmp)
            module.subprocess.run = lambda *args, **kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(args[0], 120)
            )
            results = [{"graph_report": {"hub_dynamics": {"affected_nodes": ["node"]}}}]
            summary = module._auto_create_hubs("session", results)
            assert summary["status"] == "deferred"
            assert summary["retryable"] is True
            assert summary["affected_node_count"] == 1
    finally:
        module.REPO = old_repo
        module.subprocess.run = old_run


def test_zero_success_skips_global_post_ingest_scans():
    originals = (module._auto_resolve_abbreviations,
                 module.ic.detect_people_page_candidates,
                 module._auto_create_hubs)
    calls = []
    old_repo = module.REPO
    module._auto_resolve_abbreviations = lambda _session: calls.append("abbr")
    module.ic.detect_people_page_candidates = lambda _repo: calls.append("people")
    module._auto_create_hubs = lambda _session, _results: calls.append("hub")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            module.REPO = Path(tmp)
            envelope = module.run_post_ingest_maintenance([
                {"ok": False, "skipped": True, "status": "classification_required"}
            ], "session")
            assert calls == [], "零成功文件不得触发全库收尾扫描"
            assert envelope["status"] == "skipped"
            assert all(item == {"status": "skipped", "reason": "no_successful_files"}
                       for item in envelope["components"].values())
            assert (module.REPO / envelope["receipt_path"]).is_file()

            envelope = module.run_post_ingest_maintenance([
                {"ok": True, "status": "duplicate_found"}
            ], "session")
            assert calls == [], "精确重复没有改变知识库，不得触发全库收尾扫描"
            assert envelope["status"] == "skipped"
    finally:
        module.REPO = old_repo
        (module._auto_resolve_abbreviations,
         module.ic.detect_people_page_candidates,
         module._auto_create_hubs) = originals


def test_maintenance_error_does_not_override_completed_file_status():
    originals = (
        module.REPO, module._auto_resolve_abbreviations,
        module.ic.detect_people_page_candidates, module._auto_create_hubs,
    )
    try:
        with tempfile.TemporaryDirectory() as tmp:
            module.REPO = Path(tmp)
            module._auto_resolve_abbreviations = lambda _session: (_ for _ in ()).throw(
                RuntimeError("resolver failed")
            )
            module.ic.detect_people_page_candidates = lambda _repo: {"status": "completed"}
            module._auto_create_hubs = lambda _session, _results: {"status": "no_action"}
            maintenance = module.run_post_ingest_maintenance(
                [{
                    "file": "paper.pdf", "ok": True, "status": "completed",
                    "graph_report": {"hub_dynamics": {"affected_nodes": []}},
                }], "session"
            )
            assert maintenance["status"] == "error"
            assert any("resolver failed" in error for error in maintenance["errors"])
            report = {
                "total": 1, "completed": 1, "degraded": 0, "failed": 0, "skipped": 0,
                "files": [{"file": "paper.pdf", "status": "completed"}],
                "maintenance": maintenance,
            }
            compact = module._compact_summary(
                report, module.REPO / "cross-domain" / "ingest-reports" / "report.json"
            )
            assert compact["status"] == "completed"
            assert compact["file_status"] == "completed"
            assert compact["maintenance"]["status"] == "error"
    finally:
        (module.REPO, module._auto_resolve_abbreviations,
         module.ic.detect_people_page_candidates, module._auto_create_hubs) = originals


def test_maintenance_rejects_incomplete_completed_result_envelope():
    calls = []
    original_repo = module.REPO
    original_abbr = module._auto_resolve_abbreviations
    try:
        with tempfile.TemporaryDirectory() as tmp:
            module.REPO = Path(tmp)
            module._auto_resolve_abbreviations = lambda _session: calls.append("abbr")
            maintenance = module.run_post_ingest_maintenance([{
                "file": "paper.pdf", "ok": True, "status": "completed",
                "transaction_id": "txn",
            }], "incomplete")
            assert maintenance["status"] == "validation_error"
            assert maintenance["invalid_results"][0]["missing"] == \
                "graph_report.hub_dynamics.affected_nodes"
            assert calls == []
            assert (module.REPO / maintenance["receipt_path"]).is_file()
    finally:
        module.REPO = original_repo
        module._auto_resolve_abbreviations = original_abbr


def test_abbreviation_decisions_require_exact_pending_tokens_and_atomic_todo():
    spec = importlib.util.spec_from_file_location(
        "resolve_abbreviations_test", Path(__file__).parent / "resolve_abbreviations.py"
    )
    resolver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resolver)
    report = resolver.apply_decisions(
        None,
        [
            {"token": "UNKNOWN", "resolution_kind": "canonical_name"},
            {"token": "KNOWN", "resolution_kind": "canonical_name"},
            {"token": "KNOWN", "resolution_kind": "canonical_name"},
        ],
        [{"token": "KNOWN", "page": "academic/wiki/papers/x", "field": "object"}],
    )
    assert report["status"] == "validation_error"
    assert any("not pending" in error for error in report["errors"])
    assert any("duplicate token" in error for error in report["errors"])

    with tempfile.TemporaryDirectory() as tmp:
        todo = Path(tmp) / "abbreviation-todo.jsonl"
        entry = {
            "schema": "abbreviation-todo-v2", "token": "KNOWN",
            "page": "academic/wiki/papers/x", "subject": "x",
            "predicate": "包含", "object": "KNOWN", "field": "object",
        }
        resolver._write_todo(todo, [entry, dict(entry)])
        assert len(todo.read_text(encoding="utf-8").splitlines()) == 1
        assert not todo.with_name(todo.name + ".tmp").exists()


def test_abbreviation_decisions_close_only_matching_maintenance_action():
    spec = importlib.util.spec_from_file_location(
        "resolve_abbreviations_receipt_test", Path(__file__).parent / "resolve_abbreviations.py"
    )
    resolver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resolver)
    old_repo = resolver.REPO
    try:
        with tempfile.TemporaryDirectory() as tmp:
            resolver.REPO = Path(tmp)
            review = resolver.REPO / "temp/abbreviation-review/session.json"
            review.parent.mkdir(parents=True)
            review.write_text(json.dumps({
                "candidates": [{"token": "QA", "occurrences": [{"page": "page"}]}],
            }), encoding="utf-8")
            receipt_path = resolver.REPO / "temp/inbox-maintenance/session.json"
            receipt_path.parent.mkdir(parents=True)
            receipt_path.write_text(json.dumps({
                "status": "agent_required", "errors": [],
                "actions": [
                    {"component": "abbreviations", "review_file": "temp/abbreviation-review/session.json"},
                    {"component": "hubs", "next_action": "agent_review"},
                ],
                "components": {
                    "abbreviations": {
                        "status": "agent_required",
                        "review_file": "temp/abbreviation-review/session.json",
                    },
                    "hubs": {"status": "agent_required"},
                },
            }), encoding="utf-8")
            path, receipt, tokens = resolver._load_maintenance_scope(
                "temp/inbox-maintenance/session.json"
            )
            summary = resolver._close_maintenance_receipt(
                path, receipt, tokens,
                {"applied": [{"token": "QA", "resolution_kind": "alias_to_full_name"}]},
                [],
            )
            updated = json.loads(receipt_path.read_text(encoding="utf-8"))
            assert summary["remaining_tokens"] == 0
            assert updated["components"]["abbreviations"]["status"] == "completed"
            assert updated["status"] == "agent_required"
            assert updated["actions"] == [{"component": "hubs", "next_action": "agent_review"}]
    finally:
        resolver.REPO = old_repo


def test_abbreviation_decisions_cli_closes_linked_maintenance_receipt():
    import contextlib
    import io
    import sys

    spec = importlib.util.spec_from_file_location(
        "resolve_abbreviations_cli_test", Path(__file__).parent / "resolve_abbreviations.py"
    )
    resolver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resolver)
    old_repo = resolver.REPO
    old_connect = resolver.gl.connect
    old_apply_decisions = resolver.apply_decisions
    old_argv = sys.argv

    class FakeConnection:
        def close(self):
            pass

    try:
        with tempfile.TemporaryDirectory() as tmp:
            resolver.REPO = Path(tmp).resolve()
            todo = resolver.REPO / "cross-domain/abbreviation-todo.jsonl"
            resolver._write_todo(todo, [{
                "schema_version": "abbreviation-todo-v2", "token": "QA",
                "page": "academic/wiki/papers/x", "subject": "x",
                "predicate": "包含", "object": "QA", "field": "object",
            }])
            review = resolver.REPO / "temp/abbreviation-review/session.json"
            review.parent.mkdir(parents=True)
            review.write_text(json.dumps({
                "candidates": [{"token": "QA", "occurrences": [{"page": "page"}]}],
            }), encoding="utf-8")
            receipt = resolver.REPO / "temp/inbox-maintenance/session.json"
            receipt.parent.mkdir(parents=True)
            receipt.write_text(json.dumps({
                "status": "agent_required", "errors": [],
                "actions": [
                    {"component": "abbreviations", "next_action": "agent_review"},
                    {"component": "hubs", "next_action": "agent_review"},
                ],
                "components": {
                    "abbreviations": {
                        "status": "agent_required",
                        "review_file": "temp/abbreviation-review/session.json",
                    },
                    "hubs": {"status": "agent_required"},
                },
            }), encoding="utf-8")
            decisions = resolver.REPO / "decisions.json"
            decisions.write_text(json.dumps({"decisions": [{
                "token": "QA", "resolution_kind": "canonical_name",
            }]}), encoding="utf-8")

            resolver.gl.connect = lambda: FakeConnection()
            resolver.apply_decisions = lambda _conn, _decisions, _todo: {
                "status": "completed",
                "applied": [{"token": "QA", "resolution_kind": "canonical_name"}],
                "remaining": [],
            }
            sys.argv = [
                "resolve_abbreviations.py", "--apply-decisions", str(decisions),
                "--todo", str(todo), "--maintenance-receipt",
                "temp/inbox-maintenance/session.json", "--json",
            ]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                resolver.main()

            report = json.loads(stdout.getvalue())
            updated = json.loads(receipt.read_text(encoding="utf-8"))
            assert report["maintenance"]["remaining_tokens"] == 0
            assert todo.read_text(encoding="utf-8") == ""
            assert updated["components"]["abbreviations"]["status"] == "completed"
            assert updated["actions"] == [
                {"component": "hubs", "next_action": "agent_review"},
            ]
    finally:
        resolver.REPO = old_repo
        resolver.gl.connect = old_connect
        resolver.apply_decisions = old_apply_decisions
        sys.argv = old_argv


def test_initial_maintenance_publication_and_historical_reconciliation(historical=True, interrupt_publication=False):
    import inbox_state
    import hub_semantics
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        with patch.object(module, "REPO", root), patch.object(inbox_state, "REPO", root), \
                patch.object(hub_semantics, "REPO", root), \
                patch.object(hub_semantics.gl, "connect", side_effect=AssertionError("no graph writes")):
            route_rel = "temp/hub-route-review/initial.json"
            receipt_rel = "temp/inbox-maintenance/initial.json"
            report_path = root / "cross-domain/ingest-reports/initial.json"
            reviews = []
            files = []
            for transaction_id in ("txn-first", "txn-second"):
                page = f"academic/wiki/papers/{transaction_id}"
                state = {
                    "transaction_id": transaction_id, "status": "completed",
                    "ok": True,
                    "wiki_path": page, "graph_report": {"hub_scope_route": {
                        "decision": "candidates", "reason": "scope_margin_too_small",
                        "candidates": [{"path": "academic/wiki/hubs/selected", "canonical": True}],
                    }},
                    "quality_status": "degraded",
                    "quality_warnings": [{"issue": "graph_navigation_ambiguous"}],
                }
                inbox_state.save(transaction_id, state)
                files.append(dict(state))
                reviews.append({"transaction_id": transaction_id, "wiki_path": page})
            receipt = {
                "status": "agent_required", "receipt_path": receipt_rel, "errors": [],
                "actions": [{"component": "hubs", "route_review_file": route_rel}],
                "components": {"hubs": {
                    "status": "agent_required", "route_review_count": 2,
                    "route_review_file": route_rel, "eligible_count": 0,
                    "split_count": 0, "redistribution_count": 0,
                }},
            }
            module._write_json_atomic(root / route_rel, reviews)
            module._write_json_atomic(root / receipt_rel, receipt)
            report = {"total": 2, **module._report_counts(files), "files": files, "maintenance": receipt}
            module.publish_maintenance_report(report_path, report)
            for transaction_id in ("txn-first", "txn-second"):
                linked = inbox_state.load(transaction_id)["maintenance"]
                assert linked["receipt_path"] == receipt_rel
                assert linked["report_path"] == str(report_path.relative_to(root))
            for transaction_id in ("txn-first", "txn-second"):
                page = f"academic/wiki/papers/{transaction_id}"
                result = {
                    "page": page, "hub": "academic/wiki/hubs/selected",
                    "evidence": f"{page}#研究方向定位",
                }
                if historical and transaction_id == "txn-second":
                    state = inbox_state.load(transaction_id)
                    del state["maintenance"]
                    inbox_state.save(transaction_id, state)
                interrupted = interrupt_publication and transaction_id == "txn-second"
                if interrupted:
                    original_save = inbox_state.save
                    saves = []
                    def interrupt_after_record(saved_transaction, saved_state):
                        if saved_transaction == "txn-second":
                            saves.append(saved_transaction)
                            if len(saves) == 2:
                                raise KeyboardInterrupt("interrupt publication after recording decision")
                        return original_save(saved_transaction, saved_state)
                    with patch.object(inbox_state, "save", side_effect=interrupt_after_record):
                        try:
                            hub_semantics.record_paper_route_correction(transaction_id, result)
                        except KeyboardInterrupt:
                            pass
                        else:
                            raise AssertionError("publication was not interrupted")
                    assert inbox_state.load(transaction_id)["route_corrections"][-1]["hub"] == result["hub"]
                else:
                    hub_semantics.record_paper_route_correction(transaction_id, result)
                current = json.loads(report_path.read_text())
                if interrupted:
                    assert current["maintenance"]["publication"]["status"] == "pending"
                    continue
                expected = "agent_required" if historical or transaction_id == "txn-first" else "completed"
                assert current["maintenance"]["status"] == expected
                if not historical:
                    for member in files:
                        assert inbox_state.load(member["transaction_id"])["maintenance"]["status"] == expected
            summary = module.reconcile_maintenance_report(report_path)
            assert summary["maintenance"]["status"] == "completed"
            repaired = json.loads(report_path.read_text())
            assert repaired["maintenance"]["components"]["hubs"]["route_review_count"] == 0
            assert repaired["degraded"] == 2
            for member in files:
                linked = inbox_state.load(member["transaction_id"])["maintenance"]
                assert linked["status"] == "completed"
                assert linked["components"]["hubs"]["route_review_count"] == 0
                assert linked == inbox_state.load(files[0]["transaction_id"])["maintenance"]
            assert not module._hub_route_reviews(repaired["files"])
            assert all(item["quality_warnings"] for item in repaired["files"])
            assert all(item["resolution"]["status"] == "applied"
                       for item in json.loads((root / route_rel).read_text()))
            module.reconcile_maintenance_report(report_path)
            assert json.loads(report_path.read_text()) == repaired
            receipt = json.loads((root / receipt_rel).read_text())
            receipt["actions"] = [{"component": "abbreviations", "next_action": "review"}]
            receipt["status"] = "agent_required"
            module._write_json_atomic(root / receipt_rel, receipt)
            repaired["maintenance"] = receipt
            module._write_json_atomic(report_path, repaired)
            summary = module.reconcile_maintenance_report(report_path)
            assert summary["maintenance"]["status"] == "agent_required"
            assert json.loads(report_path.read_text())["maintenance"]["actions"] == receipt["actions"]


def test_maintenance_publication_rejects_mismatched_state_before_writing():
    import inbox_state
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        with patch.object(module, "REPO", root), patch.object(inbox_state, "REPO", root):
            receipt_rel = "temp/inbox-maintenance/initial.json"
            module._write_json_atomic(root / receipt_rel, {})
            inbox_state.save("txn-mismatch", {
                "transaction_id": "txn-mismatch", "status": "completed",
                "wiki_path": "academic/wiki/papers/original",
            })
            report_path = root / "cross-domain/ingest-reports/initial.json"
            report = {
                "files": [{"transaction_id": "txn-mismatch", "status": "completed",
                           "wiki_path": "academic/wiki/papers/different"}],
                "maintenance": {"receipt_path": receipt_rel},
            }
            assert module.publish_maintenance_report(report_path, report) is False
            assert "page mismatch" in report["maintenance"]["errors"][0]
            assert report["maintenance"]["retryable"] is False
            assert not report_path.exists()
            assert "maintenance" not in inbox_state.load("txn-mismatch")
            try:
                module.reconcile_maintenance_report(root / "outside.json")
            except ValueError:
                pass
            else:
                raise AssertionError("unmanaged report accepted")


def test_paper_batch_keeps_preclassified_fingerprint_results():
    import inspect
    source = inspect.getsource(module.main)
    assert "results.extend(batch_results)" in source


def test_maintenance_publication_recovers_write_failures():
    import inbox_state
    scenarios = ("initial_report", "first_state", "second_state", "final_report", "interruption")
    for scenario in scenarios:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch.object(module, "REPO", root), patch.object(inbox_state, "REPO", root), \
                    patch.object(module, "run_post_ingest_maintenance", side_effect=AssertionError("no global maintenance")):
                receipt_rel = "temp/inbox-maintenance/batch.json"
                report_path = root / "cross-domain/ingest-reports/batch.json"
                receipt = {"status": "completed", "receipt_path": receipt_rel, "actions": [], "errors": [], "components": {}}
                module._write_json_atomic(root / receipt_rel, receipt)
                files = []
                for transaction_id in ("txn-first", "txn-second"):
                    state = {"status": "completed", "ok": True, "transaction_id": transaction_id,
                             "wiki_path": f"academic/wiki/papers/{transaction_id}",
                             "quality_status": "degraded", "quality_warnings": [{"issue": "independent"}]}
                    inbox_state.save(transaction_id, state)
                    files.append(dict(state))
                report = {"total": 2, **module._report_counts(files), "files": files, "maintenance": receipt}
                original_save = inbox_state.save
                original_write = module._write_json_atomic
                writes = []
                def injected_write(path, value):
                    writes.append(value["maintenance"]["publication"]["status"])
                    if scenario == "initial_report" or scenario == "final_report" and len(writes) >= 2:
                        raise OSError("simulated report write failure")
                    return original_write(path, value)
                def injected_save(transaction_id, state):
                    if scenario == "interruption" and transaction_id == "txn-second":
                        raise KeyboardInterrupt("simulated process interruption")
                    if (scenario == "first_state" and transaction_id == "txn-first"
                            or scenario == "second_state" and transaction_id == "txn-second"):
                        raise OSError("simulated transaction write failure")
                    return original_save(transaction_id, state)
                with patch.object(module, "_write_json_atomic", side_effect=injected_write), \
                        patch.object(inbox_state, "save", side_effect=injected_save):
                    if scenario == "interruption":
                        try:
                            module.publish_maintenance_report(report_path, report)
                        except KeyboardInterrupt:
                            pass
                        else:
                            raise AssertionError("interruption was swallowed")
                    else:
                        assert module.publish_maintenance_report(report_path, report) is False
                        compact = module._compact_summary(report, report_path)
                        assert compact["file_status"] == "completed" and compact["failed"] == 0
                        assert compact["maintenance"]["status"] == "error"
                        assert compact["maintenance"]["retryable"] is True
                if scenario == "initial_report":
                    assert not report_path.exists()
                    assert report["maintenance"]["publication"]["report_persisted"] is False
                    assert all("maintenance" not in inbox_state.load(item["transaction_id"]) for item in files)
                    assert module.publish_maintenance_report(report_path, report)
                else:
                    persisted = json.loads(report_path.read_text())
                    expected = "pending" if scenario in {"final_report", "interruption"} else "error"
                    assert persisted["maintenance"]["publication"]["status"] == expected
                    compact = module.reconcile_maintenance_report(report_path)
                    assert compact["maintenance"]["status"] == "completed"
                assert json.loads((root / receipt_rel).read_text()) == receipt
                for item in files:
                    state = inbox_state.load(item["transaction_id"])
                    assert state["status"] == "completed"
                    assert state["maintenance"]["status"] == "completed"
                    assert state["quality_warnings"] == [{"issue": "independent"}]
                repaired = json.loads(report_path.read_text())
                module.reconcile_maintenance_report(report_path)
                assert json.loads(report_path.read_text()) == repaired


def main():
    test_extract_last_json_ignores_domain_status()
    test_managed_external_file_staging_and_inbox_boundary()
    test_managed_external_file_rejects_symlink_target()
    test_document_dispatch_marks_inbox_entrypoint()
    test_agent_resume_uses_same_document_transaction()
    test_resume_ocr_parameters_only_apply_to_image_document_transaction()
    test_extract_last_json_preserves_top_level_batch_envelope()
    test_scan_inbox_includes_only_nonempty_facts_pending()
    test_facts_pending_run_returns_agent_task_without_raw_write()
    test_agent_run_directly_dispatches_without_importing_dsh()
    test_agent_run_directly_dispatches_without_importing_dsh(fail_publication=True)
    test_api_run_uses_dsh_loop_without_direct_dispatch()
    test_agent_low_confidence_classification_is_one_batch_task()
    test_exact_duplicate_cleanup_reverifies_and_writes_receipt()
    test_exact_duplicate_cleanup_refuses_sha_mismatch()
    test_report_counts_separate_duplicates_agent_waits_and_failures()
    test_duplicate_only_report_has_duplicate_terminal_status()
    test_dsi_tool_routes_file_types()
    test_academic_document_classification_gate()
    test_academic_conference_classification_uses_first_h1()
    test_explicit_document_type_forces_complete_agent_dispatch()
    test_abbreviation_todo_binds_field_and_filters_page_identity()
    test_uncertain_scores_request_api_review_without_changing_program_type()
    test_docx_meeting_transcript_keeps_document_extractor_and_explicit_source_kind()
    test_api_classification_review_resolves_uncertain_program_decision()
    test_agent_classification_evidence_allows_pdf_line_wrapping()
    test_plan_ingest_order_versions_last()
    test_map_paper_batch_results()
    test_map_paper_batch_preserves_bibliographic_review_contract()
    test_auto_resolve_abbr_key_contract()
    test_compact_summary_excludes_graph_diagnostics_and_returns_report_path()
    test_auto_create_hubs_writes_split_handoff()
    test_hub_timeout_is_deferred_and_retryable()
    test_zero_success_skips_global_post_ingest_scans()
    test_maintenance_error_does_not_override_completed_file_status()
    test_maintenance_rejects_incomplete_completed_result_envelope()
    test_abbreviation_decisions_require_exact_pending_tokens_and_atomic_todo()
    test_abbreviation_decisions_close_only_matching_maintenance_action()
    test_abbreviation_decisions_cli_closes_linked_maintenance_receipt()
    test_initial_maintenance_publication_and_historical_reconciliation()
    test_initial_maintenance_publication_and_historical_reconciliation(historical=False)
    test_initial_maintenance_publication_and_historical_reconciliation(historical=False, interrupt_publication=True)
    test_maintenance_publication_rejects_mismatched_state_before_writing()
    test_maintenance_publication_recovers_write_failures()
    test_paper_batch_keeps_preclassified_fingerprint_results()
    print("ingest_inbox dispatch regression: PASS")


if __name__ == "__main__":
    main()
