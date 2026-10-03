#!/usr/bin/env python3
"""Project evidence-bound meeting updates into existing student guidance records."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

import yaml


PROTOCOL_VERSION = "student-guidance-projection-v1"
CANDIDATE_VERSION = "student-guidance-candidates-v1"
GUIDANCE_KINDS = {
    "progress", "next_step", "blocker", "decision", "milestone", "topic_change",
}
KIND_LABELS = {
    "progress": "进展",
    "next_step": "下一步",
    "blocker": "阻塞",
    "decision": "决定",
    "milestone": "里程碑",
    "topic_change": "方向变化",
}
LATEST_KINDS = {"progress", "blocker", "decision", "milestone", "topic_change"}
PERSON_LINK_RE = re.compile(r"(?m)^- 知识库人物页：\[[^]]+\]\(([^)]+)\)\s*$")
UPDATED_RE = re.compile(r"(?m)^- 更新日期：(\d{4}-\d{2}-\d{2})\s*$")


def _repo_relative(path: Path, repo: Path) -> str:
    return path.resolve().relative_to(repo.resolve()).as_posix()


def discover_student_records(repo: Path) -> dict:
    students_dir = repo / "projects" / "学生指导" / "students"
    students = []
    if students_dir.is_dir():
        for record in sorted(students_dir.glob("*.md"), key=lambda item: item.name):
            text = record.read_text(encoding="utf-8")
            match = PERSON_LINK_RE.search(text)
            people_page = ""
            if match:
                target = (record.parent / match.group(1)).resolve()
                try:
                    people_page = _repo_relative(target, repo)
                except ValueError:
                    people_page = ""
            student_key = Path(people_page).stem if people_page else f"student-record:{record.stem}"
            students.append({
                "student_key": student_key,
                "label": record.stem,
                "record": _repo_relative(record, repo),
                "people_page": people_page,
            })
    return {"protocol_version": CANDIDATE_VERSION, "students": students}


def prepare_candidate_catalog(state: dict, repo: Path) -> tuple[dict, Path]:
    extract_dir = repo / state["extract_dir"]
    extract_dir.mkdir(parents=True, exist_ok=True)
    path = extract_dir / "student-guidance-candidates.json"
    catalog = discover_student_records(repo)
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state["student_guidance_candidates"] = _repo_relative(path, repo)
    return catalog, path


def load_candidate_catalog(state: dict, repo: Path) -> dict:
    relative = str(state.get("student_guidance_candidates") or "")
    if not relative:
        return prepare_candidate_catalog(state, repo)[0]
    path = repo / relative
    if not path.is_file():
        return prepare_candidate_catalog(state, repo)[0]
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("protocol_version") != CANDIDATE_VERSION:
        raise ValueError("学生指导候选目录协议无效")
    if not isinstance(value.get("students"), list):
        raise ValueError("学生指导候选目录缺少 students")
    return value


def validate_person_updates(updates, catalog: dict) -> list[str]:
    errors = []
    if not isinstance(updates, list):
        return ["person_updates must be a list"]
    students = catalog.get("students") or []
    by_key = {str(row.get("student_key") or ""): row for row in students}
    for index, row in enumerate(updates):
        prefix = f"person_updates[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{prefix} must be an object")
            continue
        person = str(row.get("person") or "").strip()
        label = str(row.get("person_label") or "").strip()
        kind = str(row.get("kind") or "").strip()
        if person not in by_key:
            errors.append(f"{prefix}.person is not an existing student candidate: {person}")
            continue
        if label != str(by_key[person].get("label") or ""):
            errors.append(f"{prefix}.person_label does not match candidate {person}")
        if kind not in GUIDANCE_KINDS:
            errors.append(f"{prefix}.kind is unsupported: {kind}")
    return errors


def _clean_summary(text: str, limit: int = 220) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip().replace("|", "｜")
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def _max_date(existing: str, incoming: str) -> str:
    candidates = [value for value in (existing, incoming) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or "")]
    return max(candidates) if candidates else datetime.now().strftime("%Y-%m-%d")


def _meeting_title(wiki_path: Path, fallback: str) -> str:
    text = wiki_path.read_text(encoding="utf-8")
    match = re.match(r"\A---\s*\n(.*?)\n---", text, re.S)
    if not match:
        return fallback
    try:
        frontmatter = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return fallback
    return str(frontmatter.get("title") or fallback)


def _insert_meeting_block(text: str, block: str, marker: str) -> tuple[str, bool]:
    if marker in text:
        return text, False
    heading = "## 会议更新"
    match = re.search(rf"(?m)^{re.escape(heading)}\s*$", text)
    if not match:
        return text.rstrip() + f"\n\n{heading}\n\n{block}\n", True
    next_heading = re.search(r"(?m)^##\s+", text[match.end():])
    insert_at = match.end() + (next_heading.start() if next_heading else len(text[match.end():]))
    prefix = text[:insert_at].rstrip()
    suffix = text[insert_at:].lstrip("\n")
    combined = prefix + "\n\n" + block + "\n"
    if suffix:
        combined += "\n" + suffix
    return combined, True


def _update_record(text: str, *, record_path: Path, wiki_path: Path,
                   meeting_id: str, meeting_title: str, meeting_date: str,
                   student_key: str, updates: list[dict]) -> tuple[str, bool]:
    block_id = hashlib.sha256(f"{meeting_id}|{student_key}".encode("utf-8")).hexdigest()[:16]
    marker = f"<!-- student-guidance:{block_id}:start -->"
    relative_wiki = Path(os.path.relpath(wiki_path, record_path.parent)).as_posix()
    lines = [marker, f"### {meeting_date} · [{meeting_title}]({relative_wiki}#学生指导更新)"]
    for update in updates:
        evidence = ",".join(update["evidence_ids"])
        lines.append(
            f"- {KIND_LABELS[update['kind']]}：{_clean_summary(update['text'], 500)} "
            f"<!-- evidence:{evidence} -->"
        )
    lines.append(f"<!-- student-guidance:{block_id}:end -->")
    updated, inserted = _insert_meeting_block(text, "\n".join(lines), marker)
    date_match = UPDATED_RE.search(updated)
    if not date_match:
        raise ValueError(f"{record_path.name} 缺少更新日期字段")
    effective_date = _max_date(date_match.group(1), meeting_date)
    updated = UPDATED_RE.sub(f"- 更新日期：{effective_date}", updated, count=1)
    return updated, inserted


def _status_summary(updates: list[dict], kinds: set[str]) -> str:
    values = [_clean_summary(row["text"]) for row in updates if row["kind"] in kinds]
    return _clean_summary("；".join(dict.fromkeys(values)))


def _update_status(text: str, *, candidate: dict, updates: list[dict], meeting_date: str) -> str:
    label = candidate["label"]
    filename = Path(candidate["record"]).name
    prefix = f"| [{label}](../students/{filename}) |"
    lines = text.splitlines()
    row_index = next((index for index, line in enumerate(lines) if line.startswith(prefix)), None)
    if row_index is None:
        raise ValueError(f"总览缺少学生条目: {label}")
    columns = [part.strip() for part in lines[row_index].split("|")]
    if len(columns) != 8:
        raise ValueError(f"总览学生条目列数异常: {label}")
    latest = _status_summary(updates, LATEST_KINDS)
    next_step = _status_summary(updates, {"next_step"})
    if latest:
        columns[4] = latest
    if next_step:
        columns[5] = next_step
    columns[6] = _max_date(columns[6], meeting_date)
    lines[row_index] = "| " + " | ".join(columns[1:7]) + " |"
    header_pattern = re.compile(r"^- 更新日期：(\d{4}-\d{2}-\d{2})$")
    for index, line in enumerate(lines):
        match = header_pattern.match(line)
        if match:
            lines[index] = f"- 更新日期：{_max_date(match.group(1), meeting_date)}"
            break
    else:
        raise ValueError("学生指导总览缺少更新日期字段")
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _atomic_write(path: Path, text: str) -> None:
    mode = path.stat().st_mode
    with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
            delete=False) as handle:
        handle.write(text)
        temp_path = Path(handle.name)
    os.chmod(temp_path, mode)
    os.replace(temp_path, path)


def apply_projection(state: dict, repo: Path) -> tuple[bool, str]:
    meeting_ir = state.get("meeting_ir_content") or {}
    updates = meeting_ir.get("person_updates") or []
    try:
        catalog = load_candidate_catalog(state, repo)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return False, f"学生指导候选目录读取失败: {exc}"
    errors = validate_person_updates(updates, catalog)
    if errors:
        return False, "; ".join(errors[:5])
    report = {
        "protocol_version": PROTOCOL_VERSION,
        "meeting_id": state.get("meeting_id", ""),
        "updated_records": [],
        "unchanged_records": [],
        "update_count": len(updates),
    }
    if not updates:
        state["student_guidance_report"] = report
        return _write_report(state, repo, report)

    wiki_relative = str(state.get("wiki_path") or "") + ".md"
    wiki_path = repo / wiki_relative
    if not wiki_path.is_file():
        return False, f"会议 Wiki 尚未落位: {wiki_relative}"
    status_path = repo / "projects" / "学生指导" / "notes" / "status.md"
    if not status_path.is_file():
        return False, "学生指导总览不存在"
    meeting_id = str(state.get("meeting_id") or "")
    meeting_date = str(state.get("date") or datetime.now().strftime("%Y-%m-%d"))
    meeting_title = _meeting_title(wiki_path, meeting_id)
    by_key = {row["student_key"]: row for row in catalog["students"]}
    grouped: dict[str, list[dict]] = {}
    for update in updates:
        grouped.setdefault(update["person"], []).append(update)

    originals: dict[Path, str] = {status_path: status_path.read_text(encoding="utf-8")}
    changes: dict[Path, str] = {}
    status_text = originals[status_path]
    try:
        for student_key, student_updates in grouped.items():
            candidate = by_key[student_key]
            record_path = repo / candidate["record"]
            students_root = (repo / "projects" / "学生指导" / "students").resolve()
            if not record_path.is_file() or students_root not in record_path.resolve().parents:
                raise ValueError(f"学生记录路径无效: {candidate['record']}")
            original = record_path.read_text(encoding="utf-8")
            originals[record_path] = original
            changed, inserted = _update_record(
                original, record_path=record_path, wiki_path=wiki_path,
                meeting_id=meeting_id, meeting_title=meeting_title,
                meeting_date=meeting_date, student_key=student_key,
                updates=student_updates,
            )
            if changed != original:
                changes[record_path] = changed
            report["updated_records" if inserted else "unchanged_records"].append(candidate["record"])
            status_text = _update_status(
                status_text, candidate=candidate, updates=student_updates,
                meeting_date=meeting_date,
            )
        if status_text != originals[status_path]:
            changes[status_path] = status_text
    except (OSError, ValueError, KeyError) as exc:
        return False, f"学生指导投影预检失败: {exc}"

    written: list[Path] = []
    try:
        for path, content in changes.items():
            _atomic_write(path, content)
            written.append(path)
    except OSError as exc:
        for path in reversed(written):
            try:
                _atomic_write(path, originals[path])
            except OSError:
                pass
        return False, f"学生指导投影写入失败: {exc}"
    state["student_guidance_report"] = report
    return _write_report(state, repo, report)


def _write_report(state: dict, repo: Path, report: dict) -> tuple[bool, str]:
    try:
        path = repo / state["extract_dir"] / "student-guidance-report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        state["student_guidance_report_path"] = _repo_relative(path, repo)
    except (OSError, KeyError, ValueError) as exc:
        return False, f"学生指导投影报告写入失败: {exc}"
    return True, ""
