#!/usr/bin/env python3
"""Frontier：独立于事实层的单一 Question Page、库内回答与轨迹管理器。

Markdown 是主数据；academic/frontier/frontier.db 仅为可重建的 FTS/稀疏导航索引。
Frontier 只读 Raw/Wiki/cross-domain/graph.db，并以 fact_links 单向引用它们。
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".scripts"
sys.path.insert(0, str(SCRIPTS))

import graph_lib as gl
import query_actions as qa
import source_locator as sl

DEFAULT_ROOT = REPO / "academic" / "frontier"

# ``intake``/``thread`` 仅用于读取迁移前记录；新问题统一为 Question Page。
KINDS = {"question", "trajectory", "intake", "thread"}
STATUSES = {"captured", "triaged", "active", "parked", "resolved", "rejected"}
KB_STATES = {"unassessed", "no_evidence", "no_answer_found", "partial", "conflicting", "answered"}
SCIENTIFIC_STATES = {"unverified", "likely_open", "partially_resolved", "contested", "likely_resolved", "resolved"}
ORIGIN_KINDS = {"user_proposed", "paper_explicit", "ai_synthesis", "ai_inference"}
EPISTEMIC = {"sourced", "synthesized", "derived", "speculative", "untested", "supported", "refuted", "inconclusive"}
ENTRY_KINDS = {
    "partial_answer", "candidate_answer", "residual_gap", "hypothesis", "approach",
    "prediction", "test_plan", "test_result", "critique", "status_change",
    "question_raised", "concept_introduced", "method_introduced", "scope_extended",
    "limitation_identified", "branch_created", "approaches_merged", "result_challenged",
    "problem_reframed", "problem_partially_resolved", "problem_resolved", "problem_reopened",
    "kb_refresh_candidate",
}
EDGE_PREDICATES = {
    "part_of", "refines", "depends_on", "branches_from", "merges_into", "challenges",
    "partially_answers", "reopens", "tested_by", "supersedes", "promoted_to",
    "merged_into", "related_to", "supported_by", "answered_by",
}
FACT_RELATIONS = {"grounded_in", "raised_by", "about", "motivated_by", "answered_by", "supported_by"}

QUESTION_CUES = re.compile(
    r"open question|open problem|future work|future research|remains? (?:an? )?open|"
    r"remains? unclear|not yet (?:known|understood|resolved)|further (?:work|research)|"
    r"leave .*? for future|it would be interesting|开放问题|未来工作|未来研究|"
    r"仍(?:然)?不清楚|尚未(?:解决|理解|明确)|有待(?:研究|解决|验证)|值得进一步",
    re.I,
)

ENUMERATION_CUE = re.compile(
    r"^(?:first|second|third|fourth|finally|additionally|moreover|"
    r"one direction(?: is(?: to)?)?|another direction(?: is(?: to)?)?|"
    r"a second direction(?: is(?: to)?)?|a third direction(?: is(?: to)?)?|"
    r"首先|其次|再次|最后|此外|另一方面)\b[\s,:，：-]*", re.I,
)


def explicit_question_units(text: str) -> list[str]:
    """把一行 future-work/limitation 段确定性拆成独立问题单元。"""
    compact = re.sub(r"\s+", " ", text.strip())
    if not QUESTION_CUES.search(compact):
        return []
    parts = re.split(
        r"(?<=[.!?。！？])\s+(?=[A-Z0-9一二三四五六七八九十])|"
        r"\s+(?=(?:First|Second|Third|Fourth|Finally|Additionally|Moreover|Another|"
        r"首先|其次|再次|最后|此外|另一方面)\b[,:，：-]?)",
        compact,
        flags=re.I,
    )
    units, expect_next, in_future_scope = [], False, False
    for part in parts:
        part = part.strip()
        if not part:
            continue
        has_cue = bool(QUESTION_CUES.search(part))
        generic_intro = bool(re.fullmatch(
            r"(?:future work|future research|未来工作|未来研究)[.:：。]?", part, re.I
        ) or re.search(
            r"(?:acknowledge|discuss).{0,45}(?:limitations?|opportunities).{0,30}future work[.:]?$",
            part, re.I,
        ))
        if generic_intro:
            in_future_scope = True
            expect_next = bool(re.fullmatch(
                r"(?:future work|future research|未来工作|未来研究)[.:：。]?", part, re.I
            ))
            continue
        marker = ENUMERATION_CUE.match(part)
        cleaned = ENUMERATION_CUE.sub("", part).strip()
        proposal = bool(re.search(
            r"^(?:explore|determine|investigate|extend|apply|incorporate|improve|reduce|balance|evaluate)\b|"
            r"\b(?:could|may|should|would|need(?:s|ed)? to|remains? to be|"
            r"room (?:to improve|for improvement)|to (?:explore|determine|investigate|extend|apply|incorporate|"
            r"improve|reduce|balance|evaluate))\b|有待|需要进一步|可(?:用于|纳入|扩展|改进)",
            cleaned, re.I,
        )) and not re.search(r"\bcould be useful\b", cleaned, re.I)
        direction_marker = bool(marker and "direction" in marker.group(0).lower())
        limitation_marker = bool(marker and re.search(
            r"trade-?off|limitation|gap|未解决|权衡|局限", part, re.I
        ))
        if has_cue or direction_marker or limitation_marker or expect_next or (in_future_scope and proposal):
            if len(cleaned) >= 18:
                units.append(cleaned)
        expect_next = False
    return list(dict.fromkeys(units))


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def print_json(value) -> int:
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


def resolve_root(value: str | None) -> Path:
    if not value:
        return DEFAULT_ROOT
    path = Path(value)
    return path if path.is_absolute() else REPO / path


def ensure_layout(root: Path) -> None:
    for name in ("questions", "trajectories", "intake", "threads"):
        (root / name).mkdir(parents=True, exist_ok=True)


def db_path(root: Path) -> Path:
    return root / "frontier.db"


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS records (
      id TEXT PRIMARY KEY,
      kind TEXT NOT NULL,
      title TEXT NOT NULL,
      question TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL,
      origin_kind TEXT NOT NULL,
      kb_state TEXT NOT NULL,
      scientific_state TEXT NOT NULL,
      normalized_key TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      source_file TEXT NOT NULL UNIQUE,
      body TEXT NOT NULL DEFAULT '',
      possibly_stale INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_frontier_records_kind_status ON records(kind,status);
    CREATE INDEX IF NOT EXISTS idx_frontier_records_normalized ON records(normalized_key);
    CREATE TABLE IF NOT EXISTS entries (
      id TEXT PRIMARY KEY,
      parent_id TEXT NOT NULL,
      kind TEXT NOT NULL,
      content TEXT NOT NULL,
      origin_kind TEXT NOT NULL,
      epistemic_status TEXT NOT NULL,
      review_status TEXT NOT NULL,
      created_at TEXT NOT NULL,
      evidence_json TEXT NOT NULL DEFAULT '[]'
    );
    CREATE INDEX IF NOT EXISTS idx_frontier_entries_parent ON entries(parent_id);
    CREATE TABLE IF NOT EXISTS frontier_edges (
      subject TEXT NOT NULL,
      predicate TEXT NOT NULL,
      object TEXT NOT NULL,
      epistemic_status TEXT NOT NULL,
      evidence_json TEXT NOT NULL DEFAULT '[]',
      source_record TEXT NOT NULL,
      PRIMARY KEY(subject,predicate,object,source_record)
    );
    CREATE INDEX IF NOT EXISTS idx_frontier_edges_subject ON frontier_edges(subject);
    CREATE INDEX IF NOT EXISTS idx_frontier_edges_object ON frontier_edges(object);
    CREATE TABLE IF NOT EXISTS fact_links (
      record_id TEXT NOT NULL,
      target_kind TEXT NOT NULL,
      target_path TEXT NOT NULL,
      relation TEXT NOT NULL,
      PRIMARY KEY(record_id,target_kind,target_path,relation)
    );
    CREATE INDEX IF NOT EXISTS idx_frontier_fact_target ON fact_links(target_path);
    CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(id UNINDEXED,title,question,body)")
    except sqlite3.OperationalError:
        pass


def connect_index(root: Path) -> sqlite3.Connection:
    ensure_layout(root)
    conn = sqlite3.connect(db_path(root))
    conn.row_factory = sqlite3.Row
    _init_db(conn)
    return conn


def normalize_text(text: str) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", (text or "").casefold())


def make_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:6]}"


def record_dir(root: Path, kind: str) -> Path:
    return root / {
        "question": "questions", "trajectory": "trajectories",
        "intake": "intake", "thread": "threads",
    }[kind]


def record_path(root: Path, kind: str, record_id: str) -> Path:
    return record_dir(root, kind) / f"{record_id}.md"


def parse_record(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"Frontier 记录缺 YAML frontmatter: {path}")
    parts = text.split("---\n", 2)
    if len(parts) != 3:
        raise ValueError(f"Frontier frontmatter 未闭合: {path}")
    data = yaml.safe_load(parts[1]) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Frontier frontmatter 必须为对象: {path}")
    return data, parts[2].lstrip("\n")


def _bullets(values) -> str:
    items = [str(v).strip() for v in (values or []) if str(v).strip()]
    return "\n".join(f"- {v}" for v in items) if items else "- （无）"


def render_body(record: dict) -> str:
    title = record.get("title") or record.get("question") or record["id"]
    lines = [f"# {title}", ""]
    if record["kind"] in {"question", "intake", "thread"}:
        lines += ["## 核心问题", "", record.get("question", ""), ""]
        mentions = record.get("source_mentions") or []
        if mentions:
            lines += ["## 来源表述", ""]
            for mention in mentions:
                lines.append(f"- {mention.get('text', '').strip()} — {mention.get('locator', '')}")
            lines.append("")
        elif record.get("original_question"):
            lines += ["## 来源表述", "", record["original_question"], ""]
    else:
        lines += ["## 轨迹范围", "", record.get("scope", ""), ""]
    if record["kind"] != "trajectory":
        lines += [
            "## 本知识库当前回答", "",
            record.get("kb_summary") or "尚未尝试回答。", "",
            "## 回答范围", "", record.get("coverage_note") or "仅评估当前 WikiGraph。", "",
            "## 残余缺口", "", _bullets(record.get("residual_gaps")), "",
            "## 保留价值", "", record.get("value_reason") or "待审查。", "",
        ]
    entries = record.get("entries") or []
    if entries:
        lines += ["## 条目", ""]
        for entry in entries:
            lines += [
                f"### {entry.get('id', '')} · {entry.get('kind', '')}", "",
                entry.get("content", ""), "",
                f"- 来源：{entry.get('origin_kind', '')}",
                f"- 认识论状态：{entry.get('epistemic_status', '')}",
                f"- 审查：{entry.get('review_status', 'candidate')}",
                f"- 证据：{', '.join(entry.get('evidence') or []) or '无'}", "",
            ]
    lines += ["## 事实锚点", ""]
    anchors = record.get("anchors") or {}
    for kind in ("raw", "wiki", "graph"):
        lines.append(f"### {kind}")
        lines.append("")
        lines.append(_bullets(anchors.get(kind)))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def validate_record(record: dict) -> None:
    required = {"id", "kind", "title", "status", "origin_kind", "kb_state", "scientific_state", "created_at", "updated_at"}
    missing = sorted(required - set(record))
    if missing:
        raise ValueError(f"记录缺字段: {missing}")
    if record["kind"] not in KINDS:
        raise ValueError(f"非法 kind: {record['kind']}")
    if record["status"] not in STATUSES:
        raise ValueError(f"非法 status: {record['status']}")
    if record["kb_state"] not in KB_STATES:
        raise ValueError(f"非法 kb_state: {record['kb_state']}")
    if record["scientific_state"] not in SCIENTIFIC_STATES:
        raise ValueError(f"非法 scientific_state: {record['scientific_state']}")
    if record["origin_kind"] not in ORIGIN_KINDS:
        raise ValueError(f"非法 origin_kind: {record['origin_kind']}")
    for entry in record.get("entries") or []:
        if entry.get("kind") not in ENTRY_KINDS:
            raise ValueError(f"非法 entry kind: {entry.get('kind')}")
        if entry.get("epistemic_status") not in EPISTEMIC:
            raise ValueError(f"非法 epistemic_status: {entry.get('epistemic_status')}")
        if entry.get("epistemic_status") == "sourced" and not any("#" in str(v) for v in entry.get("evidence") or []):
            raise ValueError("sourced 条目必须有 Raw locator")


def write_record(root: Path, record: dict) -> Path:
    validate_record(record)
    ensure_layout(root)
    path = record_path(root, record["kind"], record["id"])
    body = render_body(record)
    front = yaml.safe_dump(record, allow_unicode=True, sort_keys=False, width=1000)
    path.write_text(f"---\n{front}---\n\n{body}", encoding="utf-8")
    return path


def iter_record_files(root: Path):
    # legacy 目录只读兼容；migrate-questions 会将其安全移动到 questions/。
    for name in ("questions", "intake", "threads", "trajectories"):
        folder = root / name
        if folder.exists():
            yield from sorted(folder.glob("*.md"))


def load_records(root: Path) -> dict[str, tuple[dict, str, Path]]:
    records = {}
    for path in iter_record_files(root):
        data, body = parse_record(path)
        records[data["id"]] = (data, body, path)
    return records


def rebuild_index(root: Path) -> dict:
    ensure_layout(root)
    records = load_records(root)
    conn = connect_index(root)
    with conn:
        for table in ("records", "entries", "frontier_edges", "fact_links"):
            conn.execute(f"DELETE FROM {table}")
        try:
            conn.execute("DELETE FROM records_fts")
        except sqlite3.OperationalError:
            pass
        entry_count = edge_count = fact_count = 0
        for record_id, (record, body, path) in records.items():
            validate_record(record)
            rel = path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else str(path)
            conn.execute(
                "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record_id, record["kind"], record["title"], record.get("question", ""), record["status"],
                 record["origin_kind"], record["kb_state"], record["scientific_state"],
                 normalize_text(record.get("question") or record["title"]), record["created_at"], record["updated_at"],
                 rel, body, int(bool(record.get("possibly_stale"))),),
            )
            try:
                conn.execute("INSERT INTO records_fts VALUES (?,?,?,?)", (record_id, record["title"], record.get("question", ""), body))
            except sqlite3.OperationalError:
                pass
            for entry in record.get("entries") or []:
                entry_id = f"{record_id}:{entry['id']}"
                conn.execute(
                    "INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?)",
                    (entry_id, record_id, entry["kind"], entry.get("content", ""), entry.get("origin_kind", record["origin_kind"]),
                     entry["epistemic_status"], entry.get("review_status", "candidate"), entry.get("created_at", record["created_at"]),
                     json.dumps(entry.get("evidence") or [], ensure_ascii=False)),
                )
                entry_count += 1
            for relation in record.get("relations") or []:
                conn.execute(
                    "INSERT OR IGNORE INTO frontier_edges VALUES (?,?,?,?,?,?)",
                    (record_id, relation["predicate"], relation["object"], relation.get("epistemic_status", "derived"),
                     json.dumps(relation.get("evidence") or [], ensure_ascii=False), record_id),
                )
                edge_count += 1
            anchors = record.get("anchors") or {}
            for target_kind in ("raw", "wiki", "graph"):
                relation = "grounded_in" if target_kind == "raw" else "about"
                for target in anchors.get(target_kind) or []:
                    conn.execute("INSERT OR IGNORE INTO fact_links VALUES (?,?,?,?)", (record_id, target_kind, str(target), relation))
                    fact_count += 1
        conn.execute("INSERT OR REPLACE INTO metadata VALUES ('last_rebuilt',?)", (now_iso(),))
    conn.close()
    return {"records": len(records), "entries": entry_count, "edges": edge_count, "fact_links": fact_count}


def _parse_json_text(text: str) -> dict:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _as_locator(source: str) -> str:
    source = str(source or "").strip()
    if not source or "/raw/" not in source:
        return ""
    base, _, locator = source.partition("#")
    resolved = sl.resolve_path(base)
    if resolved is None or not resolved.is_file():
        return ""
    try:
        base = resolved.relative_to(REPO).as_posix()
    except ValueError:
        base = str(resolved)
    locator = locator or "全篇"
    if re.fullmatch(r"L\d+", locator):
        try:
            line_no = int(locator[1:])
            line_count = len(resolved.read_text(encoding="utf-8", errors="replace").splitlines())
            if not 1 <= line_no <= line_count:
                locator = "全篇"
        except Exception:
            locator = "全篇"
    elif locator != "全篇" and sl.locator_status(locator, resolved) == "missing":
        locator = "全篇"
    return f"{base}#{locator}"


def _raw_excerpt(locator: str, limit: int = 1000) -> str:
    raw, _, loc = locator.partition("#")
    path = REPO / raw
    if not path.is_file() or path.suffix.lower() not in {".md", ".txt"}:
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if re.fullmatch(r"L\d+", loc):
        idx = int(loc[1:]) - 1
        if 0 <= idx < len(lines):
            return lines[idx].strip()[:limit]
    text = "\n".join(lines)
    if loc and loc != "全篇":
        match = re.search(rf"^#{{1,6}}\s+{re.escape(loc)}\s*$", text, re.M | re.I)
        if match:
            start = match.end()
            nxt = re.search(r"^#{1,6}\s+", text[start:], re.M)
            return text[start:start + (nxt.start() if nxt else limit)].strip()[:limit]
    abstract = re.search(r"^#{1,6}\s+Abstract\s*$\n(.*?)(?=^#{1,6}\s+|\Z)", text, re.M | re.I | re.S)
    return (abstract.group(1).strip() if abstract else text.strip())[:limit]


def duplicate_candidates(root: Path, question: str, limit: int = 5) -> list[dict]:
    target = normalize_text(question)
    scored = []
    for record, _, _ in load_records(root).values():
        if record.get("kind") not in {"question", "thread"}:
            continue
        other = normalize_text(record.get("question") or record.get("title", ""))
        score = difflib.SequenceMatcher(None, target, other).ratio() if target and other else 0.0
        if score >= 0.55:
            scored.append({"id": record["id"], "title": record["title"], "status": record["status"], "score": round(score, 3)})
    return sorted(scored, key=lambda item: item["score"], reverse=True)[:limit]


def build_kb_packet(
    question: str,
    root: Path,
    topk: int = 6,
    recall_fn: Callable | None = None,
    relations_fn: Callable | None = None,
) -> dict:
    recall_fn = recall_fn or qa.wiki_recall
    relations_fn = relations_fn or qa.graph_relations
    text, _ = recall_fn(question, "academic", str(topk))
    recall = _parse_json_text(text)
    candidates = (recall.get("candidates") or [])[:topk]
    raw_locators, graph_paths = [], []
    compact_candidates = []
    for candidate in candidates:
        page = str(candidate.get("path") or "").strip()
        if not page:
            continue
        compact_candidates.append({
            "path": page,
            "title": candidate.get("title", ""),
            "navigation": str(candidate.get("navigation") or "")[:1600],
            "score": candidate.get("score", 0),
        })
        rel_text, _ = relations_fn(page)
        relations = _parse_json_text(rel_text)
        for edge in relations.get("edges") or []:
            locator = _as_locator(edge.get("source", ""))
            if locator and locator not in raw_locators:
                raw_locators.append(locator)
            for endpoint in (edge.get("subject"), edge.get("object")):
                endpoint = str(endpoint or "")
                if endpoint and endpoint != page and "/raw/" not in endpoint and endpoint not in graph_paths:
                    graph_paths.append(endpoint)
        try:
            fm = gl.read_frontmatter(page)
            for source in gl.parse_list_field(fm, "sources"):
                locator = _as_locator(source)
                if locator and locator not in raw_locators:
                    raw_locators.append(locator)
        except Exception:
            pass
    raw_locators = raw_locators[:8]
    evidence = [{"locator": loc, "excerpt": _raw_excerpt(loc)} for loc in raw_locators[:5]]
    return {
        "question": question,
        "coverage": "仅评估当前 WikiGraph；无命中不等于科学界无答案。",
        "recall_mode": recall.get("mode", "empty"),
        "candidates": compact_candidates,
        "raw_evidence": evidence,
        "anchors": {
            "raw": raw_locators,
            "wiki": [item["path"] for item in compact_candidates],
            "graph": graph_paths[:16],
        },
        "duplicate_candidates": duplicate_candidates(root, question),
    }


def assessment_schema(value) -> bool:
    if not isinstance(value, dict):
        return False
    required = {"canonical_question", "kb_state", "kb_summary", "residual_gaps", "value_reason", "academic", "specific", "recommended_disposition", "duplicate_target"}
    if not required <= set(value):
        return False
    return (
        isinstance(value["canonical_question"], str)
        and value["kb_state"] in KB_STATES - {"unassessed"}
        and isinstance(value["kb_summary"], str)
        and isinstance(value["residual_gaps"], list)
        and all(isinstance(item, str) for item in value["residual_gaps"])
        and isinstance(value["value_reason"], str)
        and isinstance(value["academic"], bool)
        and isinstance(value["specific"], bool)
        and value["recommended_disposition"] in {"new_thread", "merge", "resolved", "parked", "reject"}
        and isinstance(value["duplicate_target"], str)
    )


def assessment_prompt(packet: dict) -> str:
    compact = json.dumps(packet, ensure_ascii=False)[:14000]
    return f"""你是 Frontier 准入评估器。只能依据给定 WikiGraph 证据包判断本知识库状态，不能把无命中说成科学界未解决。
输出一个 JSON 对象，字段严格为：
canonical_question, kb_state(no_evidence/no_answer_found/partial/conflicting/answered), kb_summary,
residual_gaps(字符串数组), value_reason, academic(布尔), specific(布尔),
recommended_disposition(new_thread/merge/resolved/parked/reject), duplicate_target(无则空字符串)。
规则：已有重复 Question 优先 merge；已充分回答且无残余缺口用 resolved；只有具体、有价值且有残余缺口才 new_thread（兼容字段，表示同页转 triaged）；宁少勿多。

知识库证据包：
{compact}"""


def run_assessment(packet: dict) -> dict:
    import llm_structured as llm
    return llm.call_json(
        assessment_prompt(packet), assessment_schema,
        max_tokens=900, retries=1, operation="frontier_assess", reasoning="fast",
        system="你是受约束的研究前沿准入组件，只输出 JSON，不补充知识库外事实。",
    )


def answer_schema(value) -> bool:
    if not isinstance(value, dict):
        return False
    required = {"kb_state", "answer", "supported_claims", "derived_claims",
                "residual_gaps", "coverage_note"}
    if not required <= set(value) or value["kb_state"] not in KB_STATES - {"unassessed"}:
        return False
    claims = value["supported_claims"]
    return (
        isinstance(value["answer"], str)
        and isinstance(claims, list)
        and all(isinstance(item, dict)
                and isinstance(item.get("claim"), str)
                and isinstance(item.get("evidence"), list)
                and all(isinstance(loc, str) for loc in item["evidence"])
                for item in claims)
        and isinstance(value["derived_claims"], list)
        and all(isinstance(item, str) for item in value["derived_claims"])
        and isinstance(value["residual_gaps"], list)
        and all(isinstance(item, str) for item in value["residual_gaps"])
        and isinstance(value["coverage_note"], str)
    )


def answer_prompt(packet: dict) -> str:
    # 弱模型只看回答所需的紧凑证据，避免 navigation/excerpt 占满推理预算。
    compact_packet = {
        "question": packet.get("question", ""),
        "coverage": packet.get("coverage", ""),
        "question_sources": (packet.get("question_sources") or [])[:3],
        "candidates": [{
            "path": item.get("path", ""), "title": item.get("title", ""),
            "navigation": str(item.get("navigation", ""))[:320],
        } for item in (packet.get("candidates") or [])[:4]],
        "evidence": [{
            "locator": item.get("locator", ""),
            "excerpt": str(item.get("excerpt", ""))[:420],
        } for item in (packet.get("raw_evidence") or [])[:4]],
        "allowed_raw_locators": (packet.get("anchors", {}).get("raw") or [])[:8],
    }
    compact = json.dumps(compact_packet, ensure_ascii=False)
    return f"""你是 Frontier 的知识库内回答器。只能依据给定 WikiGraph 证据包回答，不能使用外部知识。
输出严格 JSON：kb_state(no_evidence/no_answer_found/partial/conflicting/answered), answer,
supported_claims([{{claim,evidence:[Raw locator]}}]), derived_claims(字符串数组),
residual_gaps(字符串数组), coverage_note。
规则：每条 supported_claim 必须引用证据包中已有 Raw locator；推导只能进入 derived_claims；
来源中的开放问题表述本身不是答案；无命中只表示本库覆盖不足，不能说科学界尚未解决。

知识库证据包：
{compact}"""


def run_answer(packet: dict) -> dict:
    import llm_structured as llm
    return llm.call_json(
        answer_prompt(packet), answer_schema,
        max_tokens=1800, retries=1, operation="frontier_answer", reasoning="fast",
        system="你是证据约束的库内回答组件，只输出 JSON，不补充知识库外事实。",
    )


def _merge_unique(target: list, values: list) -> None:
    for value in values:
        if value and value not in target:
            target.append(value)


def _next_entry_id(record: dict) -> str:
    numbers = []
    for entry in record.get("entries") or []:
        match = re.fullmatch(r"E-(\d+)", str(entry.get("id", "")))
        if match:
            numbers.append(int(match.group(1)))
    return f"E-{(max(numbers, default=0) + 1):04d}"


def apply_answer(root: Path, record: dict, packet: dict, answer: dict) -> dict:
    """将有证据约束的库内回答写回同一 Question Page。"""
    if not answer_schema(answer):
        raise ValueError("answer 未通过 schema")
    allowed = set(packet.get("anchors", {}).get("raw") or [])
    for claim in answer["supported_claims"]:
        evidence = claim.get("evidence") or []
        if not evidence or any(locator not in allowed or "#" not in locator for locator in evidence):
            raise ValueError("supported_claim 只能引用证据包中的 Raw locator")
    if answer["kb_state"] in {"partial", "conflicting", "answered"} and not answer["supported_claims"]:
        raise ValueError("partial/conflicting/answered 必须至少有一条 supported_claim")

    normalized = {
        "kb_state": answer["kb_state"],
        "answer": answer["answer"].strip(),
        "supported_claims": answer["supported_claims"],
        "derived_claims": [item.strip() for item in answer["derived_claims"] if item.strip()],
        "residual_gaps": [item.strip() for item in answer["residual_gaps"] if item.strip()],
        "coverage_note": answer["coverage_note"].strip(),
    }
    if not normalized["supported_claims"] and normalized["kb_state"] in {"no_evidence", "no_answer_found"}:
        normalized["answer"] = "当前知识库未检索到足以回答该问题的可定位证据。"
    fingerprint = hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    changed = fingerprint != record.get("answer_fingerprint")
    if changed:
        created = now_iso()
        entries = record.setdefault("entries", [])
        for claim in normalized["supported_claims"]:
            entries.append({
                "id": _next_entry_id(record), "kind": "partial_answer",
                "content": claim["claim"].strip(), "origin_kind": "ai_synthesis",
                "epistemic_status": "sourced", "review_status": "candidate",
                "created_at": created, "evidence": claim["evidence"],
            })
        for claim in normalized["derived_claims"]:
            entries.append({
                "id": _next_entry_id(record), "kind": "candidate_answer",
                "content": claim, "origin_kind": "ai_inference",
                "epistemic_status": "derived", "review_status": "candidate",
                "created_at": created, "evidence": [],
            })
    record["kb_state"] = normalized["kb_state"]
    record["kb_summary"] = normalized["answer"]
    record["residual_gaps"] = normalized["residual_gaps"]
    record["coverage_note"] = normalized["coverage_note"] or packet.get("coverage", "")
    record["answer_status"] = "completed"
    record["answer_checked_at"] = now_iso()
    record["answer_fingerprint"] = fingerprint
    record["possibly_stale"] = False
    # 只持久化问题来源和真正被支持结论使用的 Raw；召回候选留在 kb_candidates，
    # 不把一次搜索的低相关路径固化成 Frontier fact_links。
    mentions = record.get("source_mentions") or []
    source_raw = [item.get("locator", "") for item in mentions if item.get("locator")]
    source_wiki = [item.get("page", "") for item in mentions if item.get("page")]
    cited_raw = [locator for claim in normalized["supported_claims"] for locator in claim["evidence"]]
    record["anchors"] = {
        "raw": list(dict.fromkeys(source_raw + cited_raw)),
        "wiki": list(dict.fromkeys(source_wiki)),
        "graph": [],
    }
    record["kb_candidates"] = packet.get("candidates") or []
    record["updated_at"] = now_iso()
    write_record(root, record)
    rebuild_index(root)
    return {"id": record["id"], "status": "completed", "kb_state": record["kb_state"],
            "changed": changed, "supported_claims": len(normalized["supported_claims"])}


def answer_question(root: Path, record_id: str, topk: int = 6, *, no_ai: bool = False,
                    packet: dict | None = None, answer_fn: Callable | None = None) -> dict:
    record, _ = find_record(root, record_id)
    if record.get("kind") not in {"question", "intake", "thread"}:
        raise ValueError("answer 只接受 Question Page")
    packet = packet or build_kb_packet(record.get("question") or record["title"], root, topk)
    packet["question_sources"] = list((record.get("anchors") or {}).get("raw") or [])
    if not packet.get("candidates") and not packet.get("raw_evidence"):
        answer = {
            "kb_state": "no_evidence",
            "answer": "当前知识库未检索到足以回答该问题的证据。",
            "supported_claims": [], "derived_claims": [],
            "residual_gaps": [record.get("question") or record["title"]],
            "coverage_note": packet.get("coverage", "仅评估当前 WikiGraph。"),
        }
        return apply_answer(root, record, packet, answer)
    if no_ai:
        record["answer_status"] = "pending"
        record["answer_checked_at"] = now_iso()
        record["coverage_note"] = packet.get("coverage", "")
        record["updated_at"] = now_iso()
        write_record(root, record)
        rebuild_index(root)
        return {"id": record_id, "status": "pending", "kb_state": record["kb_state"]}
    result = (answer_fn or run_answer)(packet)
    if result.get("ok"):
        return apply_answer(root, record, packet, result["parsed"])
    record["answer_status"] = "pending"
    record["answer_checked_at"] = now_iso()
    record["updated_at"] = now_iso()
    write_record(root, record)
    rebuild_index(root)
    return {"id": record_id, "status": "pending", "kb_state": record["kb_state"],
            "reason": result.get("status", "model_unavailable")}


def new_question(question: str, origin_kind: str, packet: dict,
                 source_page: str = "", source_locator: str = "") -> dict:
    created = now_iso()
    record_id = make_id("Q")
    anchors = packet.get("anchors") or {"raw": [], "wiki": [], "graph": []}
    if source_page and source_page not in anchors.setdefault("wiki", []):
        anchors["wiki"].insert(0, source_page)
    if source_locator and source_locator not in anchors.setdefault("raw", []):
        anchors["raw"].insert(0, source_locator)
    return {
        "id": record_id,
        "kind": "question",
        "title": question.strip()[:120],
        "question": question.strip(),
        "original_question": question.strip(),
        "status": "captured",
        "origin_kind": origin_kind,
        "kb_state": "unassessed",
        "scientific_state": "unverified",
        "created_at": created,
        "updated_at": created,
        "review_status": "candidate",
        "kb_summary": "",
        "residual_gaps": [],
        "value_reason": "",
        "anchors": anchors,
        "kb_candidates": packet.get("candidates") or [],
        "duplicate_candidates": packet.get("duplicate_candidates") or [],
        "coverage_note": packet.get("coverage", ""),
        "relations": [],
        "entries": [],
        "source_page": source_page,
        "source_locator": source_locator,
        "source_mentions": ([{
            "text": question.strip(), "page": source_page, "locator": source_locator,
        }] if source_page or source_locator else [{
            "text": question.strip(), "page": "", "locator": "",
        }]),
        "answer_status": "pending",
        "possibly_stale": False,
    }


def new_intake(question: str, origin_kind: str, packet: dict,
               source_page: str = "", source_locator: str = "") -> dict:
    """Legacy API alias：新记录始终写为单一 Question Page。"""
    return new_question(question, origin_kind, packet, source_page, source_locator)


def _thread_gate(record: dict, assessment: dict) -> list[str]:
    errors = []
    if not assessment.get("academic"):
        errors.append("非学术问题")
    if not assessment.get("specific"):
        errors.append("问题不够具体")
    if not assessment.get("value_reason", "").strip():
        errors.append("缺价值理由")
    if not [gap for gap in assessment.get("residual_gaps") or [] if str(gap).strip()]:
        errors.append("缺残余科学问题")
    if any(item.get("score", 0) >= 0.92 for item in record.get("duplicate_candidates") or []):
        errors.append("存在高相似 Question，须先合并审查")
    return errors


def create_thread_from_intake(root: Path, intake: dict) -> dict:
    """Legacy API alias：准入只提升同一 Question Page，不再复制 Thread。"""
    intake["kind"] = "question"
    intake["status"] = "triaged"
    intake.setdefault("human_reviewed", False)
    intake["updated_at"] = now_iso()
    write_record(root, intake)
    return intake


def apply_assessment(root: Path, intake: dict, assessment: dict, promote: bool = True) -> dict:
    if not assessment_schema(assessment):
        raise ValueError("assessment 未通过 schema")
    intake["canonical_question"] = assessment["canonical_question"].strip() or intake["question"]
    intake["title"] = intake["canonical_question"][:120]
    intake["kb_state"] = assessment["kb_state"]
    intake["kb_summary"] = assessment["kb_summary"].strip()
    intake["residual_gaps"] = [item.strip() for item in assessment["residual_gaps"] if item.strip()]
    intake["value_reason"] = assessment["value_reason"].strip()
    intake["assessment"] = {key: assessment[key] for key in ("academic", "specific", "recommended_disposition", "duplicate_target")}
    intake["updated_at"] = now_iso()
    disposition = assessment["recommended_disposition"]
    gate_errors = []
    records = load_records(root)
    duplicate_target = assessment.get("duplicate_target", "")
    if disposition == "merge" and duplicate_target in records and records[duplicate_target][0].get("kind") in {"question", "thread"}:
        intake["status"] = "triaged"
        intake.setdefault("relations", []).append({"predicate": "merged_into", "object": duplicate_target, "epistemic_status": "synthesized", "evidence": []})
    elif disposition == "resolved":
        intake["status"] = "resolved"
    elif disposition == "reject":
        intake["status"] = "rejected"
    elif disposition == "parked":
        intake["status"] = "parked"
    elif disposition == "new_thread":
        gate_errors = _thread_gate(intake, assessment)
        intake["status"] = "triaged" if not gate_errors else "captured"
        if promote and not gate_errors:
            intake["kind"] = "question"
            intake.setdefault("human_reviewed", False)
    write_record(root, intake)
    rebuild_index(root)
    return {
        "question_id": intake["id"], "intake_id": intake["id"],
        "thread_id": intake["id"] if intake["status"] == "triaged" else "",
        "status": intake["status"], "gate_errors": gate_errors,
    }


def find_record(root: Path, record_id: str) -> tuple[dict, Path]:
    item = load_records(root).get(record_id)
    if not item:
        raise ValueError(f"Frontier 记录不存在: {record_id}")
    return item[0], item[2]


def mark_stale_for_targets(root: Path, targets: set[str], exclude: set[str] | None = None) -> list[str]:
    changed = []
    exclude = exclude or set()
    for record, _, _ in load_records(root).values():
        if record.get("id") in exclude:
            continue
        if record.get("kind") not in {"question", "thread", "trajectory"}:
            continue
        anchors = record.get("anchors") or {}
        flat = {str(value) for values in anchors.values() for value in (values or [])}
        if flat & targets:
            record["possibly_stale"] = True
            record["updated_at"] = now_iso()
            record.setdefault("stale_reasons", []).extend(sorted((flat & targets) - set(record.get("stale_reasons") or [])))
            write_record(root, record)
            changed.append(record["id"])
    return changed


def extract_paper_candidates(root: Path, page: str, limit: int = 3) -> dict:
    ensure_layout(root)
    page_path = Path(page)
    if not page_path.is_absolute():
        page_path = REPO / page_path
    if page_path.suffix != ".md":
        page_path = page_path.with_suffix(".md")
    if not page_path.is_file():
        raise ValueError(f"Wiki 页面不存在: {page}")
    page_rel = page_path.relative_to(REPO).with_suffix("").as_posix()
    fm = gl.read_frontmatter(page_rel)
    raw_path = None
    raw_rel = ""
    for source in gl.parse_list_field(fm, "sources"):
        base = source.split("#", 1)[0]
        candidate = REPO / base
        if candidate.is_file() and candidate.suffix.lower() in {".md", ".txt"}:
            raw_path, raw_rel = candidate, base
            break
    if raw_path is None:
        return {"page": page_rel, "captured": [], "skipped": "无可读 Raw Markdown/TXT", "stale_records": []}
    existing = load_records(root)
    existing_keys = {(rec.get("source_page"), rec.get("source_locator"), normalize_text(rec.get("question", ""))) for rec, _, _ in existing.values()}
    captured, reused = [], []
    reached_limit = False
    for line_no, line in enumerate(raw_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        stripped_line = re.sub(r"\s+", " ", line.strip())
        if re.match(r"^#{1,6}\s+(references|参考文献)", stripped_line, re.I):
            break
        if not 18 <= len(stripped_line) <= 4000:
            continue
        for stripped in explicit_question_units(stripped_line):
            locator = f"{raw_rel}#L{line_no}"
            key = (page_rel, locator, normalize_text(stripped))
            if key in existing_keys:
                continue
            exact = [rec for rec, _, _ in existing.values()
                     if rec.get("kind") in {"question", "intake", "thread"}
                     and normalize_text(rec.get("question", "")) == normalize_text(stripped)]
            if len(exact) == 1:
                question = exact[0]
                mention = {"text": stripped, "page": page_rel, "locator": locator}
                if mention not in question.setdefault("source_mentions", []):
                    question["source_mentions"].append(mention)
                _merge_unique(question.setdefault("anchors", {}).setdefault("raw", []), [locator])
                _merge_unique(question["anchors"].setdefault("wiki", []), [page_rel])
                question["possibly_stale"] = True
                question["updated_at"] = now_iso()
                write_record(root, question)
                reused.append(question["id"])
                existing_keys.add(key)
            else:
                packet = {"anchors": {"raw": [locator], "wiki": [page_rel], "graph": []}, "candidates": [], "duplicate_candidates": [], "coverage": "论文作者明示候选；尚未评估当前科学状态。"}
                question = new_question(stripped, "paper_explicit", packet, page_rel, locator)
                question["value_reason"] = "论文作者明示的开放问题、局限或 future work，待审查其研究价值。"
                write_record(root, question)
                captured.append(question["id"])
                existing[question["id"]] = (question, "", record_path(root, "question", question["id"]))
                existing_keys.add(key)
            if len(captured) + len(reused) >= max(1, min(limit, 5)):
                reached_limit = True
                break
        if reached_limit:
            break
    targets = {page_rel}
    rel_text, _ = qa.graph_relations(page_rel)
    for edge in _parse_json_text(rel_text).get("edges") or []:
        targets.update(str(edge.get(key) or "") for key in ("subject", "object") if edge.get(key))
    stale = mark_stale_for_targets(root, targets, set(captured + reused))
    rebuild_index(root)
    return {"page": page_rel, "captured": captured, "reused": reused,
            "count": len(captured), "stale_records": stale}


def cmd_init(args) -> int:
    root = resolve_root(args.root)
    ensure_layout(root)
    return print_json({"status": "initialized", "root": str(root), "index": rebuild_index(root)})


def cmd_rebuild(args) -> int:
    root = resolve_root(args.root)
    return print_json({"status": "rebuilt", "root": str(root), "index": rebuild_index(root)})


def cmd_ask(args) -> int:
    root = resolve_root(args.root)
    question = args.question.strip()
    if len(normalize_text(question)) < 3:
        raise ValueError("问题过短，无法形成学术问题记录")
    packet = build_kb_packet(question, root, args.topk)
    question_page = new_question(question, args.origin, packet)
    write_record(root, question_page)
    answer_result = answer_question(root, question_page["id"], args.topk,
                                    no_ai=args.no_ai, packet=packet)
    question_page, _ = find_record(root, question_page["id"])
    assessment_result = None
    applied = None
    if args.assessment_file:
        assessment = json.loads(Path(args.assessment_file).read_text(encoding="utf-8"))
        applied = apply_assessment(root, question_page, assessment, promote=not args.no_promote)
    elif not args.no_ai:
        assessment_result = run_assessment(packet)
        if assessment_result.get("ok"):
            applied = apply_assessment(root, question_page, assessment_result["parsed"], promote=not args.no_promote)
        else:
            question_page["assessment_status"] = assessment_result.get("status", "failed")
            question_page["updated_at"] = now_iso()
            write_record(root, question_page)
            rebuild_index(root)
    result = {
        "status": applied["status"] if applied else "captured",
        "question_id": question_page["id"],
        "intake_id": question_page["id"],
        "thread_id": (applied or {}).get("thread_id", ""),
        "answer": answer_result,
        "kb_candidates": len(packet["candidates"]),
        "raw_evidence": [item["locator"] for item in packet["raw_evidence"]],
        "duplicate_candidates": packet["duplicate_candidates"],
        "gate_errors": (applied or {}).get("gate_errors", []),
    }
    if assessment_result and not assessment_result.get("ok"):
        result["assessment"] = {
            "status": assessment_result.get("status"),
            "error": assessment_result.get("error", ""),
            "prompt": assessment_result.get("prompt", "") if assessment_result.get("status") == "agent_required" else "",
        }
    return print_json(result)


def cmd_assess(args) -> int:
    root = resolve_root(args.root)
    intake, _ = find_record(root, args.record_id)
    if intake["kind"] not in {"question", "intake", "thread"}:
        raise ValueError("assess 只接受 Question Page")
    assessment = json.loads(Path(args.assessment_file).read_text(encoding="utf-8"))
    return print_json(apply_assessment(root, intake, assessment, promote=not args.no_promote))


def cmd_review(args) -> int:
    root = resolve_root(args.root)
    record, _ = find_record(root, args.record_id)
    if args.status == "active":
        if record["kind"] not in {"question", "thread", "trajectory"}:
            raise ValueError("只有 Question/Trajectory 可 active")
        if record["kind"] in {"question", "thread"} and (not record.get("value_reason") or not record.get("residual_gaps")):
            raise ValueError("Question 缺价值理由或残余缺口，不得 active")
        if record["kind"] == "trajectory" and not record.get("entries"):
            raise ValueError("Trajectory 无事件，不得 active")
        record["human_reviewed"] = True
        record["reviewed_by"] = args.reviewer or "user"
        record["reviewed_at"] = now_iso()
    record["status"] = args.status
    record["review_status"] = "reviewed"
    record["updated_at"] = now_iso()
    write_record(root, record)
    rebuild_index(root)
    return print_json({"id": record["id"], "status": record["status"], "human_reviewed": bool(record.get("human_reviewed"))})


def cmd_list(args) -> int:
    root = resolve_root(args.root)
    rebuild_index(root)
    conn = connect_index(root)
    sql = "SELECT id,kind,title,status,kb_state,scientific_state,possibly_stale,updated_at FROM records"
    clauses, params = [], []
    if args.kind:
        clauses.append("kind=?"); params.append(args.kind)
    if args.status:
        clauses.append("status=?"); params.append(args.status)
    elif not args.all:
        clauses.append("status IN ('triaged','active')")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC LIMIT ?"; params.append(args.limit)
    rows = [dict(row) for row in conn.execute(sql, params)]
    conn.close()
    return print_json({"count": len(rows), "records": rows})


def cmd_search(args) -> int:
    root = resolve_root(args.root)
    rebuild_index(root)
    conn = connect_index(root)
    tokens = re.findall(r"[A-Za-z0-9_-]+|[\u3400-\u9fff]+", args.query)
    rows = []
    if tokens:
        try:
            query = " OR ".join('"' + token.replace('"', '') + '"' for token in tokens[:8])
            rows = [dict(row) for row in conn.execute(
                "SELECT r.id,r.kind,r.title,r.status,r.kb_state FROM records_fts f JOIN records r ON r.id=f.id WHERE records_fts MATCH ? LIMIT ?",
                (query, args.limit),
            )]
        except sqlite3.OperationalError:
            rows = []
    if not rows:
        like = f"%{args.query}%"
        rows = [dict(row) for row in conn.execute(
            "SELECT id,kind,title,status,kb_state FROM records WHERE title LIKE ? OR question LIKE ? OR body LIKE ? LIMIT ?",
            (like, like, like, args.limit),
        )]
    conn.close()
    return print_json({"query": args.query, "count": len(rows), "records": rows})


def cmd_show(args) -> int:
    root = resolve_root(args.root)
    record, path = find_record(root, args.record_id)
    rel = path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else str(path)
    return print_json({"record": record, "source_file": rel})


def cmd_capture_paper(args) -> int:
    root = resolve_root(args.root)
    result = extract_paper_candidates(root, args.page, args.limit)
    attempts = []
    if not args.no_answer:
        for record_id in list(dict.fromkeys(result["captured"] + result.get("reused", [])))[:args.limit]:
            try:
                attempts.append(answer_question(root, record_id, args.topk))
            except Exception as exc:
                # 回答是非阻断后处理；Question Page 已保存，失败只保持 pending。
                record, _ = find_record(root, record_id)
                record["answer_status"] = "pending"
                record["answer_checked_at"] = now_iso()
                record["updated_at"] = now_iso()
                write_record(root, record)
                attempts.append({"id": record_id, "status": "pending",
                                 "reason": type(exc).__name__})
        rebuild_index(root)
    result["answer_attempts"] = attempts
    return print_json(result)


def cmd_add_trajectory(args) -> int:
    root = resolve_root(args.root)
    events = []
    if args.events_file:
        events = json.loads(Path(args.events_file).read_text(encoding="utf-8"))
        if not isinstance(events, list):
            raise ValueError("events-file 必须是 JSON 数组")
    created = now_iso()
    trajectory = {
        "id": make_id("T"), "kind": "trajectory", "title": args.title.strip(), "question": "",
        "scope": args.scope.strip(), "status": "triaged" if events else "captured",
        "origin_kind": args.origin, "kb_state": "unassessed", "scientific_state": "unverified",
        "created_at": created, "updated_at": created, "review_status": "candidate", "human_reviewed": False,
        "anchors": {"raw": args.raw_anchor or [], "wiki": args.wiki_anchor or [], "graph": args.graph_anchor or []},
        "relations": [{"predicate": "related_to", "object": value, "epistemic_status": "sourced", "evidence": []} for value in (args.thread or [])],
        "entries": events, "possibly_stale": False,
    }
    write_record(root, trajectory)
    rebuild_index(root)
    return print_json({"id": trajectory["id"], "status": trajectory["status"], "events": len(events)})


def cmd_add_entry(args) -> int:
    root = resolve_root(args.root)
    record, _ = find_record(root, args.record_id)
    if record["kind"] not in {"question", "thread", "trajectory"}:
        raise ValueError("只有 Question/Trajectory 可追加 entry")
    evidence = args.evidence or []
    if args.epistemic == "sourced" and not any("#" in item for item in evidence):
        raise ValueError("sourced entry 必须提供 Raw locator")
    next_no = len(record.get("entries") or []) + 1
    entry = {
        "id": f"E-{next_no:04d}", "kind": args.kind, "content": args.content.strip(),
        "origin_kind": args.origin, "epistemic_status": args.epistemic,
        "review_status": "candidate", "created_at": now_iso(), "evidence": evidence,
    }
    record.setdefault("entries", []).append(entry)
    record["updated_at"] = now_iso()
    write_record(root, record)
    rebuild_index(root)
    return print_json({"record_id": record["id"], "entry": entry})


def cmd_link(args) -> int:
    root = resolve_root(args.root)
    subject, _ = find_record(root, args.subject)
    find_record(root, args.object)
    if args.predicate not in EDGE_PREDICATES:
        raise ValueError(f"非法 Frontier predicate: {args.predicate}")
    evidence = args.evidence or []
    if args.epistemic == "sourced" and args.predicate in {"challenges", "partially_answers", "reopens", "supported_by", "answered_by"} and not any("#" in item for item in evidence):
        raise ValueError("事实性 Frontier 关系必须有 Raw locator")
    relation = {"predicate": args.predicate, "object": args.object, "epistemic_status": args.epistemic, "evidence": evidence}
    if not any(item.get("predicate") == args.predicate and item.get("object") == args.object for item in subject.get("relations") or []):
        subject.setdefault("relations", []).append(relation)
        subject["updated_at"] = now_iso()
        write_record(root, subject)
    rebuild_index(root)
    return print_json({"subject": args.subject, "predicate": args.predicate, "object": args.object})


def migrate_question_pages(root: Path) -> dict:
    """将 legacy intake/thread 原样收敛为 questions/；不做语义自动合并。"""
    moved = []
    for record, _, old_path in list(load_records(root).values()):
        if record.get("kind") not in {"intake", "thread"}:
            continue
        record["kind"] = "question"
        record.setdefault("source_mentions", [])
        if not record["source_mentions"] and (record.get("original_question") or record.get("source_locator")):
            record["source_mentions"].append({
                "text": record.get("original_question") or record.get("question", ""),
                "page": record.get("source_page", ""),
                "locator": record.get("source_locator", ""),
            })
        record.setdefault("answer_status", "completed" if record.get("kb_summary") else "pending")
        record["updated_at"] = now_iso()
        new_path = write_record(root, record)
        if old_path != new_path and old_path.exists():
            old_path.unlink()
        moved.append({"id": record["id"], "from": str(old_path), "to": str(new_path)})
    report = rebuild_index(root)
    return {"moved": moved, "count": len(moved), "index": report}


def _reset_question_for_unit(record: dict, unit: str) -> None:
    """把历史“整段问题”收敛为一个问题单元，并使旧回答失效。"""
    record["question"] = unit
    record["canonical_question"] = unit
    record["title"] = unit[:120]
    record["kb_state"] = "unassessed"
    record["kb_summary"] = ""
    record["residual_gaps"] = []
    record["answer_status"] = "pending"
    record.pop("answer_fingerprint", None)
    record.pop("answer_checked_at", None)
    record["entries"] = [entry for entry in (record.get("entries") or [])
                         if not (entry.get("origin_kind") in {"ai_synthesis", "ai_inference"}
                                 and entry.get("kind") in {"partial_answer", "candidate_answer", "kb_refresh_candidate"})]
    mentions = record.get("source_mentions") or []
    record["anchors"] = {
        "raw": list(dict.fromkeys(item.get("locator", "") for item in mentions if item.get("locator"))),
        "wiki": list(dict.fromkeys(item.get("page", "") for item in mentions if item.get("page"))),
        "graph": [],
    }
    record["possibly_stale"] = False
    record["updated_at"] = now_iso()


def split_question_pages(root: Path) -> dict:
    """将历史一段一页确定性拆为一问题一页；不调用 LLM。"""
    changed, created = [], []
    for record, _, _ in list(load_records(root).values()):
        if record.get("kind") != "question" or record.get("origin_kind") != "paper_explicit":
            continue
        source_text = record.get("original_question") or record.get("question", "")
        units = explicit_question_units(source_text)
        if not units:
            continue
        if len(units) == 1 and normalize_text(units[0]) == normalize_text(record.get("question", "")):
            continue
        base = json.loads(json.dumps(record, ensure_ascii=False))
        _reset_question_for_unit(record, units[0])
        write_record(root, record)
        changed.append(record["id"])
        for unit in units[1:]:
            existing = [item for item, _, _ in load_records(root).values()
                        if item.get("kind") == "question"
                        and normalize_text(item.get("question", "")) == normalize_text(unit)]
            if existing:
                continue
            clone = json.loads(json.dumps(base, ensure_ascii=False))
            clone["id"] = make_id("Q")
            clone["created_at"] = now_iso()
            _reset_question_for_unit(clone, unit)
            write_record(root, clone)
            created.append(clone["id"])
    report = rebuild_index(root)
    return {"changed": changed, "created": created, "count": len(changed) + len(created),
            "index": report}


def cmd_answer(args) -> int:
    return print_json(answer_question(resolve_root(args.root), args.record_id, args.topk,
                                      no_ai=args.no_ai))


def cmd_migrate_questions(args) -> int:
    return print_json(migrate_question_pages(resolve_root(args.root)))


def cmd_split_questions(args) -> int:
    return print_json(split_question_pages(resolve_root(args.root)))


def cmd_refresh(args) -> int:
    root = resolve_root(args.root)
    record, _ = find_record(root, args.record_id)
    if record["kind"] not in {"question", "intake", "thread"}:
        raise ValueError("refresh 只接受 Question Page")
    return print_json(answer_question(root, record["id"], args.topk, no_ai=args.no_ai))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="", help="Frontier 根目录（测试/迁移用）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init"); p.set_defaults(func=cmd_init)
    p = sub.add_parser("rebuild"); p.set_defaults(func=cmd_rebuild)

    p = sub.add_parser("ask")
    p.add_argument("--question", required=True); p.add_argument("--origin", choices=sorted(ORIGIN_KINDS), default="user_proposed")
    p.add_argument("--topk", type=int, default=6); p.add_argument("--no-ai", action="store_true")
    p.add_argument("--assessment-file", default=""); p.add_argument("--no-promote", action="store_true")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("assess"); p.add_argument("record_id"); p.add_argument("--assessment-file", required=True); p.add_argument("--no-promote", action="store_true"); p.set_defaults(func=cmd_assess)
    p = sub.add_parser("answer"); p.add_argument("record_id"); p.add_argument("--topk", type=int, default=6); p.add_argument("--no-ai", action="store_true"); p.set_defaults(func=cmd_answer)
    p = sub.add_parser("migrate-questions"); p.set_defaults(func=cmd_migrate_questions)
    p = sub.add_parser("split-questions"); p.set_defaults(func=cmd_split_questions)
    p = sub.add_parser("review"); p.add_argument("record_id"); p.add_argument("--status", choices=sorted(STATUSES - {"captured"}), required=True); p.add_argument("--reviewer", default="user"); p.set_defaults(func=cmd_review)
    p = sub.add_parser("list"); p.add_argument("--kind", choices=sorted(KINDS), default=""); p.add_argument("--status", choices=sorted(STATUSES), default=""); p.add_argument("--all", action="store_true"); p.add_argument("--limit", type=int, default=50); p.set_defaults(func=cmd_list)
    p = sub.add_parser("search"); p.add_argument("query"); p.add_argument("--limit", type=int, default=20); p.set_defaults(func=cmd_search)
    p = sub.add_parser("show"); p.add_argument("record_id"); p.set_defaults(func=cmd_show)
    p = sub.add_parser("capture-paper"); p.add_argument("page"); p.add_argument("--limit", type=int, default=3); p.add_argument("--topk", type=int, default=6); p.add_argument("--no-answer", action="store_true"); p.set_defaults(func=cmd_capture_paper)

    p = sub.add_parser("add-trajectory"); p.add_argument("--title", required=True); p.add_argument("--scope", required=True); p.add_argument("--origin", choices=sorted(ORIGIN_KINDS), default="ai_synthesis"); p.add_argument("--events-file", default=""); p.add_argument("--thread", action="append"); p.add_argument("--raw-anchor", action="append"); p.add_argument("--wiki-anchor", action="append"); p.add_argument("--graph-anchor", action="append"); p.set_defaults(func=cmd_add_trajectory)
    p = sub.add_parser("add-entry"); p.add_argument("record_id"); p.add_argument("--kind", choices=sorted(ENTRY_KINDS), required=True); p.add_argument("--content", required=True); p.add_argument("--origin", choices=sorted(ORIGIN_KINDS), default="user_proposed"); p.add_argument("--epistemic", choices=sorted(EPISTEMIC), default="untested"); p.add_argument("--evidence", action="append"); p.set_defaults(func=cmd_add_entry)
    p = sub.add_parser("link"); p.add_argument("subject"); p.add_argument("predicate", choices=sorted(EDGE_PREDICATES)); p.add_argument("object"); p.add_argument("--epistemic", choices=sorted(EPISTEMIC), default="derived"); p.add_argument("--evidence", action="append"); p.set_defaults(func=cmd_link)
    p = sub.add_parser("refresh"); p.add_argument("record_id"); p.add_argument("--topk", type=int, default=6); p.add_argument("--no-ai", action="store_true"); p.set_defaults(func=cmd_refresh)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {str(exc)}"}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
