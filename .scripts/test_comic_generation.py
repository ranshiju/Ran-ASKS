#!/usr/bin/env python3
from __future__ import annotations

import base64
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import comic_generation as cg


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zf1sAAAAASUVORK5CYII="
)


class ComicGenerationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        self.project = self.repo / "projects" / "demo"
        (self.project / "outputs").mkdir(parents=True)
        (self.project / "notes").mkdir()
        (self.project / "schema.yaml").write_text("project: {}\n", encoding="utf-8")
        (self.repo / "teaching" / "outputs").mkdir(parents=True)
        self.catalog = self.repo / "llm-models.yaml"
        self.catalog.write_text(
            "image_generation:\n"
            "  candidates:\n"
            "    - model: Test-Image\n"
            "      task: text_to_image\n"
            "      probe_status: pending\n",
            encoding="utf-8",
        )
        self.env = self.repo / ".env"
        self.env.write_text(
            "LLM_API_BASE=https://provider.example/v1\n"
            "LLM_API_KEY=secret-value\n"
            "COMIC_IMAGE_API_BASE=${LLM_API_BASE}\n"
            "COMIC_IMAGE_API_KEY=${LLM_API_KEY}\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def kwargs(self, **overrides):
        values = {
            "project": "demo",
            "explicit_output_root": None,
            "article_id": "article-01",
            "asset_id": "cover",
            "prompt": "A clear educational comic",
            "model": "Test-Image",
            "size": "1024x1024",
            "parameters": {},
            "catalog_path": self.catalog,
            "repo": self.repo,
            "env_file": self.env,
        }
        values.update(overrides)
        return values

    def test_catalog_candidates_are_explicit(self):
        self.assertEqual(cg.image_candidates(self.catalog)[0]["model"], "Test-Image")
        with self.assertRaises(cg.ComicGenerationError):
            cg.require_candidate("Unknown", self.catalog)

    def test_dry_run_does_not_write_or_require_remote_permission(self):
        result = cg.generate_asset(**self.kwargs(), dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse((self.project / "outputs" / "article-01").exists())

    def test_remote_call_requires_explicit_permission(self):
        with self.assertRaisesRegex(cg.ComicGenerationError, "allow-remote"):
            cg.generate_asset(**self.kwargs())

    def test_general_output_root_is_supported_and_guarded(self):
        output = cg.output_directory(
            None,
            "article-01",
            self.repo,
            self.repo / "teaching" / "outputs",
        )
        self.assertEqual(
            output,
            (self.repo / "teaching" / "outputs" / "article-01" / "images").resolve(),
        )
        forbidden = self.repo / "raw" / "outputs"
        forbidden.mkdir(parents=True)
        with self.assertRaises(cg.ComicGenerationError):
            cg.allowed_output_root(forbidden, self.repo)

    def test_project_output_symlink_may_not_escape_repository(self):
        outside = self.repo.parent / f"outside-{self.repo.name}"
        outside.mkdir()
        original = self.project / "outputs"
        original.rmdir()
        original.symlink_to(outside, target_is_directory=True)
        try:
            with self.assertRaises(cg.ComicGenerationError):
                cg.output_directory("demo", "article-01", self.repo)
        finally:
            original.unlink()
            outside.rmdir()

    def test_base64_response_writes_image_and_sanitized_receipt(self):
        payload = {"data": [{"b64_json": base64.b64encode(PNG_1X1).decode("ascii")}]}
        with mock.patch.object(cg, "call_image_api", return_value=(payload, "req-123")):
            result = cg.generate_asset(**self.kwargs(), allow_remote=True)
        output = Path(result["output_path"])
        self.assertEqual(output.read_bytes(), PNG_1X1)
        manifest = json.loads((output.parent / "manifest.json").read_text(encoding="utf-8"))
        receipt = manifest["runs"][0]
        serialized = json.dumps(receipt)
        self.assertEqual(receipt["review_status"], "pending")
        self.assertNotIn("secret-value", serialized)
        self.assertNotIn("b64_json", serialized)
        self.assertNotIn("https://provider.example", serialized)

    def test_storyboard_must_stay_outside_protected_and_generated_dirs(self):
        storyboard = self.project / "notes" / "storyboard.yaml"
        storyboard.write_text(
            "article_id: article-01\nassets:\n  - id: cover\n    prompt: Explain one idea\n",
            encoding="utf-8",
        )
        value = cg.load_storyboard("demo", storyboard, self.repo)
        self.assertEqual(value["assets"][0]["id"], "cover")
        generated = self.project / "outputs" / "storyboard.yaml"
        generated.write_text("article_id: x\nassets: []\n", encoding="utf-8")
        with self.assertRaises(cg.ComicGenerationError):
            cg.load_storyboard("demo", generated, self.repo)

    def test_endpoint_does_not_duplicate_v1(self):
        self.assertEqual(
            cg.endpoint_url("https://provider.example/v1", "/v1/images/generations"),
            "https://provider.example/v1/images/generations",
        )
        with self.assertRaises(cg.ComicGenerationError):
            cg.endpoint_url("http://provider.example", "/v1/images/generations")

    def test_generated_url_rejects_local_network_targets(self):
        for url in ("http://localhost/image.png", "http://127.0.0.1/image.png", "file:///tmp/a.png"):
            with self.assertRaises(cg.ComicGenerationError):
                cg._validate_download_url(url)

    def test_cli_does_not_expose_trust_boundary_overrides(self):
        parser = cg.build_parser()
        help_text = parser.format_help()
        for option in ("--repo", "--catalog", "--env-file", "--endpoint", "--api-key"):
            self.assertNotIn(option, help_text)
        arguments = [
            "generate", "--project", "demo", "--article-id", "a", "--asset-id", "cover",
            "--model", "Test-Image", "--prompt", "one concept", "--endpoint", "https://other.example",
        ]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(arguments)


if __name__ == "__main__":
    unittest.main()
