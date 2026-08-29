#!/usr/bin/env python3
"""会议纪要语音实体解析：知识库召回 + 局部候选排序。

默认只输出 JSON，不修改输入文件。--apply 仅应用唯一的精确 alias 替换，
模糊候选和多候选项始终保留在 review 中，供 LLM/人工确认。
"""
import argparse
import difflib
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "cross-domain" / "graph.db"
INDEX = REPO / ".scripts" / "speech_entity_index.json"
CJK_RE = re.compile(r"[\u3400-\u9fff]{2,6}")
LATIN_NAME_RE = re.compile(r"\b[A-Z][A-Za-zÀ-ÖØ-öø-ÿ]+(?:[- ][A-Z][A-Za-zÀ-ÖØ-öø-ÿ]+){1,3}\b")
TOKEN_RE = re.compile(r"[\u3400-\u9fff]{2,6}|\b[A-Za-z][A-Za-zÀ-ÖØ-öø-ÿ.-]{1,30}\b")


def _read_people_from_db():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    people = {}
    for row in conn.execute("SELECT path,title FROM nodes WHERE type='people'"):
        people[row["path"]] = row["title"]
    aliases = {}
    # 只读 confirmed 别名做自动替换; pending(待确认)不自动替换, 由模糊匹配进 review
    for row in conn.execute(
        "SELECT a.alias,a.node_path FROM aliases a "
        "JOIN nodes n ON n.path=a.node_path WHERE n.type='people'"
    ):
        aliases[row["alias"]] = row["node_path"]
    graph = {}
    graph_rows = [dict(row) for row in conn.execute(
        "SELECT subject,predicate,object FROM edges "
        "WHERE subject IN (SELECT path FROM nodes WHERE type='people') "
        "OR object IN (SELECT path FROM nodes WHERE type='people')"
    )]
    for row in graph_rows:
        for path in (row["subject"], row["object"]):
            if path in people:
                graph.setdefault(path, []).append({
                    "predicate": row["predicate"],
                    "other": row["object"] if path == row["subject"] else row["subject"],
                })
    conn.close()
    fingerprint_data = {
        "people": sorted(people.items()),
        "aliases": sorted(aliases.items()),
        "edges": sorted((r["subject"], r["predicate"], r["object"]) for r in graph_rows),
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_data, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return people, aliases, graph, fingerprint


def load_people(force_refresh=False):
    """读取人物纠错索引；仅人物相关数据变化时重建。"""
    people, aliases, graph, fingerprint = _read_people_from_db()
    if not force_refresh and INDEX.exists():
        try:
            cached = json.loads(INDEX.read_text(encoding="utf-8"))
            if cached.get("fingerprint") == fingerprint:
                return cached["people"], cached["aliases"], cached["graph"], fingerprint, False
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            pass
    payload = {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fingerprint": fingerprint,
        "people": people,
        "aliases": aliases,
        "graph": graph,
    }
    temporary = INDEX.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(INDEX)
    return people, aliases, graph, fingerprint, True


def normalize_text(value):
    return re.sub(r"[\s·•]+", "", value).lower().replace("－", "-").replace("—", "-")


def candidates_from_text(text, people, aliases, graph=None, candidate_paths=None):
    """提取文本内疑似人名，并给出知识库精确匹配和模糊候选。"""
    graph = graph or {}
    if candidate_paths:
        allowed = set(candidate_paths)
        known = sorted(
            {name for path, name in people.items() if path in allowed}
            | {alias for alias, path in aliases.items() if path in allowed},
            key=len, reverse=True,
        )
    else:
        known = sorted(set(people.values()) | set(aliases), key=len, reverse=True)
    normalized_known = {normalize_text(x): x for x in known}
    path_by_normalized = {normalize_text(name): path for path, name in people.items()}
    occurrences = []
    seen = set()
    exact_spans = []
    for known_name in known:
        path = aliases.get(known_name) or path_by_normalized.get(normalize_text(known_name))
        if not path:
            continue
        for exact in re.finditer(re.escape(known_name), text, flags=re.IGNORECASE if known_name.isascii() else 0):
            if not known_name.isascii() and (
                (exact.start() > 0 and CJK_RE.match(text[exact.start() - 1]))
                or (exact.end() < len(text) and CJK_RE.match(text[exact.end()]))
            ):
                continue
            canonical = people[path]
            item = {
                "original": exact.group(0), "normalized": canonical, "entity": path,
                "method": "alias_exact", "confidence": "high",
                "span": [exact.start(), exact.end()],
            }
            key = (exact.start(), exact.end(), canonical)
            if key not in seen:
                occurrences.append(item)
                seen.add(key)
            exact_spans.append((exact.start(), exact.end()))
    for match in TOKEN_RE.finditer(text):
        token = match.group(0).strip("，。！？：；,.!?()（）[]【】\"'")
        if len(token) < 2:
            continue
        norm = normalize_text(token)
        path = aliases.get(token)
        if path is None:
            canonical = normalized_known.get(norm)
            if canonical:
                path = aliases.get(canonical) or path_by_normalized.get(normalize_text(canonical))
        if path:
            canonical = people[path]
            item = {
                "original": token, "normalized": canonical, "entity": path,
                "method": "alias_exact", "confidence": "high",
                "span": [match.start(), match.end()],
            }
            key = (match.start(), match.end(), canonical)
            if key not in seen:
                occurrences.append(item); seen.add(key)
            continue
        if any(start < match.end() and match.start() < end for start, end in exact_spans):
            continue
        # 中文模糊匹配只检查与已知人名等长的短片段；连续长句不再作为候选。
        is_cjk = bool(CJK_RE.fullmatch(token))
        is_latin = bool(LATIN_NAME_RE.fullmatch(token))
        known_lengths = {len(x) for x in known if re.fullmatch(r"[\u3400-\u9fff]+", x)}
        if is_cjk and (len(token) not in known_lengths or len(token) > 4):
            continue
        if is_cjk and (
            (match.start() > 0 and CJK_RE.match(text[match.start() - 1]))
            or (match.end() < len(text) and CJK_RE.match(text[match.end()]))
        ):
            continue
        if not (is_cjk or is_latin):
            continue
        scored = []
        for candidate in known:
            if is_cjk and len(candidate) != len(token):
                continue
            score = difflib.SequenceMatcher(None, norm, normalize_text(candidate)).ratio()
            if score >= 0.58:
                scored.append((score, candidate, aliases.get(candidate)))
        scored.sort(reverse=True)
        if scored:
            top = scored[:3]
            evidence = []
            for score, name, path in top:
                if path:
                    evidence.extend(graph.get(path, [])[:3])
            occurrences.append({
                "original": token,
                "candidates": [
                    {"name": people.get(path, name), "entity": path, "score": round(score, 3)}
                    for score, name, path in top if path
                ],
                "method": "name_similarity",
                "confidence": "review" if len(top) == 1 or top[0][0] - top[1][0] >= 0.12 else "ambiguous",
                "span": [match.start(), match.end()],
                "context": text[max(0, match.start() - 30):min(len(text), match.end() + 30)],
                "evidence": evidence,
            })
    return occurrences


def apply_exact(text, resolutions):
    replacements = [r for r in resolutions if r.get("method") == "alias_exact" and r["original"] != r["normalized"]]
    for item in sorted(replacements, key=lambda x: x["span"][0], reverse=True):
        start, end = item["span"]
        text = text[:start] + item["normalized"] + text[end:]
    return text


def resolve(text, candidate_paths=None, force_refresh=False):
    people, aliases, graph, fingerprint, index_rebuilt = load_people(force_refresh)
    resolutions = candidates_from_text(text, people, aliases, graph, candidate_paths)
    return {
        "people_count": len(people),
        "alias_count": len(aliases),
        "resolved": [r for r in resolutions if r["method"] == "alias_exact"],
        "review": [r for r in resolutions if r["method"] != "alias_exact"],
        "stats": {
            "exact": sum(r["method"] == "alias_exact" for r in resolutions),
            "review": sum(r["method"] != "alias_exact" for r in resolutions),
            "exact_same": sum(r["method"] == "alias_exact" and r["original"] == r["normalized"] for r in resolutions),
            "exact_replaced": sum(r["method"] == "alias_exact" and r["original"] != r["normalized"] for r in resolutions),
        },
        "knowledge_snapshot": {
            "people_alias_fingerprint": fingerprint,
            "people_count": len(people),
            "alias_count": len(aliases),
            "index_rebuilt": index_rebuilt,
        },
    }


def main():
    parser = argparse.ArgumentParser(description="会议纪要语音实体解析")
    parser.add_argument("input", help="ASR/corrected 文本路径")
    parser.add_argument("--output", help="JSON 审计输出路径，默认 stdout")
    parser.add_argument("--apply", help="输出仅应用精确 alias 的 corrected 文本")
    parser.add_argument("--candidate-file", help="候选人物路径或名称文件，每行一个；用于本次会议的局部召回")
    parser.add_argument("--refresh-index", action="store_true", help="强制重建人物纠错索引，不重新处理历史会议")
    args = parser.parse_args()
    source = Path(args.input)
    text = source.read_text(encoding="utf-8", errors="replace")
    candidate_paths = None
    if args.candidate_file:
        people, aliases, _, _, _ = load_people(args.refresh_index)
        requested = {line.strip() for line in Path(args.candidate_file).read_text(encoding="utf-8").splitlines() if line.strip()}
        candidate_paths = {value for value in people if value in requested}
        candidate_paths.update(aliases[value] for value in requested if value in aliases)
    result = resolve(text, candidate_paths, args.refresh_index)
    result["input"] = str(source)
    if args.apply:
        Path(args.apply).write_text(apply_exact(text, result["resolved"]), encoding="utf-8")
        result["corrected_output"] = args.apply
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
