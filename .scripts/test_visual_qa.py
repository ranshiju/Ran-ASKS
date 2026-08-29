#!/usr/bin/env python3
"""Regression tests for resumable visual artifact QA."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import fitz
from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".scripts"))

from visual_qa import (
    VisualQAError,
    _find_soffice,
    deterministic_checks,
    load_visual_env,
    parse_page_selector,
    run_visual_qa,
)


def _normal_image(path: Path, color: tuple[int, int, int] = (40, 100, 180)) -> None:
    image = Image.new("RGB", (1200, 800), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((120, 100, 1080, 650), fill=(235, 240, 248), outline="black", width=4)
    draw.rectangle((220, 220, 500, 560), fill=color)
    draw.rectangle((600, 300, 950, 560), fill=(220, 100, 70))
    draw.line((180, 650, 1050, 650), fill="black", width=3)
    image.save(path)


def _two_page_pdf(path: Path) -> None:
    with fitz.open() as doc:
        for index, label in enumerate(("PAGE ONE", "PAGE TWO"), start=1):
            page = doc.new_page(width=720, height=540)
            page.draw_rect(fitz.Rect(80, 80, 640, 440), color=(0, 0, 0), width=2)
            page.draw_rect(
                fitz.Rect(150 + index * 20, 180, 360 + index * 30, 360),
                color=(0.1, 0.3, 0.8), fill=(0.8, 0.88, 0.98), width=2,
            )
            page.insert_text((110, 130), label, fontsize=28)
        doc.save(path)


def _vision_pass(model: str, image_path: Path, prompt: str, config) -> dict:
    return {
        "verdict": "pass",
        "scores": {
            "legibility": 92,
            "layout": 90,
            "visual_hierarchy": 88,
            "consistency": 94,
            "accessibility": 85,
        },
        "issues": [],
        "needs_human_review": False,
        "summary": f"Checked by {model}",
    }


def test_page_selector() -> None:
    assert parse_page_selector("all", 4) == [1, 2, 3, 4]
    assert parse_page_selector("1,3-4", 4) == [1, 3, 4]
    try:
        parse_page_selector("5", 4)
    except VisualQAError:
        pass
    else:
        raise AssertionError("out-of-range selector must fail")


def test_visual_env_reuses_main_llm_credentials(tmp: Path) -> None:
    env_file = tmp / ".env"
    env_file.write_text(
        "LLM_API_BASE=https://provider.example/v1\n"
        "LLM_API_KEY=test-secret\n"
        "VISUAL_QA_API_BASE=${LLM_API_BASE}\n"
        "VISUAL_QA_API_KEY=${LLM_API_KEY}\n"
        "VISUAL_QA_MODEL=GLM-4.6V\n"
        "VISUAL_QA_FALLBACK_MODEL=GLM-4.5V\n",
        encoding="utf-8",
    )
    values = load_visual_env(env_file)
    assert values["VISUAL_QA_API_BASE"] == "https://provider.example/v1"
    assert values["VISUAL_QA_API_KEY"] == "test-secret"
    assert values["VISUAL_QA_MODEL"] == "GLM-4.6V"
    assert values["VISUAL_QA_FALLBACK_MODEL"] == "GLM-4.5V"


def test_deterministic_checks(tmp: Path) -> None:
    normal = tmp / "normal.png"
    _normal_image(normal)
    checked = deterministic_checks(normal, "figure")
    assert checked["verdict"] in {"pass", "warn"}
    assert checked["metrics"]["width_px"] == 1200

    blank = tmp / "blank.png"
    Image.new("RGB", (1200, 800), "white").save(blank)
    blank_checked = deterministic_checks(blank, "figure")
    assert blank_checked["verdict"] == "fail"
    assert "blank_or_near_blank" in {item["code"] for item in blank_checked["issues"]}

    small = tmp / "small.png"
    Image.new("RGB", (100, 100), (100, 100, 100)).save(small)
    small_checked = deterministic_checks(small, "figure")
    assert small_checked["verdict"] == "fail"
    assert "resolution_too_small" in {item["code"] for item in small_checked["issues"]}


def test_image_receipt_resume_and_input_hash(tmp: Path) -> None:
    image = tmp / "figure.png"
    receipts = tmp / "receipts"
    _normal_image(image)
    first = run_visual_qa(
        image, deterministic_only=True, receipt_root=receipts
    )
    assert first["status"] == "completed"
    assert first["checked_pages"] == 1
    assert first["resumed_pages"] == 0
    summary_path = Path(first["summary_path"])
    assert summary_path.is_file()

    second = run_visual_qa(
        image, deterministic_only=True, receipt_root=receipts
    )
    assert second["resumed_pages"] == 1
    assert second["receipt_dir"] == first["receipt_dir"]

    _normal_image(image, color=(30, 170, 90))
    changed = run_visual_qa(
        image, deterministic_only=True, receipt_root=receipts
    )
    assert changed["artifact_sha256"] != first["artifact_sha256"]
    assert changed["receipt_dir"] != first["receipt_dir"]


def test_repository_receipts_cannot_write_raw(tmp: Path) -> None:
    image = tmp / "figure.png"
    _normal_image(image)
    forbidden = REPO / "academic" / "raw" / "__visual_qa_forbidden_test__"
    assert not forbidden.exists()
    try:
        run_visual_qa(
            image,
            deterministic_only=True,
            receipt_root=forbidden,
        )
    except VisualQAError:
        pass
    else:
        raise AssertionError("repository-local receipts outside temp/ must fail")
    assert not forbidden.exists()


def test_pdf_page_selection_and_resume(tmp: Path) -> None:
    pdf = tmp / "two-pages.pdf"
    _two_page_pdf(pdf)
    result = run_visual_qa(
        pdf,
        pages="2",
        deterministic_only=True,
        receipt_root=tmp / "pdf-receipts",
    )
    assert result["source_kind"] == "pdf"
    assert result["total_pages"] == 2
    assert result["selected_pages"] == [2]
    assert Path(result["receipt_dir"], "renders", "page-0002.png").is_file()
    resumed = run_visual_qa(
        pdf,
        pages="2",
        deterministic_only=True,
        receipt_root=tmp / "pdf-receipts",
    )
    assert resumed["resumed_pages"] == 1


def test_remote_primary_fallback(tmp: Path) -> None:
    image = tmp / "fallback.png"
    _normal_image(image)
    calls: list[str] = []

    def fallback_call(model, image_path, prompt, config):
        calls.append(model)
        if model == "GLM-4.6V":
            raise TimeoutError("simulated timeout")
        return _vision_pass(model, image_path, prompt, config)

    result = run_visual_qa(
        image,
        receipt_root=tmp / "remote-receipts",
        api_base="https://example.invalid/v1",
        api_key="test-key",
        vision_call=fallback_call,
    )
    assert result["status"] == "completed"
    assert calls == ["GLM-4.6V", "GLM-4.5V"]
    page = json.loads(Path(
        result["receipt_dir"], "pages", "page-0001.json"
    ).read_text(encoding="utf-8"))
    assert page["remote"]["model_used"] == "GLM-4.5V"
    assert page["state"] == "complete"


def test_remote_failure_is_partial_not_pass(tmp: Path) -> None:
    image = tmp / "failure.png"
    _normal_image(image)

    def always_fail(model, image_path, prompt, config):
        raise VisualQAError("invalid model JSON")

    result = run_visual_qa(
        image,
        receipt_root=tmp / "failure-receipts",
        api_base="https://example.invalid/v1",
        api_key="test-key",
        vision_call=always_fail,
    )
    assert result["status"] == "partial"
    assert result["verdict"] != "pass"
    page = json.loads(Path(
        result["receipt_dir"], "pages", "page-0001.json"
    ).read_text(encoding="utf-8"))
    assert page["remote"]["status"] == "error"
    assert page["state"] == "partial"


def test_missing_remote_config_is_not_pass(tmp: Path) -> None:
    image = tmp / "not-configured.png"
    _normal_image(image)
    result = run_visual_qa(
        image,
        receipt_root=tmp / "not-configured-receipts",
        api_base="",
        api_key="",
    )
    assert result["status"] == "partial"
    assert result["verdict"] == "not_checked"


def test_paper_pdf_requires_remote_opt_in(tmp: Path) -> None:
    pdf = tmp / "paper.pdf"
    _two_page_pdf(pdf)
    called = False

    def must_not_call(model, image_path, prompt, config):
        nonlocal called
        called = True
        return _vision_pass(model, image_path, prompt, config)

    blocked = run_visual_qa(
        pdf,
        pages="1",
        profile="paper",
        receipt_root=tmp / "paper-protected-receipts",
        api_base="https://example.invalid/v1",
        api_key="test-key",
        vision_call=must_not_call,
    )
    assert called is False
    assert blocked["status"] == "partial"
    assert blocked["sensitive_path"] is True

    allowed = run_visual_qa(
        pdf,
        pages="1",
        profile="paper",
        receipt_root=tmp / "paper-protected-receipts",
        api_base="https://example.invalid/v1",
        api_key="test-key",
        allow_remote=True,
        vision_call=must_not_call,
    )
    assert called is True
    assert allowed["status"] == "completed"


def test_sensitive_remote_guard(tmp: Path) -> None:
    raw_dir = tmp / "academic" / "raw" / "works" / "paper"
    raw_dir.mkdir(parents=True)
    image = raw_dir / "private-figure.png"
    _normal_image(image)
    called = False

    def must_not_call(model, image_path, prompt, config):
        nonlocal called
        called = True
        return _vision_pass(model, image_path, prompt, config)

    blocked = run_visual_qa(
        image,
        receipt_root=tmp / "sensitive-receipts",
        api_base="https://example.invalid/v1",
        api_key="test-key",
        vision_call=must_not_call,
    )
    assert called is False
    assert blocked["status"] == "partial"
    assert blocked["sensitive_path"] is True
    assert blocked["verdict"] != "pass"

    allowed = run_visual_qa(
        image,
        receipt_root=tmp / "sensitive-receipts",
        api_base="https://example.invalid/v1",
        api_key="test-key",
        allow_remote=True,
        vision_call=must_not_call,
    )
    assert called is True
    assert allowed["status"] == "completed"


def test_pptx_static_render_when_available(tmp: Path) -> None:
    fixture = REPO / "slide-library" / "templates" / "Attention复杂度进化-时间线与数据.pptx"
    if not fixture.is_file():
        print("  SKIP PPTX render: fixture unavailable")
        return
    try:
        _find_soffice()
    except VisualQAError:
        print("  SKIP PPTX render: soffice unavailable")
        return
    result = run_visual_qa(
        fixture,
        pages="1",
        profile="slides",
        deterministic_only=True,
        receipt_root=tmp / "pptx-receipts",
    )
    assert result["source_kind"] == "slides"
    assert result["checked_pages"] == 1
    assert Path(result["receipt_dir"], "renders", "source-slides.pdf").is_file()
    assert Path(result["receipt_dir"], "renders", "page-0001.png").is_file()


def main() -> None:
    test_page_selector()
    with tempfile.TemporaryDirectory(prefix="visual-qa-test-") as tmp_dir:
        tmp = Path(tmp_dir)
        test_deterministic_checks(tmp)
        test_visual_env_reuses_main_llm_credentials(tmp)
        test_image_receipt_resume_and_input_hash(tmp)
        test_repository_receipts_cannot_write_raw(tmp)
        test_pdf_page_selection_and_resume(tmp)
        test_remote_primary_fallback(tmp)
        test_remote_failure_is_partial_not_pass(tmp)
        test_missing_remote_config_is_not_pass(tmp)
        test_paper_pdf_requires_remote_opt_in(tmp)
        test_sensitive_remote_guard(tmp)
        test_pptx_static_render_when_available(tmp)
    print("visual QA regression: PASS")


if __name__ == "__main__":
    main()
