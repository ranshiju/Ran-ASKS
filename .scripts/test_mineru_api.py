#!/usr/bin/env python3
"""Regression tests for the resumable MinerU API client."""

import json
import stat
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

import mineru_api


class FakeResponse:
    def __init__(self, status_code=200, payload=None, chunks=(), headers=None):
        self.status_code = status_code
        self._payload = payload
        self._chunks = list(chunks)
        self.headers = headers or {}
        self.closed = False

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload

    def iter_content(self, chunk_size=0):
        del chunk_size
        yield from self._chunks

    def close(self):
        self.closed = True


def _zip_result(path: Path, *, markdown=True, image=True):
    with zipfile.ZipFile(path, "w") as archive:
        if markdown:
            archive.writestr("full.md", "# Paper\n\n![](images/figure.png)\n")
        if image:
            archive.writestr("images/figure.png", b"png")
        archive.writestr("layout.json", "{}")


def test_auth_gateway_envelope_is_terminal_auth_error():
    response = FakeResponse(200, {
        "success": False,
        "msgCode": "A0202",
        "msg": "user authenticate failed",
    })
    try:
        mineru_api._request_json(response, "test")
    except mineru_api.MinerUAuthError as exc:
        assert exc.code == "A0202"
        assert exc.retryable is False
    else:
        raise AssertionError("gateway auth envelope must be classified as auth")


def test_quota_and_input_errors_are_not_retryable():
    cases = [
        (-60018, mineru_api.MinerUQuotaError),
        (-60006, mineru_api.MinerUInputError),
    ]
    for code, expected in cases:
        response = FakeResponse(200, {"code": code, "msg": "failed"})
        try:
            mineru_api._request_json(response, "test")
        except expected as exc:
            assert exc.code == code
            assert exc.retryable is False
        else:
            raise AssertionError(f"{code} must be classified as {expected.__name__}")


def test_http_transient_retry_is_bounded():
    responses = [
        FakeResponse(503, {}, headers={"Retry-After": "0"}),
        FakeResponse(200, {"code": 0, "data": {}}),
    ]
    retry_state = {}
    with mock.patch.object(mineru_api.requests, "request", side_effect=responses) as request:
        with mock.patch.object(mineru_api.time, "sleep"):
            response = mineru_api._request_with_retry(
                "GET", "https://mineru.invalid/test", timeout=1, retry_state=retry_state
            )
    assert response.status_code == 200
    assert request.call_count == 2
    assert retry_state == {"http": 1}


def test_download_streams_to_atomic_destination():
    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / "result.zip"
        response = FakeResponse(200, chunks=[b"abc", b"", b"def"])
        with mock.patch.object(mineru_api.requests, "request", return_value=response):
            mineru_api.download_file("https://download.invalid/result", destination, 1)
        assert destination.read_bytes() == b"abcdef"
        assert not (destination.parent / ".result.zip.partial").exists()


def test_resume_after_download_failure_does_not_resubmit_remote_job():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        pdf = root / "paper.pdf"
        pdf.write_bytes(b"%PDF-not-a-real-pdf")
        work = root / "work"
        calls = {"reserve": 0, "upload": 0, "poll": 0, "download": 0}

        def reserve(*args, **kwargs):
            del args, kwargs
            calls["reserve"] += 1
            return "batch-1", "https://upload.invalid/one"

        def upload(*args, **kwargs):
            del args, kwargs
            calls["upload"] += 1

        def poll(*args, **kwargs):
            del args, kwargs
            calls["poll"] += 1
            return {"state": "done", "full_zip_url": "https://download.invalid/one"}

        def download(url, destination, timeout, **kwargs):
            del url, timeout, kwargs
            calls["download"] += 1
            if calls["download"] == 1:
                raise mineru_api.MinerUTransientError("temporary", retryable=True)
            _zip_result(destination)

        patches = (
            mock.patch.object(mineru_api, "apply_upload_url", side_effect=reserve),
            mock.patch.object(mineru_api, "upload_file", side_effect=upload),
            mock.patch.object(mineru_api, "poll_result", side_effect=poll),
            mock.patch.object(mineru_api, "download_file", side_effect=download),
        )
        with patches[0], patches[1], patches[2], patches[3]:
            try:
                mineru_api.extract_pdf_bundle_with_mineru(pdf, "token", work_dir=work)
            except mineru_api.MinerUTransientError:
                pass
            else:
                raise AssertionError("first download must fail")
            result = mineru_api.extract_pdf_bundle_with_mineru(pdf, "token", work_dir=work)

        assert calls == {"reserve": 1, "upload": 1, "poll": 1, "download": 2}
        assert result.meta["batch_id"] == "batch-1"
        assert len(result.image_paths) == 1
        assert len(result.sidecar_paths) == 1
        checkpoint = json.loads(result.checkpoint_path.read_text(encoding="utf-8"))
        assert checkpoint["status"] == "complete"
        assert checkpoint["batch_id"] == "batch-1"


def test_safe_extract_rejects_traversal_and_symlink():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        traversal = root / "traversal.zip"
        with zipfile.ZipFile(traversal, "w") as archive:
            archive.writestr("../escape.md", "bad")
        try:
            mineru_api.safe_extract_zip(traversal, root / "traversal-out")
        except mineru_api.MinerUInputError:
            pass
        else:
            raise AssertionError("ZIP traversal must be rejected")

        symlink = root / "symlink.zip"
        with zipfile.ZipFile(symlink, "w") as archive:
            info = zipfile.ZipInfo("images/link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "target")
        try:
            mineru_api.safe_extract_zip(symlink, root / "symlink-out")
        except mineru_api.MinerUInputError:
            pass
        else:
            raise AssertionError("ZIP symlink must be rejected")


def test_missing_markdown_is_a_failure():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        archive = root / "result.zip"
        _zip_result(archive, markdown=False)
        target = root / "out"
        mineru_api.safe_extract_zip(archive, target)
        assert mineru_api.find_main_markdown(target, "paper") is None


if __name__ == "__main__":
    test_auth_gateway_envelope_is_terminal_auth_error()
    test_quota_and_input_errors_are_not_retryable()
    test_http_transient_retry_is_bounded()
    test_download_streams_to_atomic_destination()
    test_resume_after_download_failure_does_not_resubmit_remote_job()
    test_safe_extract_rejects_traversal_and_symlink()
    test_missing_markdown_is_a_failure()
    print("mineru api regression: PASS")
