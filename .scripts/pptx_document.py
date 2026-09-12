#!/usr/bin/env python3
"""Shared PPTX extraction and source-bound host review; no remote control loop."""
from __future__ import annotations

import hashlib
import json
import posixpath
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

REPO = Path(__file__).resolve().parent.parent
NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
SCHEMA = "pptx-extraction-v1"
REVIEW_SCHEMA = "pptx-review-v1"


class PPTXError(ValueError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> str:
    return digest(path.read_bytes())


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _read_json(path: Path) -> dict:
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise PPTXError(f"需要 JSON 对象: {path.name}")
    return result


def _relationships(archive, part):
    relfile = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
    if relfile not in archive.namelist():
        return {}
    result = {}
    for rel in ET.fromstring(archive.read(relfile)):
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target", "")
        resolved = (target.lstrip("/") if target.startswith("/") else
                    posixpath.normpath(posixpath.join(posixpath.dirname(part), target)))
        if resolved.startswith("../"):
            raise PPTXError("非法 PPTX 内部关系路径")
        result[rel.get("Id")] = (resolved, rel.get("Type", "").rsplit("/", 1)[-1])
    return result


def _paragraphs(element):
    result = []
    for paragraph in element.findall("a:p", NS):
        text = "".join("\n" if n.tag == f"{{{NS['a']}}}br" else (n.text or "")
                       for n in paragraph.iter() if n.tag in {f"{{{NS['a']}}}t", f"{{{NS['a']}}}br"})
        prop = paragraph.find("a:pPr", NS)
        level = int(prop.get("lvl", "0")) if prop is not None else 0
        result.append("  " * level + text)
    return result


def _shape_lines(shape, counters):
    tag = shape.tag.rsplit("}", 1)[-1]
    props = shape.find(".//p:cNvPr", NS)
    sid = props.get("id", "?") if props is not None else "?"
    transform = shape.find("p:spPr/a:xfrm", NS)
    if transform is None:
        transform = shape.find("p:xfrm", NS)
    if transform is None:
        transform = shape.find("p:grpSpPr/a:xfrm", NS)
    geometry = ({n.tag.rsplit("}", 1)[-1]: dict(n.attrib) for n in transform}
                if transform is not None else {})
    if transform is not None and transform.attrib:
        geometry["transform"] = dict(transform.attrib)
    if tag == "grpSp" or "transform" in geometry:
        position = _json(geometry)
    else:
        position = ",".join(str(geometry.get(key, {}).get(attr, "?"))
                            for key, attr in (("off", "x"), ("off", "y"), ("ext", "cx"), ("ext", "cy")))
    lines = [f"### 对象 {sid} {tag} [{position}]", ""]
    if tag == "grpSp":
        counters["groups"] += 1
        for child in shape:
            if child.tag.rsplit("}", 1)[-1] in {"sp", "pic", "graphicFrame", "cxnSp", "grpSp"}:
                lines.extend(_shape_lines(child, counters))
        return lines
    if tag == "pic":
        counters["pictures"] += 1
        lines.append("[图片对象：文字与信息须对照页图复核，不从文件名推断内容]")
    text_body = shape.find("p:txBody", NS)
    if text_body is not None:
        lines.extend(_paragraphs(text_body))
    table = shape.find(".//a:tbl", NS)
    if table is not None:
        counters["tables"] += 1
        rows = table.findall("a:tr", NS)
        widths = [col.get("w") for col in table.findall("a:tblGrid/a:gridCol", NS)]
        lines += ["表格：原始网格行列；空单元格不填充，不推定首行为表头。", "列宽（EMU）：" + _json(widths)]
        merges = []
        for r, row in enumerate(rows, 1):
            cells = []
            for c, cell in enumerate(row.findall("a:tc", NS), 1):
                body = cell.find("a:txBody", NS)
                cells.append("\n".join(_paragraphs(body)) if body is not None else "")
                attrs = {k: v for k, v in cell.attrib.items() if k in {"gridSpan", "rowSpan", "hMerge", "vMerge"}}
                if attrs:
                    merges.append({"row": r, "column": c, **attrs})
            lines.append(f"R{r}: " + _json(cells))
        lines.append("合并信息（起点跨度及被合并格标记）：" + _json(merges))
    if tag == "cxnSp":
        counters["connectors"] += 1
        ends = [dict(n.attrib) for n in shape.findall(".//a:stCxn", NS) + shape.findall(".//a:endCxn", NS)]
        lines.append("连接端点（XML 顺序为起点、终点，不推断语义方向）：" + _json(ends))
    if tag == "graphicFrame" and table is None:
        counters["unsupported"] += 1
        lines.append("[非表格图形对象：图表/SmartArt/嵌入对象须视觉复核；未自动转写数据]")
    return lines + [""]


def extract(source: Path) -> tuple[str, dict]:
    """Read native OOXML, ordered by presentation relationships, never ZIP filename order."""
    source = Path(source)
    if source.suffix.lower() != ".pptx":
        raise PPTXError("原生提取仅支持 .pptx；旧 .ppt 须先显式转换")
    blob = source.read_bytes()
    import io
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        presentation = ET.fromstring(archive.read("ppt/presentation.xml"))
        rels = _relationships(archive, "ppt/presentation.xml")
        size = presentation.find("p:sldSz", NS)
        pages, blocks = [], []
        for number, item in enumerate(presentation.findall("p:sldIdLst/p:sldId", NS), 1):
            part, kind = rels[item.get(f"{{{NS['r']}}}id")]
            if kind != "slide":
                raise PPTXError("presentation 关系不是 slide")
            slide = ET.fromstring(archive.read(part))
            counters = dict(pictures=0, tables=0, groups=0, connectors=0, unsupported=0)
            lines = [f"## 幻灯片 {number}", f"原始页序：{number}；隐藏：{slide.get('show', '1') == '0'}。", ""]
            tree = slide.find("p:cSld/p:spTree", NS)
            if tree is None:
                raise PPTXError(f"第 {number} 页缺少对象树")
            for shape in tree:
                if shape.tag.rsplit("}", 1)[-1] in {"sp", "pic", "graphicFrame", "cxnSp", "grpSp"}:
                    lines.extend(_shape_lines(shape, counters))
                elif shape.tag.rsplit("}", 1)[-1] not in {"nvGrpSpPr", "grpSpPr", "extLst"}:
                    counters["unsupported"] += 1
                    lines.append("[未支持的页面对象：须视觉复核]")
            notes = []
            for note_part, relation in _relationships(archive, part).values():
                if relation != "notesSlide":
                    continue
                root = ET.fromstring(archive.read(note_part))
                for shape in root.findall(".//p:sp", NS):
                    ph = shape.find(".//p:ph", NS)
                    body = shape.find("p:txBody", NS)
                    if body is not None and (ph is None or ph.get("type") == "body"):
                        notes.extend(_paragraphs(body))
            if any(n.strip() for n in notes):
                lines += ["### 演讲者备注（与页面可见正文分开）", *notes, ""]
            block = "\n".join(lines).rstrip() + "\n"
            blocks.append(block)
            pages.append({"number": number, "part": part, "hidden": slide.get("show") == "0",
                          "objects": counters, "native_sha256": digest(block.encode()), "text": block})
        if not pages:
            raise PPTXError("PPTX 无幻灯片")
    header = "# PPTX 逐页原生提取\n\n对象位置为 x,y,w,h（EMU）；组内位置保留原始变换，不将存储顺序视为阅读关系。\n画布（EMU）：" + _json(dict(size.attrib) if size is not None else {}) + "\n\n"
    text = header + "\n".join(blocks)
    manifest = {"schema": SCHEMA, "source_sha256": digest(blob), "source_filename": source.name,
                "native_sha256": digest(text.encode()), "header": header, "pages": pages}
    return text, manifest


def prepare(source: Path, directory: Path) -> dict:
    """Stage native evidence and reuse existing local slide rendering, never remote QA."""
    import visual_qa
    directory = directory.resolve()
    if not directory.is_relative_to((REPO / "temp").resolve()):
        raise PPTXError("PPTX 暂存只允许 repo/temp")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "pptx-manifest.json"
    if path.exists():
        manifest = _read_json(path)
        if manifest.get("source_sha256") != file_hash(source):
            raise PPTXError("PPTX 原件在准备后变化，请新建事务")
    else:
        text, manifest = extract(source)
        (directory / "pptx-native.md").write_text(text, encoding="utf-8")
        path.write_text(_json(manifest) + "\n", encoding="utf-8")
    if manifest.get("schema") != SCHEMA or file_hash(directory / "pptx-native.md") != manifest["native_sha256"]:
        raise PPTXError("PPTX 原生提取产物发生变化")
    render_dir = directory / "pptx-renders"
    render_dir.mkdir(exist_ok=True)
    try:
        kind, rendered, count = visual_qa._prepare_source(source, render_dir)
    except visual_qa.VisualQAError as exc:
        raise PPTXError(str(exc)) from exc
    if count != len(manifest["pages"]):
        raise PPTXError("渲染页数与原始页数不一致（包括隐藏页），不可按错位页图复核")
    for page in manifest["pages"]:
        name = f"page-{page['number']:04d}.png"
        image = render_dir / name
        try:
            visual_qa.render_page(kind, rendered, page["number"], image, dpi=120)
        except visual_qa.VisualQAError as exc:
            raise PPTXError(str(exc)) from exc
        image_hash = file_hash(image)
        if page.get("image_sha256") and page["image_sha256"] != image_hash:
            raise PPTXError("PPTX 复核页图发生变化")
        page.update(image=name, image_sha256=image_hash)
    render_hash = file_hash(rendered)
    if manifest.get("render_sha256") and manifest["render_sha256"] != render_hash:
        raise PPTXError("PPTX 渲染 PDF 在准备后变化")
    manifest["render_sha256"] = render_hash
    path.write_text(_json(manifest) + "\n", encoding="utf-8")
    return manifest


def review_protocol(manifest: dict) -> dict:
    return {"name": REVIEW_SCHEMA, "source_sha256": manifest["source_sha256"],
            "native_sha256": manifest["native_sha256"],
            "required": ["schema", "source_sha256", "native_sha256", "reviewer", "pages"],
            "reviewer": {"kind": "agent", "name": "宿主名称", "reviewed_at": "ISO-8601"},
            "pages": [{"number": p["number"], "image_sha256": p["image_sha256"],
                       "checks": {"text": "verified", "layout": "verified", "critical_values": "verified",
                                  **({"table": "verified"} if p["objects"]["tables"] else {}),
                                  **({"visual_information": "verified"} if p["objects"]["pictures"] or p["objects"]["unsupported"] else {})},
                       "note": "对照页图核对的具体结果（必填）；数字、单位、拟议/已完成状态不混淆",
                       "supplement": "仅补充原生提取遗漏的可见信息，不改写原生文本；没有则空字符串",
                       "limitations": [{"detail": "非关键未决项；没有则空列表", "critical": False}],
                       "ocr_receipt": "可选：既有 image_ocr 的回执路径，须绑定本页图像"}
                      for p in manifest["pages"]],
            "policy": "逐页看图；原生错误不得用补充段掩盖，修正提取器后重做；关键未决项阻断。无远程授权不上传。备注与正文、现状与拟建/预期分开。"}


def validated(source: Path, directory: Path) -> tuple[str, dict, list[dict]]:
    """Validate review and all source bindings, and deterministically compile companion."""
    manifest = _read_json(directory / "pptx-manifest.json")
    review = _read_json(directory / "pptx-review.json")
    if manifest.get("schema") != SCHEMA or manifest.get("source_sha256") != file_hash(source):
        raise PPTXError("PPTX 来源哈希不一致")
    text, expected = extract(source)
    if manifest["native_sha256"] != expected["native_sha256"] or (directory / "pptx-native.md").read_text(encoding="utf-8") != text:
        raise PPTXError("PPTX 原生文本与原件不一致")
    if len(manifest["pages"]) != len(expected["pages"]) or manifest["header"] != expected["header"]:
        raise PPTXError("PPTX 页清单与原件不一致")
    for page, original in zip(manifest["pages"], expected["pages"]):
        if any(page.get(k) != v for k, v in original.items()):
            raise PPTXError("PPTX 页清单内容与原件不一致")
    if review.get("schema") != REVIEW_SCHEMA or any(review.get(k) != manifest[k] for k in ("source_sha256", "native_sha256")):
        raise PPTXError("PPTX 复核记录未绑定原件/原生文本")
    if file_hash(directory / "pptx-renders" / "source-slides.pdf") != manifest["render_sha256"]:
        raise PPTXError("PPTX 渲染 PDF 哈希不一致")
    reviewer = review.get("reviewer", {})
    if not isinstance(reviewer, dict) or reviewer.get("kind") != "agent" or not reviewer.get("name") or not reviewer.get("reviewed_at"):
        raise PPTXError("PPTX 复核须有真实宿主署名和日期，不得冒称人工复核")
    from datetime import datetime
    datetime.fromisoformat(reviewer["reviewed_at"])
    reviewed_pages = review.get("pages")
    if not isinstance(reviewed_pages, list) or len(reviewed_pages) != len(manifest["pages"]):
        raise PPTXError("PPTX 复核须完整覆盖全部页面")
    warnings, blocks, archived = [], [], []
    for page, checked in zip(manifest["pages"], reviewed_pages):
        if not isinstance(checked, dict) or checked.get("number") != page["number"]:
            raise PPTXError("PPTX 复核页码缺失、重复或错序")
        image = directory / "pptx-renders" / f"page-{page['number']:04d}.png"
        if checked.get("image_sha256") != page["image_sha256"] or file_hash(image) != page["image_sha256"]:
            raise PPTXError("PPTX 页图/复核哈希不一致")
        required = {"text", "layout", "critical_values"}
        if page["objects"]["tables"]:
            required.add("table")
        if page["objects"]["pictures"] or page["objects"]["unsupported"]:
            required.add("visual_information")
        checks = checked.get("checks", {})
        if not isinstance(checks, dict) or any(checks.get(k) != "verified" for k in required):
            raise PPTXError(f"PPTX 第 {page['number']} 页关键复核未通过")
        if not isinstance(checked.get("note"), str) or not checked["note"].strip():
            raise PPTXError("PPTX 逐页复核须记载具体核对结果")
        limitations = checked.get("limitations")
        if not isinstance(limitations, list):
            raise PPTXError("limitations 必须为列表")
        for limit in limitations:
            if not isinstance(limit, dict) or not isinstance(limit.get("detail"), str) or not limit["detail"].strip() or type(limit.get("critical")) is not bool:
                raise PPTXError("PPTX 限制项缺少 detail/critical")
            if limit["critical"]:
                raise PPTXError("PPTX 存在关键未决项，不能提交")
            warnings.append({"issue": "pptx_review_limit", "detail": f"第 {page['number']} 页：{limit['detail']}"})
        supplement = checked.get("supplement", "")
        if not isinstance(supplement, str):
            raise PPTXError("PPTX 补充转写须为字符串")
        # Optional independent OCR reuse, never initiate remote recognition here.
        record = dict(checked)
        if record.pop("ocr_receipt", None):
            import image_ocr
            receipt = image_ocr.load_receipt(REPO / checked["ocr_receipt"], image)
            record["ocr"] = {k: receipt.get(k) for k in ("schema", "backend", "model", "source", "text_sha256", "review_status")}
        archived.append(record)
        block = page["text"].rstrip()
        if supplement.strip():
            # Quote every line to avoid a model-inserted heading impersonating a slide.
            block += "\n\n### 页面可见信息补充（宿主复核转写）\n" + "\n".join("> " + line for line in supplement.strip().splitlines())
        if limitations:
            block += "\n\n### 复核限制（非原文）\n" + "\n".join("> " + item["detail"].replace("\n", " ") for item in limitations)
        blocks.append(block + "\n")
    companion = manifest["header"] + "\n".join(blocks)
    receipt = {"schema": "pptx-source-v1", "source_sha256": manifest["source_sha256"],
               "native_sha256": manifest["native_sha256"], "text_sha256": digest(companion.encode()),
               "render_sha256": manifest["render_sha256"], "reviewer": reviewer,
               "review_status": "reviewed_with_limits" if warnings else "reviewed",
               "pages": archived, "warnings": warnings}
    return companion, receipt, warnings
