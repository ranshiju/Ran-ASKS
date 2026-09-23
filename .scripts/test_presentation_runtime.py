#!/usr/bin/env python3
"""Phase-0 contract/probe regression; --render requires the real local toolchain."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import presentation_runtime as probe

REPO = Path(__file__).resolve().parent.parent


def route(*args):
    return subprocess.run([sys.executable, str(REPO / ".scripts/route.py"), *args],
                          cwd=REPO, capture_output=True, text=True)


class CapabilityTests(unittest.TestCase):
    def test_create_profile_is_explicit_and_bounded(self):
        result = route("--capability", "presentation", "--capability-profile", "create")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("profile: create", result.stderr)
        for text in ("当前阶段：1B", "presentation_runtime.py doctor", "presentation_runtime.py smoke",
                     "当前不提供 API adapter", "私有来源的敏感性", "presentation_state.py", "整套 PPTX 最终组装"):
            self.assertIn(text, result.stdout)
        self.assertNotIn("## 持久状态与权威", result.stdout)
        self.assertNotIn("## 最小协议与批准", result.stdout)
        self.assertNotIn("PresentationPlanner", result.stdout)
        listing = route("--list").stdout
        tasks, capabilities = listing.split("按需能力:")
        self.assertNotIn("  presentation:", tasks)
        self.assertIn(
            "presentation: artifact.presentation profiles=citation-redact,create,evidence-check,form-check",
            capabilities,
        )

    def test_unknown_profile_and_mixed_state_are_rejected(self):
        for args in (("--capability", "presentation"),
                     ("--capability", "presentation", "--capability-profile", "template"),
                     ("--task", "research", "--capability", "presentation", "--capability-profile", "create")):
            self.assertNotEqual(route(*args).returncode, 0)

    def test_plain_write_is_still_available(self):
        result = route("--capability", "write")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("起草工作流", result.stdout)
        self.assertIn("--capability presentation --capability-profile create", result.stdout)


class RuntimeTests(unittest.TestCase):
    def test_missing_dependency_is_not_ready(self):
        with patch.object(probe, "_load_pptx", side_effect=probe.RuntimeProbeError("missing fixture")):
            result = probe.doctor()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["dependencies"]["pptx"]["status"], "unavailable")
        self.assertFalse(result["authoring_available"])
        self.assertEqual(result["remote_calls"], 0)

    def test_cli_exit_code_and_dependency_failure_create_no_run(self):
        with patch.object(probe, "doctor", return_value={"status": "unavailable"}), \
             patch.object(probe, "_new_run") as new_run:
            result = probe.smoke()
            self.assertEqual(result["status"], "failed")
            new_run.assert_not_called()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(probe.main(["smoke"]), 1)
            self.assertEqual(json.loads(output.getvalue())["stage"], "doctor")

    def test_symlink_cache_and_parent_are_rejected(self):
        for component in ("temp", "temp/presentation-runtime"):
            with self.subTest(component=component), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp).resolve()
                repo, outside = base / "repo", base / "outside"
                repo.mkdir(); outside.mkdir()
                link = repo / component
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(outside, target_is_directory=True)
                with patch.object(probe, "REPO", repo):
                    with self.assertRaises(probe.RuntimeProbeError):
                        probe._new_run()
                self.assertEqual(list(outside.iterdir()), [])

    def test_failed_generation_has_receipt_and_keeps_old_runs(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(probe, "REPO", Path(tmp).resolve()), \
             patch.object(probe, "doctor", return_value={"status": "ready"}), \
             patch.object(probe, "_make_sample", side_effect=RuntimeError("synthetic failure")), \
             patch.object(probe, "_render_sample") as render:
            first = probe.smoke()
            old = Path(first["receipt"]).read_bytes()
            second = probe.smoke()
            self.assertEqual(first["status"], "failed")
            self.assertEqual(first["stage"], "native_generation")
            self.assertEqual(json.loads(old), first)
            self.assertNotEqual(first["run_dir"], second["run_dir"])
            self.assertEqual(Path(first["receipt"]).read_bytes(), old)
            render.assert_not_called()

    def test_render_failure_does_not_become_visual_pass(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(probe, "REPO", Path(tmp).resolve()), \
             patch.object(probe, "doctor", return_value={"status": "ready"}), \
             patch.object(probe, "_make_sample"), \
             patch.object(probe, "_native_check", return_value={"textboxes": 3}), \
             patch.object(probe, "_render_sample", side_effect=RuntimeError("local render failed")):
            result = probe.smoke()
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["stage"], "local_render")
            self.assertEqual(result["visual_review"], "not_checked")
            self.assertEqual(result["user_approval"], "not_requested")
            self.assertNotIn("artifacts", result)
            self.assertEqual(json.loads(Path(result["receipt"]).read_text()), result)

    def test_probe_does_not_register_model_or_material_inputs(self):
        import ast
        tree = ast.parse(Path(probe.__file__).read_text())
        imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        imports.update(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
        self.assertFalse(any(name and name.split(".")[0] in {"dsh", "llm_structured"} for name in imports))
        source = Path(probe.__file__).read_text()
        self.assertNotIn("run_visual_qa(", source)
        self.assertNotIn("load_visual_env(", source)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            probe.main(["smoke", "--input", "private/example.pdf"])


class RealRenderTests(unittest.TestCase):
    def test_native_pptx_pdf_png_roundtrip(self):
        # Never skip: this test is only selected when --render was explicitly requested.
        with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            result = probe.smoke()
        self.assertEqual(result["status"], "passed", json.dumps(result, ensure_ascii=False))
        self.assertEqual(result["native_objects"], {
            "textboxes": 3, "shapes": 2, "connectors": 1, "images": 0, "bound_endpoints": 2})
        self.assertEqual(result["render"]["page_count"], 1)
        self.assertEqual(result["render"]["text_roundtrip"], "pass")
        self.assertTrue(result["render"]["observed_pdf_fonts"])
        self.assertEqual(result["visual_review"], "not_checked")
        self.assertEqual(result["user_approval"], "not_requested")
        self.assertFalse(result["authoring_available"])
        for artifact in result["artifacts"].values():
            self.assertEqual(probe._hash(Path(artifact["path"])), artifact["sha256"])
        self.assertEqual(json.loads(Path(result["receipt"]).read_text()), result)
        print(f"\nReal render receipt: {result['receipt']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render", action="store_true")
    args = parser.parse_args()
    suite = unittest.TestSuite()
    for case in (CapabilityTests, RuntimeTests, *([RealRenderTests] if args.render else [])):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    if not args.render:
        print("Real LibreOffice render not executed; use --render to require it.")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
