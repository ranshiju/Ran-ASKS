#!/usr/bin/env python3
"""Regression tests for image/PDF to editable-PowerPoint reconstruction."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import fitz
from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".scripts"))

from visual_to_editable_ppt import (
    Presentation,
    VisualReconstructionError,
    parse_page_selector,
    run_visual_to_editable_ppt,
    sha256_file,
    assess_reconstruction_quality,
    build_parser,
)
from visual_ppt_workflow import check_reconstruction, commit_reconstruction, missing_glyphs


def _vector_pdf(path: Path) -> None:
    with fitz.open() as document:
        for index, label in enumerate(("VECTOR ONE", "VECTOR TWO"), start=1):
            page = document.new_page(width=720, height=420)
            page.draw_rect(
                fitz.Rect(80, 100, 300, 300),
                color=(0.1, 0.3, 0.8),
                fill=(0.75, 0.84, 0.95),
                width=2,
            )
            page.draw_circle(
                fitz.Point(470, 205), 72,
                color=(0.85, 0.2, 0.2), fill=(0.98, 0.78, 0.78), width=2,
            )
            page.draw_line(fitz.Point(300, 205), fitz.Point(398, 205), color=(0, 0, 0), width=2)
            page.insert_text((100, 72), f"{label} {index}", fontsize=24)
        document.save(path)


def _raster_figure(path: Path) -> None:
    image = Image.new("RGB", (1200, 720), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((90, 90, 1110, 630), outline=(40, 40, 40), width=4)
    draw.rectangle((180, 230, 430, 570), fill=(55, 125, 195))
    draw.ellipse((520, 260, 760, 500), fill=(225, 100, 80), outline=(160, 45, 35), width=4)
    draw.line((760, 380, 1040, 180), fill="black", width=5)
    draw.text((170, 140), "EDITABLE FIGURE", fill="black")
    image.save(path)


def _vision_stub(
    model: str, image_path: Path, prompt: str, api_base: str, api_key: str, timeout: int
) -> dict:
    assert model
    assert image_path.is_file()
    assert "normalized" in prompt
    assert api_base == "https://vision.invalid/v1"
    assert api_key == "test-key"
    return {
        "page_type": "diagram",
        "objects": [{
            "type": "text",
            "x": 0.78,
            "y": 0.78,
            "w": 0.15,
            "h": 0.08,
            "text": "VISION LABEL",
            "stroke": "#111111",
            "fill": None,
            "confidence": 0.97,
        }],
    }


def test_page_selector() -> None:
    assert build_parser().parse_args(["source.png"]).backend == "agent"
    assert parse_page_selector("all", 3) == [1, 2, 3]
    assert parse_page_selector("1,3", 3) == [1, 3]
    try:
        parse_page_selector("4", 3)
    except VisualReconstructionError:
        pass
    else:
        raise AssertionError("out-of-range page selection must fail")


def test_vector_pdf_is_native_editable_and_resumable(tmp: Path) -> None:
    source = tmp / "vector.pdf"
    output = tmp / "vector-editable.pptx"
    receipt_root = tmp / "receipts"
    _vector_pdf(source)
    before = sha256_file(source)
    first = run_visual_to_editable_ppt(
        source,
        output_path=output,
        receipt_root=receipt_root,
        deterministic_only=True,
    )
    assert output.is_file()
    assert first["fully_editable"] is True
    assert first["fallback_objects"] == 0
    assert first["assembly"]["media_files"] == []
    assert first["assembly"]["picture_elements"] == 0
    assert first["assembly"]["slide_count"] == 2
    assert first["assembly"]["object_counts"]["pdf_path"] >= 6
    assert first["assembly"]["object_counts"]["text"] >= 2
    assert first["input_unchanged"] is True
    assert sha256_file(source) == before
    presentation = Presentation(output)
    assert len(presentation.slides) == 2
    assert all(len(slide.shapes) >= 4 for slide in presentation.slides)

    resumed = run_visual_to_editable_ppt(
        source,
        output_path=output,
        receipt_root=receipt_root,
        deterministic_only=True,
    )
    assert resumed["resumed"] is True
    assert resumed["output_sha256"] == first["output_sha256"]


def test_raster_uses_editable_objects_and_honest_fallback(tmp: Path) -> None:
    source = tmp / "raster.png"
    output = tmp / "raster-editable.pptx"
    _raster_figure(source)
    before = sha256_file(source)
    summary = run_visual_to_editable_ppt(
        source,
        output_path=output,
        receipt_root=tmp / "raster-receipts",
        backend="api",
        deterministic_only=True,
        mode="balanced",
    )
    assert summary["status"] in {"complete", "partial"}
    assert summary["assembly"]["object_counts"].get("rectangle", 0) >= 2
    assert summary["assembly"]["total_objects"] >= 3
    assert 0.0 <= summary["editable_foreground_coverage_mean"] <= 1.0
    if summary["fallback_objects"]:
        assert summary["fully_editable"] is False
        assert summary["assembly"]["picture_elements"] >= 1
    assert sha256_file(source) == before
    assert summary["input_unchanged"] is True


def test_vision_guidance_is_schema_limited(tmp: Path) -> None:
    source = tmp / "vision.png"
    output = tmp / "vision-editable.pptx"
    _raster_figure(source)
    calls = []

    def call(model, *args):
        calls.append(model)
        return _vision_stub(model, *args)

    # Isolate from repository/user overrides: exercise the code default itself.
    with patch("visual_to_editable_ppt._load_env", return_value={
        "VISUAL_RECONSTRUCTION_API_BASE": "https://vision.invalid/v1",
        "VISUAL_RECONSTRUCTION_API_KEY": "test-key",
    }):
        summary = run_visual_to_editable_ppt(
            source,
            output_path=output,
            receipt_root=tmp / "vision-receipts",
            vision_call=call,
            backend="api",
            mode="faithful",
        )
    assert calls == ["GLM-5.3-Flash"], calls
    page_model_path = Path(summary["run_dir"]) / "pages" / "page-0001" / "objects.json"
    model = json.loads(page_model_path.read_text(encoding="utf-8"))
    vision_objects = [item for item in model["objects"] if item.get("source_method") == "vision_guidance"]
    assert len(vision_objects) <= 1
    if vision_objects:
        assert vision_objects[0]["kind"] == "text"
        assert vision_objects[0]["text"] == "VISION LABEL"


def test_reconstruction_model_fallback(tmp: Path) -> None:
    source = tmp / "fallback.png"
    _raster_figure(source)
    calls = []

    def call(model, *args):
        calls.append(model)
        if model == "GLM-5.3-Flash":
            raise RuntimeError("synthetic primary failure")
        return _vision_stub(model, *args)

    with patch("visual_to_editable_ppt._load_env", return_value={
        "VISUAL_RECONSTRUCTION_API_BASE": "https://vision.invalid/v1",
        "VISUAL_RECONSTRUCTION_API_KEY": "test-key",
    }):
        run_visual_to_editable_ppt(
            source,
            output_path=tmp / "fallback.pptx",
            receipt_root=tmp / "fallback-receipts",
            backend="api",
            vision_call=call,
            mode="faithful",
        )
    assert calls == ["GLM-5.3-Flash", "GLM-4.5V"], calls


def test_forbidden_output_area(tmp: Path) -> None:
    source = tmp / "source.png"
    _raster_figure(source)
    forbidden = REPO / "raw" / "visual-reconstruction-test.pptx"
    try:
        run_visual_to_editable_ppt(
            source,
            output_path=forbidden,
            receipt_root=tmp / "forbidden-receipts",
            deterministic_only=True,
        )
    except VisualReconstructionError as exc:
        assert "must not write" in str(exc)
    else:
        raise AssertionError("raw output path must be rejected")
    assert not forbidden.exists()


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _must_fail(action, message: str) -> None:
    try:
        action()
    except VisualReconstructionError as exc:
        assert message in str(exc), str(exc)
    else:
        raise AssertionError(f"expected reconstruction failure: {message}")


def test_agent_early_handoff_and_no_remote(tmp: Path) -> None:
    source = tmp / "handoff.png"
    _raster_figure(source)
    output = tmp / "handoff.pptx"
    with patch("visual_to_editable_ppt._build_presentation", side_effect=AssertionError("must not assemble")), \
         patch("visual_to_editable_ppt._default_vision_call", side_effect=AssertionError("must not call API")), \
         patch("visual_to_editable_ppt.assess_reconstruction_quality", return_value={
             "needs_reconstruction": True, "issues": [{"page": 1, "code": "low_editable_coverage"}],
         }):
        result = run_visual_to_editable_ppt(source, output_path=output,
                                          receipt_root=tmp / "handoff")
    assert not output.exists()
    assert result["status"] == "prepared"
    assert result["agent_task"]["schema"] == "agent-task-v1"
    assert result["agent_task"]["commands"]["commit"]
    assert len(result["agent_task"]["outputs"]) == 3
    with patch("visual_to_editable_ppt._analyze_raster_page", side_effect=AssertionError("skip redundant OCR")):
        forced = run_visual_to_editable_ppt(source, output_path=tmp / "forced.pptx",
                                          receipt_root=tmp / "forced", backend="agent", strategy="reconstruct")
    assert forced["status"] == "prepared"


def test_editable_mode_reports_discarded_pixels(tmp: Path) -> None:
    source = tmp / "discarded.png"
    _raster_figure(source)
    def residual(rgb, background, recognized, target):
        Image.new("RGBA", (12, 12), (12, 34, 56, 255)).save(target)
        return 0.75, 100

    with patch("visual_to_editable_ppt._make_residual_layer", side_effect=residual):
        result = run_visual_to_editable_ppt(source, output_path=tmp / "discarded.pptx",
                                          receipt_root=tmp / "discarded", deterministic_only=True,
                                          mode="editable", backend="api")
    assert result["fallback_objects"] == 0
    assert result["content_loss"] is True
    assert result["fully_editable"] is False
    assert result["status"] == "partial"
    assert result["editable_foreground_coverage_mean"] < 1
    assert result["quality"]["needs_reconstruction"]
    assert assess_reconstruction_quality([{"page": 1, "source_mode": "vector", "metrics": {}}])["issues"] == []


def test_managed_reconstruction_validation_and_publish(tmp: Path) -> None:
    source = tmp / "vector.pdf"
    output = tmp / "managed.pptx"
    prepared = run_visual_to_editable_ppt(source, output_path=output, backend="agent",
                                        strategy="reconstruct", receipt_root=tmp / "managed")
    state_path = Path(prepared["state_path"])
    folder = state_path.parent
    candidate = folder / "candidate.pptx"
    shutil.copyfile(tmp / "vector-editable.pptx", candidate)
    inventory_path = folder / "source-inventory.json"
    inventory = {"source_sha256": sha256_file(source), "pages": [
        {"page": 1, "texts": ["VECTOR ONE 1"]}, {"page": 2, "texts": ["VECTOR TWO 2"]},
    ]}
    _write_json(inventory_path, inventory)
    checked = check_reconstruction(state_path)
    assert checked["passed"] and not checked["cached"]
    with patch("visual_to_editable_ppt._render_pptx_for_comparison", side_effect=AssertionError("must reuse render")):
        assert check_reconstruction(state_path)["cached"]
    review_path = folder / "review.json"
    review = {"source_sha256": sha256_file(source), "candidate_sha256": sha256_file(candidate),
              "inventory_sha256": sha256_file(inventory_path), "pages": [
                  {"page": page, "source_checked": True, "visual_checked": True, "pictures": []}
                  for page in [1, 2]
              ]}
    _write_json(review_path, review)
    bad_review = {**review, "candidate_sha256": "stale"}
    _write_json(review_path, bad_review)
    _must_fail(lambda: commit_reconstruction(state_path), "review hashes")
    assert not output.exists()
    _write_json(review_path, review)
    with patch("visual_to_editable_ppt._render_pptx_for_comparison", side_effect=AssertionError("must reuse render")):
        result = commit_reconstruction(state_path)
        repeated = commit_reconstruction(state_path)
    assert result["output_sha256"] == sha256_file(output)
    assert repeated["resumed"]
    assert result["input_unchanged"] and result["fully_editable"]
    sidecar = json.loads(output.with_suffix(".reconstruction.json").read_text())
    assert sidecar["output_sha256"] == sha256_file(output)
    with patch("visual_to_editable_ppt._analyze_pdf_page", side_effect=AssertionError("must skip published source")):
        resumed = run_visual_to_editable_ppt(source, output_path=output, backend="agent",
                                            strategy="reconstruct", receipt_root=tmp / "managed")
    assert resumed["resumed"]
    inventory["pages"][0]["texts"].append("MISSING SOURCE CONTENT")
    _write_json(inventory_path, inventory)
    missing = check_reconstruction(state_path)
    assert not missing["passed"] and not missing["cached"]
    assert {issue["layer"] for issue in missing["issues"]} == {"native", "rendered"}
    _must_fail(lambda: commit_reconstruction(state_path), "failed validation")
    output.write_bytes(b"changed after publication")
    _must_fail(lambda: run_visual_to_editable_ppt(source, output_path=output, backend="agent",
                                                strategy="reconstruct", receipt_root=tmp / "managed"), "already exists")


def test_publication_rollback_and_output_conflict(tmp: Path) -> None:
    source = tmp / "vector.pdf"
    output = tmp / "rollback.pptx"
    output.write_bytes(b"previous output")
    prepared = run_visual_to_editable_ppt(source, output_path=output, backend="agent", strategy="reconstruct",
                                        overwrite=True, receipt_root=tmp / "rollback")
    state_path = Path(prepared["state_path"])
    folder = state_path.parent
    candidate = folder / "candidate.pptx"
    shutil.copyfile(tmp / "vector-editable.pptx", candidate)
    inventory_path = folder / "source-inventory.json"
    _write_json(inventory_path, {"source_sha256": sha256_file(source), "pages": [
        {"page": 1, "texts": ["VECTOR ONE 1"]}, {"page": 2, "texts": ["VECTOR TWO 2"]},
    ]})
    checked = check_reconstruction(state_path)
    _write_json(folder / "review.json", {"source_sha256": sha256_file(source),
        "candidate_sha256": checked["candidate_sha256"], "inventory_sha256": checked["inventory_sha256"],
        "pages": [{"page": page, "source_checked": True, "visual_checked": True, "pictures": []} for page in [1, 2]],
    })
    with patch("visual_ppt_workflow.core._write_json_atomic", side_effect=OSError("sidecar publication failure")):
        try:
            commit_reconstruction(state_path)
        except OSError:
            pass
        else:
            raise AssertionError("publication must fail")
    assert output.read_bytes() == b"previous output"
    assert not output.with_suffix(".reconstruction.json").exists()
    output.write_bytes(b"concurrent change")
    _must_fail(lambda: commit_reconstruction(state_path), "output changed")


def test_automatic_render_is_reused(tmp: Path) -> None:
    source = tmp / "vector.pdf"
    with patch("visual_to_editable_ppt._render_pptx_for_comparison", side_effect=AssertionError("must not convert twice")):
        result = run_visual_to_editable_ppt(source, output_path=tmp / "single-render.pptx",
                                          receipt_root=tmp / "single-render", deterministic_only=True)
    assert len(result["visual_comparison"]) == 2


def test_batch_preparation_and_partial_failure(tmp: Path) -> None:
    source = tmp / "batch-a.png"
    _raster_figure(source)
    second = tmp / "batch-b.png"
    shutil.copyfile(source, second)
    command = [sys.executable, str(REPO / ".scripts/visual_to_editable_ppt.py"), str(source), str(second),
               "--strategy", "reconstruct", "--output-dir", str(tmp / "batch-outputs"),
               "--receipt-root", str(tmp / "batch-receipts")]
    process = subprocess.run(command, capture_output=True, text=True, cwd=REPO, timeout=60)
    assert process.returncode == 0, process.stderr + process.stdout
    result = json.loads(process.stdout)
    assert result["counts"] == {"prepared": 2}
    assert all(item["agent_task"]["schema"] == "agent-task-v1" for item in result["items"])
    command[3] = str(tmp / "missing-input.png")
    process = subprocess.run(command, capture_output=True, text=True, cwd=REPO, timeout=60)
    assert process.returncode == 2, process.stderr + process.stdout
    result = json.loads(process.stdout)
    assert result["counts"] == {"prepared": 1, "failed": 1}
    assert result["items"][0]["resumed"]


def test_bitmap_fallback_requires_review(tmp: Path) -> None:
    source = tmp / "hybrid.pdf"
    image = tmp / "hybrid-photo.png"
    Image.new("RGB", (120, 80), "#6699CC").save(image)
    with fitz.open() as document:
        page = document.new_page(width=720, height=420)
        page.insert_text((50, 60), "BITMAP CASE", fontsize=24)
        page.insert_image(fitz.Rect(50, 100, 350, 300), filename=str(image))
        document.save(source)
    original = tmp / "hybrid-original.pptx"
    run_visual_to_editable_ppt(source, output_path=original, deterministic_only=True,
                              receipt_root=tmp / "hybrid-original", backend="api")
    output = tmp / "hybrid-managed.pptx"
    prepared = run_visual_to_editable_ppt(source, output_path=output, strategy="reconstruct",
                                        receipt_root=tmp / "hybrid-managed")
    state_path = Path(prepared["state_path"])
    folder = state_path.parent
    shutil.copyfile(original, folder / "candidate.pptx")
    _write_json(folder / "source-inventory.json", {"source_sha256": sha256_file(source),
                                                 "pages": [{"page": 1, "texts": ["BITMAP CASE"]}]})
    checked = check_reconstruction(state_path)
    assert checked["passed"]
    review = {"source_sha256": sha256_file(source), "candidate_sha256": checked["candidate_sha256"],
              "inventory_sha256": checked["inventory_sha256"], "pages": [
                  {"page": 1, "source_checked": True, "visual_checked": True, "pictures": []},
              ]}
    _write_json(folder / "review.json", review)
    _must_fail(lambda: commit_reconstruction(state_path), "every bitmap")
    review["pages"][0]["pictures"] = [{"name": name, "reason": "source photo"} for name in checked["pages"][0]["pictures"]]
    _write_json(folder / "review.json", review)
    result = commit_reconstruction(state_path)
    assert result["status"] == "partial" and result["fully_editable"] is False
    source.write_bytes(b"changed source")
    _must_fail(lambda: check_reconstruction(state_path), "source changed")


def test_missing_glyphs_are_not_text_success() -> None:
    class MissingGlyphPage:
        def get_texttrace(self):
            return [{"chars": [(ord("中"), 0), (ord("A"), 12), (ord(" "), 0)]}]

    assert missing_glyphs(MissingGlyphPage()) == ["中"]


def main() -> None:
    test_page_selector()
    (REPO / "temp").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="visual-to-ppt-test-", dir=REPO / "temp") as tmp_dir:
        tmp = Path(tmp_dir)
        test_vector_pdf_is_native_editable_and_resumable(tmp)
        test_raster_uses_editable_objects_and_honest_fallback(tmp)
        test_vision_guidance_is_schema_limited(tmp)
        test_reconstruction_model_fallback(tmp)
        test_forbidden_output_area(tmp)
        test_agent_early_handoff_and_no_remote(tmp)
        test_editable_mode_reports_discarded_pixels(tmp)
        test_managed_reconstruction_validation_and_publish(tmp)
        test_publication_rollback_and_output_conflict(tmp)
        test_automatic_render_is_reused(tmp)
        test_batch_preparation_and_partial_failure(tmp)
        test_bitmap_fallback_requires_review(tmp)
        test_missing_glyphs_are_not_text_success()
    print("visual to editable ppt regression: PASS")


if __name__ == "__main__":
    main()
