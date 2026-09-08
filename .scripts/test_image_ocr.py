#!/usr/bin/env python3
"""Offline regression for OCR transport, integrity, and image ingest handoff."""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import agent_task
import image_ocr as ocr
import inbox_plan
import ingest_document as document
import ingest_pipeline


class ImageOCRTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "inbox" / "image.PNG"
        self.source.parent.mkdir()
        Image.new("RGB", (80, 40), "white").save(self.source)
        self.config = {"base": "https://example.invalid/v1", "key": "secret-key",
                       "model": "GLM-4.6V", "fallback_model": ""}

    def receipt(self):
        info, _ = ocr.read_image(self.source)
        receipt = ocr.make_receipt(info, "# 表单\n\n| 金额 | 日期 |\n| --- | --- |\n| 15,000.00 | |",
                                   backend="agent")
        receipt["review"] = {
            "schema": "image-ocr-review-v1", "source_sha256": info["sha256"],
            "text_sha256": receipt["text_sha256"], "reviewer_kind": "agent", "reviewer": "test-agent",
            "reviewed_at": "2026-09-08T00:00:00+08:00", "risk": "critical",
            "checks": [{"field": "金额", "locator": "L5", "critical": True, "status": "verified", "note": "15000"},
                       {"field": "日期", "locator": "L5", "critical": False, "status": "unresolved", "note": "空白"}],
            "limitations": ["空白日期不补填"],
        }
        return ocr.validate_receipt(receipt, self.source)

    def state(self):
        return {"transaction_id": "ocr-test", "status": "preprocess", "source": "inbox/image.PNG",
                "source_filename": "image.PNG", "subproject": "admin",
                "extract_dir": "temp/inbox-extract/ocr-test", "source_kind": "ordinary"}

    def test_image_classification_is_document_not_meeting(self):
        for suffix in ocr.IMAGE_SUFFIXES:
            result = inbox_plan.classify(Path("会议" + suffix.upper()))
            self.assertEqual(result["kind"], "document")
            self.assertEqual(result["source_kind"], "ordinary")
            self.assertTrue(result["requires_review"])
            self.assertIsNone(result["subproject"])

    def test_review_binding_identity_and_field_locators(self):
        receipt = self.receipt()
        for field, value in (("source_sha256", "stale"), ("text_sha256", "stale"),
                             ("reviewer_kind", "model-human"), ("reviewed_at", "yesterday")):
            with self.subTest(field=field), self.assertRaises(ocr.ImageOCRError):
                ocr.validate_receipt({**receipt, "review": {**receipt["review"], field: value}}, self.source)
        review = {**receipt["review"], "checks": [{**receipt["review"]["checks"][0], "locator": "L999"}]}
        with self.assertRaises(ocr.ImageOCRError):
            ocr.validate_receipt({**receipt, "review": review}, self.source)

    def test_unassessed_or_unresolved_critical_review_blocks_ingestion(self):
        receipt = self.receipt()
        unreviewed = {key: value for key, value in receipt.items() if key != "review"}
        unresolved = {**receipt, "review": {**receipt["review"], "checks": [
            {**receipt["review"]["checks"][0], "status": "unresolved"}]}}
        no_critical = {**receipt, "review": {**receipt["review"], "checks": []}}
        with patch.object(document, "REPO", self.root), patch.object(document, "ingest_mode", return_value="api"):
            for candidate in (unreviewed, unresolved, no_critical):
                output = self.root / "review-ocr.json"
                ocr.save_receipt(output, candidate, self.source)
                state = {**self.state(), "ocr_result": str(output)}
                success, message = document.step_preprocess(state)
                self.assertFalse(success, message)
                self.assertNotIn("ocr", state)
                self.assertTrue(ocr.review_blockers(ocr.load_receipt(output, self.source)))

    def test_review_distinguishes_agent_human_and_retains_partial_warnings(self):
        receipt = self.receipt()
        self.assertEqual(receipt["review_status"], "partial")
        self.assertFalse(ocr.review_blockers(receipt))
        self.assertTrue(receipt["review_required"])
        for kind in ("agent", "human"):
            review = {**receipt["review"], "reviewer_kind": kind, "checks": [receipt["review"]["checks"][0]]}
            validated = ocr.validate_receipt({**receipt, "review": review}, self.source)
            self.assertEqual(validated["review_status"], kind + "-reviewed")
            self.assertFalse(validated["review_required"])
            self.assertEqual(validated["review"]["limitations"], receipt["review"]["limitations"])

    def test_image_wiki_confidence_and_review_status_are_program_owned(self):
        import yaml
        source = "---\ntitle: 表单\ntype: reference\nstatus: draft\nconfidence: high\n---\n\n## Content\n\n表单"
        receipt = self.receipt()
        result, _ = document.normalize_document_wiki(
            source, correct_sources="admin/raw/references/image.md", source_date="",
            doc_text=receipt["markdown"], created_at="2026-09-08", source_filename="image.PNG", ocr=receipt,
        )
        metadata = yaml.safe_load(result.split("---")[1])
        self.assertEqual(metadata["source_type"], "ocr")
        self.assertEqual(metadata["confidence"], "medium")
        self.assertEqual(metadata["ocr_review_status"], "partial")
        self.assertTrue(metadata["ocr_review_required"])
        self.assertIsNone(metadata["date"])
        self.assertEqual(document.confidence_for("ordinary", "image.jpg"), "low")
        self.assertEqual(document.source_type_for("ordinary", "chat.md"), "discussion")
        self.assertEqual(document.confidence_for("ordinary", "chat.md"), "medium")

    def test_attach_review_cli_is_local_and_keeps_original_receipt(self):
        original = self.root / "original.json"
        output = self.root / "reviewed.json"
        review_path = self.root / "review.json"
        receipt = self.receipt()
        ocr.save_receipt(original, {key: value for key, value in receipt.items() if key != "review"}, self.source)
        before = original.read_bytes()
        review_path.write_text(json.dumps(receipt["review"]), encoding="utf-8")
        with patch.object(ocr, "call_api") as api, patch("sys.stdout", io.StringIO()), patch("sys.argv", [
            "image_ocr.py", str(self.source), "--check", str(original), "--review-file", str(review_path),
            "--output", str(output),
        ]):
            self.assertEqual(ocr.main(), 0)
            api.assert_not_called()
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(ocr.load_receipt(output, self.source)["review"], receipt["review"])

    def test_all_image_formats_have_binary_locators_and_companion_dedup(self):
        import source_locator
        import source_fingerprints
        for suffix in ocr.IMAGE_SUFFIXES:
            source = self.root / ("test" + suffix)
            source.write_bytes(self.source.read_bytes())
            companion = source.with_suffix(".md")
            companion.write_text("OCR text", encoding="utf-8")
            self.assertFalse(source_locator.valid_locator("L1", source))
            self.assertTrue(source_fingerprints._is_source_artifact(source))
            self.assertFalse(source_fingerprints._is_source_artifact(companion))
            source.unlink()
            companion.unlink()

    def test_no_consent_means_no_api(self):
        with patch.object(ocr, "call_api") as call:
            with self.assertRaisesRegex(ocr.ImageOCRError, "allow_remote"):
                ocr.recognize_image(self.source, config=self.config)
            call.assert_not_called()

    def test_receipt_roundtrip_rejects_tampering_and_other_source(self):
        receipt = self.receipt()
        output = self.root / "output.json"
        ocr.save_receipt(output, receipt, self.source)
        self.assertEqual(ocr.load_receipt(output, self.source)["markdown"], receipt["markdown"])
        receipt["markdown"] += "changed"
        with self.assertRaisesRegex(ocr.ImageOCRError, "文本哈希"):
            ocr.validate_receipt(receipt, self.source)
        Image.new("RGB", (80, 40), "black").save(self.source)
        with self.assertRaisesRegex(ocr.ImageOCRError, "SHA-256"):
            ocr.load_receipt(output, self.source)

    def test_raw_wiki_source_and_symlink_outputs_rejected(self):
        targets = [self.root / "raw" / "ocr.json", self.root / "wiki" / "ocr.json", self.source]
        raw = self.root / "raw"
        raw.mkdir()
        link = self.root / "alias"
        link.symlink_to(raw, target_is_directory=True)
        targets.append(link / "ocr.json")
        for target in targets:
            with self.assertRaises(ocr.ImageOCRError):
                ocr.save_receipt(target, self.receipt(), self.source)

    def test_corrupt_multiframe_and_large_images_rejected(self):
        self.source.write_bytes(b"not an image")
        with self.assertRaises(ocr.ImageOCRError):
            ocr.read_image(self.source)
        target = self.root / "multi.tiff"
        Image.new("RGB", (20, 20)).save(target, save_all=True,
                                        append_images=[Image.new("RGB", (20, 20))])
        with self.assertRaisesRegex(ocr.ImageOCRError, "多帧"):
            ocr.read_image(target)
        Image.new("RGB", (80, 40)).save(self.source)
        with patch.object(ocr, "MAX_PIXELS", 100):
            with self.assertRaises(ocr.ImageOCRError):
                ocr.read_image(self.source)

    def test_exif_orientation_normalized_without_modifying_original(self):
        target = self.root / "rotated.jpg"
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (80, 40)).save(target, exif=exif)
        original = target.read_bytes()
        info, png = ocr.read_image(target)
        self.assertEqual((info["width"], info["height"]), (40, 80))
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertEqual(target.read_bytes(), original)

    def test_transport_endpoint_and_strict_response_validation(self):
        for base in ("https://example.invalid", "https://example.invalid/v1/"):
            for finish, text, valid in (("stop", "008", True), ("length", "half", False),
                                        (None, "text", False), ("stop", "", False),
                                        ("stop", "[无可识别文字]", False), ("stop", [], False)):
                response = io.BytesIO(json.dumps({"choices": [{"finish_reason": finish,
                                          "message": {"content": text}}]}).encode())
                with patch.object(ocr.urllib.request, "urlopen", return_value=response) as call:
                    config = {**self.config, "base": base}
                    if valid:
                        receipt = ocr.recognize_image(self.source, allow_remote=True, config=config)
                        self.assertEqual(receipt["markdown"], "008\n")
                    else:
                        with self.assertRaises(ocr.ImageOCRError):
                            ocr.recognize_image(self.source, allow_remote=True, config=config)
                    self.assertEqual(call.call_args.args[0].full_url,
                                     "https://example.invalid/v1/chat/completions")

    def test_provider_error_is_sanitized_and_fallback_bounded(self):
        error = urllib.error.HTTPError("https://secret.invalid", 500, "secret-key", {},
                                       io.BytesIO(b"secret-key data:image"))
        with patch.object(ocr.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(ocr.ImageOCRError) as raised:
                ocr.recognize_image(self.source, allow_remote=True, config=self.config)
        self.assertNotIn("secret", str(raised.exception))
        config = {**self.config, "fallback_model": "GLM-4.5V"}
        with patch.object(ocr, "call_api", side_effect=[ocr.ImageOCRError("failed"), "text"]) as call:
            receipt = ocr.recognize_image(self.source, allow_remote=True, config=config)
            self.assertEqual(receipt["model"], "GLM-4.5V")
            self.assertEqual(call.call_count, 2)
        with patch.object(ocr, "call_api", side_effect=ocr.ImageOCRError("failed")) as call:
            with self.assertRaises(ocr.ImageOCRError):
                ocr.recognize_image(self.source, allow_remote=True, config=config, model="chosen")
            self.assertEqual(call.call_count, 1)

    def test_config_inherits_endpoint_and_env_override(self):
        import llm_structured
        with patch.object(llm_structured, "load_env", return_value={
                "LLM_API_BASE": "base", "LLM_API_KEY": "key", "LLM_MODEL": "text-model"}), \
                patch.dict(os.environ, {"IMAGE_OCR_MODEL": "chosen"}):
            config = ocr.load_config()
        self.assertEqual(config["base"], "base")
        self.assertEqual(config["key"], "key")
        self.assertEqual(config["model"], "chosen")

    def test_reasoning_budget_and_fallback_settings_are_independent(self):
        config = {**self.config, "model": "GLM-5.3-Flash", "fallback_model": "GLM-4.6V",
                  "reasoning_effort": "low", "fallback_reasoning_effort": "default", "max_tokens": "4000"}
        with patch.object(ocr, "call_api", side_effect=[ocr.ImageOCRError("failed"), "text"]) as call:
            receipt = ocr.recognize_image(self.source, allow_remote=True, config=config)
        self.assertEqual(call.call_args_list[0].kwargs["reasoning_effort"], "low")
        self.assertEqual(call.call_args_list[1].kwargs["reasoning_effort"], "default")
        self.assertEqual(call.call_args_list[1].kwargs["max_tokens"], 4000)
        self.assertEqual(receipt["request_settings"], {"reasoning_effort": "default", "max_tokens": 4000})
        with patch.object(ocr, "call_api", return_value="text") as call:
            ocr.recognize_image(self.source, allow_remote=True, config=config, reasoning_effort="high", max_tokens=2000)
            self.assertEqual(call.call_args.kwargs["reasoning_effort"], "high")
            self.assertEqual(call.call_args.kwargs["max_tokens"], 2000)

    def test_reasoning_field_emission_and_invalid_settings(self):
        for effort in ("low", "high", "default"):
            response = {"choices": [{"finish_reason": "stop", "message": {"content": "008"}}]}
            with patch.object(ocr.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as call:
                receipt = ocr.recognize_image(self.source, allow_remote=True, config=self.config, reasoning_effort=effort)
                payload = json.loads(call.call_args.args[0].data)
                self.assertEqual(payload.get("reasoning_effort"), None if effort == "default" else effort)
                self.assertEqual(receipt["request_settings"]["reasoning_effort"], effort)
        for options in ({"reasoning_effort": "medium"}, {"max_tokens": 0}, {"max_tokens": True}):
            with patch.object(ocr, "call_api") as call:
                with self.assertRaises(ocr.ImageOCRError):
                    ocr.recognize_image(self.source, allow_remote=True, config=self.config, **options)
                call.assert_not_called()

    def test_agent_preprocess_pause_resume_and_manifest(self):
        state = self.state()
        with patch.object(document, "REPO", self.root), patch.object(document, "ingest_mode", return_value="agent"), \
                patch.object(ocr, "call_api") as call:
            self.assertFalse(document.step_preprocess(state)[0])
            self.assertTrue(agent_task.is_prepared(state))
            task = state["agent_task"]
            self.assertEqual(task["kind"], "image_ocr")
            output = self.root / task["outputs"][0]["path"]
            output.write_text(self.receipt()["markdown"], encoding="utf-8")
            state["status"] = "preprocess"
            self.assertFalse(document.step_preprocess(state)[0])
            self.assertEqual(state["agent_task"]["kind"], "image_review")
            review_path = self.root / state["agent_task"]["outputs"][0]["path"]
            review_path.write_text(json.dumps({**self.receipt()["review"], "reviewer_kind": "human"}), encoding="utf-8")
            self.assertFalse(document.step_preprocess(state)[0])
            self.assertTrue(agent_task.is_prepared(state))
            review_path.write_text(json.dumps(self.receipt()["review"]), encoding="utf-8")
            self.assertTrue(document.step_preprocess(state)[0])
            self.assertEqual(state["raw_locator_kind"], "companion")
            self.assertIn("image.PNG", document._manifest_raw_files(state))
            self.assertIn(state["locator_source_filename"], document._manifest_raw_files(state))
            self.assertNotIn("image-ocr.json", document._manifest_raw_files(state))
            self.assertNotIn("pre_handoff_status", state)
            call.assert_not_called()

    def test_pipeline_preserves_prepared_preprocess_and_resumes_it(self):
        state = self.state()
        reached = []

        def stop_at_wiki(current):
            reached.append(current["status"])
            agent_task.prepare(current, kind="test_wiki", transaction_id="ocr-test",
                               inputs=[{"name": "text", "path": "temp/doc.md"}],
                               outputs=[{"name": "wiki", "path": "temp/wiki.md"}],
                               protocol={"name": "test"})
            return False, "prepared"

        spec = {**document.DOCUMENT_SPEC, "steps": {
            **document.DOCUMENT_SPEC["steps"], "write_wiki": stop_at_wiki}}
        with patch.object(document, "REPO", self.root), patch.object(ingest_pipeline, "REPO", self.root), \
                patch.object(document, "ingest_mode", return_value="agent"), \
                patch.object(ingest_pipeline, "_save"):
            state = ingest_pipeline.run_pipeline(state, spec, lambda *args, **kwargs: None)
            self.assertTrue(agent_task.is_prepared(state))
            state = ingest_pipeline.run_pipeline(state, spec, lambda *args, **kwargs: None)
            self.assertFalse(reached)
            output = self.root / state["agent_task"]["outputs"][0]["path"]
            output.write_text(self.receipt()["markdown"], encoding="utf-8")
            state = ingest_pipeline.run_pipeline(state, spec, lambda *args, **kwargs: None)
            self.assertFalse(reached)
            self.assertEqual(state["agent_task"]["kind"], "image_review")
            review_path = self.root / state["agent_task"]["outputs"][0]["path"]
            review_path.write_text(json.dumps(self.receipt()["review"]), encoding="utf-8")
            state = ingest_pipeline.run_pipeline(state, spec, lambda *args, **kwargs: None)
            self.assertEqual(reached, ["write_wiki"])
            self.assertEqual(state["agent_task"]["kind"], "test_wiki")

    def test_explicit_result_reused_without_remote_call(self):
        output = self.root / "ocr.json"
        ocr.save_receipt(output, self.receipt(), self.source)
        state = {**self.state(), "ocr_result": str(output)}
        with patch.object(document, "REPO", self.root), patch.object(ocr, "recognize_image") as recognize:
            self.assertTrue(document.step_preprocess(state)[0])
            recognize.assert_not_called()
            self.assertEqual(state["ocr"]["backend"], "agent")

    def test_explicit_remote_ocr_does_not_change_semantic_backend(self):
        state = {**self.state(), "allow_remote_ocr": True}
        receipt = {**self.receipt(), "backend": "api", "model": "GLM-4.6V"}
        with patch.object(document, "REPO", self.root), patch.object(document, "ingest_mode", return_value="agent"), \
                patch.object(ocr, "recognize_image", return_value=receipt) as recognize, \
                patch.object(document, "call_text") as semantics:
            self.assertTrue(document.step_preprocess(state)[0])
            recognize.assert_called_once_with(self.source, allow_remote=True)
            semantics.assert_not_called()
            self.assertEqual(document.ingest_mode(), "agent")
            self.assertEqual(state["quality_warnings"][0]["issue"], "ocr_visual_review_required")

    def test_explicit_receipt_can_resume_waiting_agent_task(self):
        state = self.state()
        with patch.object(document, "REPO", self.root), patch.object(document, "ingest_mode", return_value="agent"):
            document.step_preprocess(state)
            output = self.root / "ocr.json"
            ocr.save_receipt(output, self.receipt(), self.source)
            with patch.object(document.inbox_state, "load", return_value=state), \
                    patch.object(document.inbox_state, "save"), \
                    patch.object(document.ic, "run_resume_post_maintenance", return_value=None), \
                    patch.object(document, "run_pipeline", side_effect=lambda current: current) as pipeline, \
                    patch("sys.argv", ["ingest_document.py", "--resume", "ocr-test", "--verbose",
                                       "--ocr-result", str(output)]), patch("sys.stdout", io.StringIO()):
                document.main()
                self.assertEqual(pipeline.call_args.args[0]["status"], "preprocess")
            self.assertTrue(document.step_preprocess(state)[0])
            self.assertNotIn("_awaiting_image_ocr", state)

    def test_dispatch_and_dsh_preserve_explicit_ocr_parameters(self):
        import ingest_inbox
        sys.path.insert(0, str(ocr.REPO))
        self.addCleanup(sys.path.pop, 0)
        from dsh import ingest_tools
        options = {"ocr_result": "temp/ocr.json", "allow_remote_ocr": True}
        command = ingest_inbox.dispatch_command("document", "inbox/image.png", "admin", **options)
        self.assertIn("--ocr-result", command)
        self.assertIn("--allow-remote-ocr", command)
        name, arguments = ingest_inbox.dsi_tool("document", "inbox/image.png", "admin", **options)
        tool = next(item for item in ingest_tools.build_ingest_tools() if item.name == name)
        with patch.object(ingest_tools, "_ingest_call", return_value="ok") as call:
            tool.execute_fn(arguments)
            self.assertIn("--allow-remote-ocr", call.call_args.args[0])
            self.assertIn("temp/ocr.json", call.call_args.args[0])
            tool.execute_fn({"file": "inbox/image.png", "subproject": "admin"})
            self.assertNotIn("--allow-remote-ocr", call.call_args.args[0])

    def test_finalizer_keeps_original_and_companion_in_managed_transaction(self):
        state = self.state()
        output = self.root / "ocr.json"
        receipt = self.receipt()
        ocr.save_receipt(output, receipt, self.source)
        state.update({"ocr_result": str(output), "admin_id": "ocr-form",
                      "raw_dir": "admin/raw/references/ocr-form", "wiki_path": "admin/wiki/references/ocr-form"})
        original_run = document.ic.run

        def isolated_finalizer(command, repo):
            actual = [command[0], str(Path(document.__file__).with_name("inbox_finalize.py")),
                      *command[2:], "--project-root", str(repo)]
            return original_run(actual, repo)

        with patch.object(document, "REPO", self.root), \
                patch.object(document.ic, "run", side_effect=isolated_finalizer):
            self.assertTrue(document.step_preprocess(state)[0])
            (self.root / state["extract_dir"] / "wiki.md").write_text("# OCR test\n", encoding="utf-8")
            success, message = document.step_finalize(state)
            self.assertTrue(success, message)
        raw = self.root / state["raw_dir"]
        self.assertEqual((raw / self.source.name).read_bytes(), self.source.read_bytes())
        self.assertEqual((raw / state["locator_source_filename"]).read_text(), self.receipt()["markdown"])
        self.assertFalse((raw / "image-ocr.json").exists())
        self.assertTrue(self.source.is_file())
        self.assertTrue(Path(state["receipt"]).is_file())
        context_path = raw / state["source_context_filename"]
        context = json.loads(context_path.read_text(encoding="utf-8"))
        self.assertEqual(context["schema"], "document-source-context-v1")
        self.assertEqual(context["ocr"]["original"], self.source.name)
        self.assertEqual(context["ocr"]["companion"], state["locator_source_filename"])
        self.assertEqual(context["ocr"]["source_sha256"], ocr.sha256((raw / self.source.name).read_bytes()))
        self.assertEqual(context["ocr"]["text_sha256"], ocr.sha256((raw / state["locator_source_filename"]).read_bytes()))
        self.assertEqual(context["ocr"]["created"], receipt["created"])
        self.assertEqual(context["ocr"]["prompt_version"], receipt["prompt_version"])
        self.assertTrue(context["ocr"]["review_required"])
        self.assertNotIn("temp/", context_path.read_text(encoding="utf-8"))
        import source_fingerprints
        self.assertFalse(source_fingerprints._is_source_artifact(context_path))
        log = document.FINALIZE_TAIL_CONFIG["build_log_entry"]({
            "today": "2026-09-07", "doc_id": "ocr-form", "page_name": "ocr-form",
            "title": "表单", "edges": 0, "report": {}, "state": state,
        })
        self.assertIn(state["raw_dir"] + "/" + context_path.name, log)
        self.assertIn("生成时间不代表业务日期", log)

    def test_provenance_keeps_api_identity_without_changing_transcription(self):
        receipt = {**self.receipt(), "backend": "api", "model": "GLM-5.3-Flash"}
        output = self.root / "ocr.json"
        ocr.save_receipt(output, receipt, self.source)
        state = {**self.state(), "ocr_result": str(output)}
        with patch.object(document, "REPO", self.root):
            self.assertTrue(document.step_preprocess(state)[0])
        extract = self.root / state["extract_dir"]
        context = json.loads((extract / state["source_context_filename"]).read_text())
        self.assertEqual(context["ocr"]["backend"], "api")
        self.assertEqual(context["ocr"]["model"], "GLM-5.3-Flash")
        self.assertEqual((extract / "doc.md").read_text(), receipt["markdown"])
        self.assertEqual((extract / state["locator_source_filename"]).read_text(), receipt["markdown"])
        self.assertEqual(state["ocr"]["created"], receipt["created"])

    def test_finalizer_rejects_missing_or_modified_provenance_and_pair_names(self):
        output = self.root / "ocr.json"
        ocr.save_receipt(output, self.receipt(), self.source)
        state = {**self.state(), "ocr_result": str(output)}
        with patch.object(document, "REPO", self.root), patch.object(document.ic, "step_finalize") as finalize:
            self.assertTrue(document.step_preprocess(state)[0])
            context_path = self.root / state["extract_dir"] / state["source_context_filename"]
            original = context_path.read_text()
            context_path.unlink()
            self.assertFalse(document.step_finalize(state)[0])
            for content in ("{", "{}", original.replace('"review_required": true', '"review_required": false'),
                            original.replace('"companion": "image.md"', '"companion": "other.md"')):
                with self.subTest(content=content):
                    context_path.write_text(content, encoding="utf-8")
                    self.assertFalse(document.step_finalize(state)[0])
            context_path.write_text(original, encoding="utf-8")
            for field in ("source_context_filename", "locator_source_filename", "source_filename"):
                with self.subTest(field=field):
                    changed = {**state, field: "other.md"}
                    self.assertFalse(document.step_finalize(changed)[0])
            finalize.assert_not_called()

    def test_legacy_receipt_does_not_invent_ocr_date(self):
        receipt = self.receipt()
        receipt.pop("created")
        receipt = ocr.validate_receipt(receipt, self.source)
        with patch.object(document, "REPO", self.root):
            context = document._document_source_context(self.source, receipt)
        self.assertIsNone(context["ocr"]["created"])

    def test_finalizer_rejects_missing_companion_changed_text_or_changed_image(self):
        state = self.state()
        output = self.root / "ocr.json"
        receipt = self.receipt()
        ocr.save_receipt(output, receipt, self.source)
        state["ocr_result"] = str(output)
        with patch.object(document, "REPO", self.root), patch.object(document.ic, "step_finalize") as finalize:
            self.assertTrue(document.step_preprocess(state)[0])
            companion = self.root / state["extract_dir"] / state["locator_source_filename"]
            companion.unlink()
            self.assertFalse(document.step_finalize(state)[0])
            companion.write_text("changed", encoding="utf-8")
            self.assertFalse(document.step_finalize(state)[0])
            companion.write_text(receipt["markdown"], encoding="utf-8")
            Image.new("RGB", (80, 40), "black").save(self.source)
            self.assertFalse(document.step_finalize(state)[0])
            finalize.assert_not_called()

    def test_api_ingest_requires_separate_consent(self):
        with patch.object(document, "REPO", self.root), patch.object(document, "ingest_mode", return_value="api"), \
                patch.object(ocr, "recognize_image") as recognize:
            success, error = document.step_preprocess(self.state())
            self.assertFalse(success)
            self.assertIn("--allow-remote-ocr", error)
            recognize.assert_not_called()

    def test_changed_source_and_empty_agent_text_stay_prepared(self):
        state = self.state()
        with patch.object(document, "REPO", self.root), patch.object(document, "ingest_mode", return_value="agent"):
            document.step_preprocess(state)
            output = self.root / state["agent_task"]["outputs"][0]["path"]
            output.write_text("  ", encoding="utf-8")
            state["status"] = "preprocess"
            self.assertFalse(document.step_preprocess(state)[0])
            self.assertTrue(agent_task.is_prepared(state))
            output.write_text("old image", encoding="utf-8")
            Image.new("RGB", (80, 40), "black").save(self.source)
            state["status"] = "preprocess"
            self.assertFalse(document.step_preprocess(state)[0])
            self.assertTrue(agent_task.is_prepared(state))


if __name__ == "__main__":
    unittest.main()
