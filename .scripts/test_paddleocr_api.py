#!/usr/bin/env python3
"""Offline protocol and resource-boundary regressions for the official API."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import paddleocr_api as api


class Response:
    def __init__(self, data=None, content=b"", status=200, headers=None):
        self.data = data
        self.content = content
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self.data

    def iter_content(self, chunk_size):
        yield self.content

    def close(self):
        pass


def records(text="Formula $E=mc^2$\n\n| a | b |\n|---|---|\n![figure.png](figure.png)"):
    return [{"result": {"layoutParsingResults": [{"markdown": {
        "text": text,
        "images": {"figure.png": "https://bucket.bj.bcebos.com/figure.png",
                   "unused.png": "https://127.0.0.1/forbidden"},
    }}]}}]


class PaddleOCRTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pdf = self.root / "paper.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 test")
        self.work = self.root / ".paddleocr"
        self.submissions = 0
        self.downloads = []

    def request(self, method, url, **kwargs):
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer test-secret"})
        self.assertFalse(kwargs["allow_redirects"])
        if method == "POST":
            self.submissions += 1
            self.assertEqual(url, api.JOBS_URL)
            self.assertEqual(kwargs["data"]["model"], api.MODEL)
            self.assertEqual(json.loads(kwargs["data"]["optionalPayload"]),
                             {"returnMarkdownImages": True})
            self.assertEqual(kwargs["files"]["file"].read(), self.pdf.read_bytes())
            return Response({"code": 0, "data": {"jobId": "job-1"}})
        self.assertEqual(url, api.JOBS_URL + "/job-1")
        return Response({"code": 0, "data": {"state": "done", "resultUrl": {
            "jsonUrl": "https://bucket.bj.bcebos.com/result.jsonl"}}})

    def download(self, url, **kwargs):
        self.assertNotIn("headers", kwargs)
        self.assertFalse(kwargs["allow_redirects"])
        self.downloads.append(url)
        if url.endswith("result.jsonl"):
            return Response(content="\n".join(json.dumps(item) for item in records()).encode())
        return Response(content=b"\x89PNG\r\n\x1a\nfixture")

    def extract(self):
        return api.extract_pdf_bundle_with_paddleocr(
            self.pdf, "test-secret", work_dir=self.work)

    def test_protocol_normalized_images_and_provenance(self):
        with patch.object(requests, "request", self.request), patch.object(requests, "get", self.download):
            result = self.extract()
        self.assertEqual(self.submissions, 1)
        self.assertEqual(len(self.downloads), 2)
        self.assertIn("$E=mc^2$", result.markdown)
        self.assertIn("| a | b |", result.markdown)
        self.assertIn("![figure.png](images/page-1-", result.markdown)
        self.assertEqual(len(list((result.artifact_root / "images").iterdir())), 1)
        self.assertEqual(result.meta["input_sha256"], api._sha256_file(self.pdf))
        self.assertEqual(result.meta["page_count"], 1)
        checkpoint = (self.work / "paddleocr-job-v1.json").read_text()
        self.assertNotIn("test-secret", checkpoint)
        self.assertNotIn("resultUrl", checkpoint)

    def test_download_failure_resumes_without_new_submission(self):
        with patch.object(requests, "request", self.request), patch.object(
                requests, "get", side_effect=requests.ConnectionError("signed secret URL")):
            with self.assertRaises(api.PaddleOCRError) as error:
                self.extract()
        self.assertNotIn("signed secret", str(error.exception))
        with patch.object(requests, "request", self.request), patch.object(requests, "get", self.download):
            self.extract()
        self.assertEqual(self.submissions, 1)

    def test_input_change_rejects_checkpoint(self):
        with patch.object(requests, "request", self.request), patch.object(requests, "get", self.download):
            self.extract()
        self.pdf.write_bytes(b"changed PDF")
        with patch.object(requests, "request") as remote:
            with self.assertRaises(api.PaddleOCRError):
                self.extract()
            remote.assert_not_called()

    def test_ambiguous_submission_is_not_retried(self):
        with patch.object(requests, "request", side_effect=requests.Timeout("test-secret")) as remote:
            with self.assertRaises(api.PaddleOCRError):
                self.extract()
            with self.assertRaises(api.PaddleOCRError) as error:
                self.extract()
            self.assertEqual(remote.call_count, 1)
            self.assertNotIn("test-secret", str(error.exception))

    def test_resource_hosts_redirects_and_oversize(self):
        for url in ("http://bucket.bj.bcebos.com/x", "https://127.0.0.1/x",
                    "https://evil.example/x", "https://bcebos.com.evil.example/x",
                    "https://user:password@bucket.bj.bcebos.com/x"):
            with self.subTest(url=url), self.assertRaises(api.PaddleOCRError):
                api._resource_url(url)
        target = self.root / "image.png"
        target.write_bytes(b"old")
        with patch.object(requests, "get", return_value=Response(content=b"oversize")):
            with self.assertRaises(api.PaddleOCRError):
                api._download("https://bucket.bj.bcebos.com/x", target, timeout=60, max_bytes=3)
        self.assertEqual(target.read_bytes(), b"old")
        self.assertFalse(target.with_suffix(".png.partial").exists())
        with patch.object(requests, "get", return_value=Response(
                status=302, headers={"Location": "https://127.0.0.1/x"})) as remote:
            with self.assertRaises(api.PaddleOCRError):
                api._download("https://bucket.bj.bcebos.com/x", target, timeout=60, max_bytes=3)
            self.assertEqual(remote.call_count, 1)

    def test_unsafe_or_missing_image_reference_never_downloads(self):
        for text in ("![x](../figure.png)", "![x](https://evil.example/x.png)",
                     "![x](missing.png)", '<img src="https://evil.example/x">', ""):
            with self.subTest(text=text), patch.object(requests, "get") as remote:
                with self.assertRaises(api.PaddleOCRError):
                    api._materialize_pages(records(text), self.root, timeout=60)
                remote.assert_not_called()

    def test_official_html_image_is_normalized_and_downloaded_once(self):
        text = '<img src="figure.png" alt="Figure [1] &amp; data" width="24%" />\n![again](figure.png)'
        with patch.object(requests, "get", self.download):
            markdown, pages = api._materialize_pages(records(text), self.root, timeout=60)
        self.assertEqual(pages, 1)
        self.assertIn("![Figure (1) &amp; data](images/page-1-", markdown)
        self.assertNotIn("<img", markdown)
        self.assertEqual(len(self.downloads), 1)

    def test_html_image_cannot_expand_resource_or_execution_permissions(self):
        tags = (
            '<img src="figure.png" onerror="execute()">',
            '<img src="figure.png" srcset="https://evil.example/other.png 2x">',
            '<img src="figure.png" src="../other.png">',
            '<img src="../figure.png">',
            '<img src="https://evil.example/figure.png">',
            '<img src="missing.png">',
            '<img alt="missing source">',
            '<img src="figure.png"',
        )
        for tag in tags:
            with self.subTest(tag=tag), patch.object(requests, "get") as remote:
                with self.assertRaises(api.PaddleOCRError):
                    api._materialize_pages(records(tag), self.root, timeout=60)
                remote.assert_not_called()

    def test_poll_timeout_preserves_job(self):
        def pending(method, url, **kwargs):
            if method == "POST":
                return self.request(method, url, **kwargs)
            return Response({"code": 0, "data": {"state": "pending"}})

        with patch.object(requests, "request", pending), patch.object(api.time, "sleep"), patch.object(
                api.time, "monotonic", side_effect=range(100)):
            with self.assertRaises(api.PaddleOCRError):
                api.extract_pdf_bundle_with_paddleocr(
                    self.pdf, "test-secret", work_dir=self.work, poll_timeout_sec=3)
        self.assertEqual(json.loads((self.work / "paddleocr-job-v1.json").read_text())["job_id"], "job-1")

    def test_malformed_result_and_business_failure(self):
        with self.assertRaises(api.PaddleOCRError):
            api._api_data(Response({"code": 123, "message": "test-secret"}))
        with patch.object(requests, "request", self.request), patch.object(
                requests, "get", return_value=Response(content=b"not-json")):
            with self.assertRaises(api.PaddleOCRError):
                self.extract()
        with self.assertRaises(api.PaddleOCRError):
            api._materialize_pages([{"result": {}}], self.root, timeout=60)

    def test_authentication_failure_is_not_retried(self):
        with patch.object(requests, "request", return_value=Response(status=401)) as remote:
            with self.assertRaises(api.PaddleOCRError):
                self.extract()
            self.assertEqual(remote.call_count, 1)
        self.assertEqual(json.loads((self.work / "paddleocr-job-v1.json").read_text())["status"], "rejected")
        with patch.object(requests, "request", self.request), patch.object(requests, "get", self.download):
            self.extract()
        self.assertEqual(self.submissions, 1)

    def test_symlink_asset_directory_is_rejected(self):
        artifacts = self.root / "artifacts"
        artifacts.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (artifacts / "images").symlink_to(outside, target_is_directory=True)
        with patch.object(requests, "get") as remote:
            with self.assertRaises(api.PaddleOCRError):
                api._materialize_pages(records(), artifacts, timeout=60)
            remote.assert_not_called()
        self.assertFalse(list(outside.iterdir()))


if __name__ == "__main__":
    unittest.main()
