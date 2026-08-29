#!/usr/bin/env python3
"""Regression tests for image/PDF to editable-PowerPoint reconstruction."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

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
)


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
    prior_base = os.environ.get("VISUAL_RECONSTRUCTION_API_BASE")
    prior_key = os.environ.get("VISUAL_RECONSTRUCTION_API_KEY")
    os.environ["VISUAL_RECONSTRUCTION_API_BASE"] = "https://vision.invalid/v1"
    os.environ["VISUAL_RECONSTRUCTION_API_KEY"] = "test-key"
    try:
        summary = run_visual_to_editable_ppt(
            source,
            output_path=output,
            receipt_root=tmp / "vision-receipts",
            vision_call=_vision_stub,
            mode="faithful",
        )
    finally:
        if prior_base is None:
            os.environ.pop("VISUAL_RECONSTRUCTION_API_BASE", None)
        else:
            os.environ["VISUAL_RECONSTRUCTION_API_BASE"] = prior_base
        if prior_key is None:
            os.environ.pop("VISUAL_RECONSTRUCTION_API_KEY", None)
        else:
            os.environ["VISUAL_RECONSTRUCTION_API_KEY"] = prior_key
    page_model_path = Path(summary["run_dir"]) / "pages" / "page-0001" / "objects.json"
    model = json.loads(page_model_path.read_text(encoding="utf-8"))
    vision_objects = [item for item in model["objects"] if item.get("source_method") == "vision_guidance"]
    assert len(vision_objects) <= 1
    if vision_objects:
        assert vision_objects[0]["kind"] == "text"
        assert vision_objects[0]["text"] == "VISION LABEL"


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


def main() -> None:
    test_page_selector()
    (REPO / "temp").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="visual-to-ppt-test-", dir=REPO / "temp") as tmp_dir:
        tmp = Path(tmp_dir)
        test_vector_pdf_is_native_editable_and_resumable(tmp)
        test_raster_uses_editable_objects_and_honest_fallback(tmp)
        test_vision_guidance_is_schema_limited(tmp)
        test_forbidden_output_area(tmp)
    print("visual to editable ppt regression: PASS")


if __name__ == "__main__":
    main()
