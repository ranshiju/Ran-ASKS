#!/usr/bin/env python3
"""resolve_abbreviations.py — 知识库内校验命题裸缩写，识别无法消解者报 warning。

双层校验（均限本知识库，不拉外部知识）：
  1. 图层：裸缩写 → resolve_bare_name 查 alias 表 + keyword 节点；命中则已建立关联
  2. raw 层：反查命题源页 → extract_abbreviations 提取该论文 raw 关键段的缩写定义；
     命中说明 raw 里有全称，可据此建 keyword 节点（仍限知识库内）
  两层均 miss → warning（缩写在知识库内无全称可溯，需人工判断是否新建概念）

非阻断后置：摄入照常跑完，agent 用本工具 --list 看哪些缩写待处理。

用法:
  python3 .scripts/resolve_abbreviations.py --list
"""
import argparse
from datetime import datetime
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl
import graph_ingest as gi

REPO = Path(__file__).resolve().parent.parent
DEFAULT_TODO = REPO / "cross-domain" / "abbreviation-todo.jsonl"
RESOLUTION_KINDS = {
    "alias_to_full_name", "canonical_name", "unit_or_standard",
    "dataset_or_model", "ambiguous",
}


def _extract_abbr_tokens(text):
    """从文本提取裸缩写候选（≥2 连续大写字母开头的串，排除括号内已释义的）。"""
    if not text:
        return []
    no_paren = re.sub(r"[（(][^)）]*[)）]", "", text)
    return re.findall(r"[A-Z]{2,}[A-Za-z0-9]*", no_paren)


def _source_page(conn, prop_path):
    """反查命题节点的源页（subject=page, predicate=命题谓词, object=prop）。"""
    r = conn.execute(
        "SELECT subject FROM edges WHERE object=? AND predicate IN (?,?,?) LIMIT 1",
        (prop_path, "局限性", "核心创新点", "未来展望"),
    ).fetchone()
    return r["subject"] if r else None


def _read_todo(path: Path) -> tuple[list[dict], list[str]]:
    entries = []
    errors = []
    if not path.exists():
        return entries, errors
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {line_number}: {exc}")
            continue
        if not isinstance(value, dict):
            errors.append(f"line {line_number}: expected object")
            continue
        token_source = str(value.get("token") or value.get("value") or value.get("object") or "")
        tokens = _extract_abbr_tokens(token_source)
        if not tokens:
            errors.append(f"line {line_number}: no abbreviation token")
            continue
        for token in tokens:
            entries.append({
                **value,
                "schema_version": "abbreviation-todo-v2",
                "token": token,
                "context": str(
                    value.get("context") or value.get("value") or
                    value.get("object") or value.get("subject") or token
                ),
                "locator": value.get("locator") or value.get("source") or "",
                "resolution_state": value.get("resolution_state", "unresolved"),
            })
    return entries, errors


def _write_todo(path: Path, entries: list[dict]) -> None:
    unique = {}
    for entry in entries:
        key = (
            entry.get("page", ""), entry.get("subject", ""),
            entry.get("predicate", ""), entry.get("object", ""),
            entry.get("field", "object"), entry.get("token", ""),
        )
        unique[key] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(
        "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in unique.values()),
        encoding="utf-8",
    )
    temp_path.replace(path)


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    temp_path.replace(path)


def _load_maintenance_scope(receipt_arg: str) -> tuple[Path, dict, set[str]]:
    receipt_path = Path(receipt_arg)
    if not receipt_path.is_absolute():
        receipt_path = REPO / receipt_path
    receipt_path = receipt_path.resolve()
    allowed_root = (REPO / "temp" / "inbox-maintenance").resolve()
    if receipt_path.parent != allowed_root or receipt_path.suffix != ".json":
        raise ValueError("maintenance receipt must be a JSON file under temp/inbox-maintenance")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    component = receipt.get("components", {}).get("abbreviations", {})
    review_file = str(component.get("review_file") or "")
    if not review_file:
        raise ValueError("maintenance receipt has no abbreviation review_file")
    review_path = (REPO / review_file).resolve()
    review_root = (REPO / "temp" / "abbreviation-review").resolve()
    if review_path.parent != review_root or review_path.suffix != ".json":
        raise ValueError("abbreviation review_file is outside temp/abbreviation-review")
    review = json.loads(review_path.read_text(encoding="utf-8"))
    tokens = {
        str(item.get("token") or "").strip()
        for item in review.get("candidates", []) if str(item.get("token") or "").strip()
    }
    if not tokens:
        raise ValueError("abbreviation review has no candidate tokens")
    return receipt_path, receipt, tokens


def _close_maintenance_receipt(receipt_path: Path, receipt: dict,
                               review_tokens: set[str], report: dict,
                               remaining_entries: list[dict]) -> dict:
    remaining_review = [
        entry for entry in remaining_entries if entry.get("token") in review_tokens
    ]
    remaining_tokens = sorted({entry["token"] for entry in remaining_review})
    component = receipt.setdefault("components", {}).setdefault("abbreviations", {})
    component.update({
        "status": "agent_required" if remaining_tokens else "completed",
        "remaining": len(remaining_review),
        "remaining_tokens": len(remaining_tokens),
        "remaining_occurrences": len(remaining_review),
        "applied_decisions": report.get("applied", []),
    })
    if not remaining_tokens:
        component.pop("next_action", None)
    actions = [
        action for action in receipt.get("actions", [])
        if not (action.get("component") == "abbreviations" and not remaining_tokens)
    ]
    receipt["actions"] = actions
    errors = receipt.get("errors", [])
    deferred = any(
        isinstance(value, dict) and value.get("status") == "deferred"
        for value in receipt.get("components", {}).values()
    )
    receipt["status"] = (
        "error" if errors else "agent_required" if actions else
        "deferred" if deferred else "completed"
    )
    receipt["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_json_atomic(receipt_path, receipt)
    return {
        "status": receipt["status"],
        "receipt_path": str(receipt_path.relative_to(REPO.resolve())),
        "remaining_tokens": len(remaining_tokens),
        "remaining_occurrences": len(remaining_review),
    }


def _raw_definitions(page: str, cache: dict) -> dict:
    if not page:
        return {}
    if page not in cache:
        try:
            import extract_abbreviations as ea
            pairs, _ = ea.extract_for_page(page + ".md")
            cache[page] = {abbr: full for abbr, full in pairs}
        except Exception:
            cache[page] = {}
    return cache[page]


def _collect_pending(conn, todo_entries: list[dict] | None = None) -> list[dict]:
    """Collect unresolved tokens; todo_entries bounds Raw reads during inbox tail."""
    title_idx, alias_idx, suffix_idx = gl.build_name_index(conn)
    rows = conn.execute(
        "SELECT path, title FROM nodes WHERE entity_subtype='proposition'"
    ).fetchall()
    filter_tokens = ({entry["token"] for entry in todo_entries}
                     if todo_entries is not None else None)
    pending = {}
    raw_cache = {}
    for row in rows:
        r = dict(row)
        title = r["title"] or ""
        if not gi.is_bare_abbreviation(title):
            continue
        tokens = _extract_abbr_tokens(title)
        if filter_tokens is not None:
            tokens = [token for token in tokens if token in filter_tokens]
        if not tokens:
            continue
        page = _source_page(conn, r["path"])
        raw_defs = _raw_definitions(page, raw_cache)
        for tok in tokens:
            # 层1：图 resolve
            resolved, _ambig = gl.resolve_bare_name(tok, title_idx, alias_idx, suffix_idx)
            if resolved:
                sub = conn.execute(
                    "SELECT entity_subtype FROM nodes WHERE path=?", (resolved,)
                ).fetchone()
                if sub and sub["entity_subtype"] == "keyword":
                    continue
            # 层2：raw 提取
            if tok in raw_defs:
                pending.setdefault(tok, {"token": tok, "raw": raw_defs[tok], "occurrences": []})
                pending[tok]["occurrences"].append({
                    "path": r["path"], "title": title, "page": page or "",
                })
                continue
            # 双层 miss → warning
            pending.setdefault(tok, {"token": tok, "raw": None, "occurrences": []})
            pending[tok]["occurrences"].append({
                "path": r["path"], "title": title, "page": page or "",
            })

    # Slot/free-edge warnings may not have a proposition node. Preserve them as
    # review occurrences and still use their page-local Raw definition.
    for entry in todo_entries or []:
        tok = entry["token"]
        resolved, _ambig = gl.resolve_bare_name(tok, title_idx, alias_idx, suffix_idx)
        if resolved:
            subtype = conn.execute(
                "SELECT entity_subtype FROM nodes WHERE path=?", (resolved,)
            ).fetchone()
            if subtype and subtype["entity_subtype"] == "keyword":
                continue
        page = str(entry.get("page", ""))
        raw_defs = _raw_definitions(page, raw_cache)
        item = pending.setdefault(tok, {
            "token": tok, "raw": raw_defs.get(tok), "occurrences": [],
        })
        if item.get("raw") is None and tok in raw_defs:
            item["raw"] = raw_defs[tok]
        occurrence = {
            "path": str(entry.get("object") or entry.get("subject") or ""),
            "title": str(entry.get("context") or entry.get("object") or tok),
            "page": page,
            "field": entry.get("field", "object"),
            "locator": entry.get("locator", ""),
        }
        if occurrence not in item["occurrences"]:
            item["occurrences"].append(occurrence)

    return [pending[token] for token in sorted(pending)]


def list_pending(conn, todo_entries: list[dict] | None = None, *, emit: bool = True) -> dict:
    """双层校验，列出无法在知识库内消解的裸缩写（warning）。"""
    pending = _collect_pending(conn, todo_entries)
    if not pending:
        report = {"status": "completed", "raw_resolvable": 0,
                  "warning_count": 0, "candidates": []}
        if emit:
            print("无需消解的裸缩写（所有命题缩写已 resolve 或无裸缩写）。")
        return report
    candidates = []
    for info in pending:
        tok = info["token"]
        occurrences = info["occurrences"]
        context = occurrences[0]["title"] if occurrences else tok
        if info["raw"]:
            suggested_kind = "alias_to_full_name"
        elif context.strip() == tok:
            suggested_kind = "canonical_name"
        elif re.search(r"\d", tok) or tok.endswith(("Hz", "B")):
            suggested_kind = "unit_or_standard"
        elif re.search(r"benchmark|dataset|model|基准|数据集|模型", context, re.I):
            suggested_kind = "dataset_or_model"
        else:
            suggested_kind = "ambiguous"
        candidates.append({
            **info,
            "raw_full_name": info["raw"][0] if info["raw"] else None,
            "suggested_kind": suggested_kind,
            "allowed_kinds": sorted(RESOLUTION_KINDS),
        })
    report = {
        "status": "agent_required" if any(not item["raw"] for item in candidates) else "completed",
        "raw_resolvable": sum(bool(item["raw"]) for item in candidates),
        "warning_count": sum(not item["raw"] for item in candidates),
        "candidates": candidates,
    }
    if not emit:
        return report
    for info in candidates:
        tok = info["token"]
        occurrences = info["occurrences"]
        if info["raw"]:
            print(f"  〔raw 有定义〕{tok} = {info['raw'][0]}（{len(occurrences)} 处命题，可自动消解）")
        else:
            print(f"  ⚠ {tok}（{len(occurrences)} 处命题，知识库内无全称可溯 → warning）")
        for occurrence in occurrences[:2]:
            print(f"      └ {occurrence['title']}")
        if len(occurrences) > 2:
            print(f"      … 等 {len(occurrences)} 处")
        print()
    print(f"汇总：raw 可消解 {report['raw_resolvable']}，warning {report['warning_count']}")
    return report


def _replace_abbr_in_title(title, abbr, kid):
    """把 title 中的裸缩写替换为 keyword id（词边界保护，不拆 GPT-2 这类复合词）。"""
    pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(abbr) + r"(?![A-Za-z0-9])")
    return pattern.sub(kid, title)


def _rename_node(conn, old_path, new_path):
    """重命名节点 path 并迁移所有关联边。返回是否执行了重命名。

    edges.subject/object 对 nodes(path) 有立即 FK 且无 ON UPDATE 动作，
    不能直接先改父节点 path。改为复制节点行到 new_path → 迁移边 → 删除旧节点，
    三步在每个中间态都满足 FK。
    """
    if old_path == new_path or gl.node_exists(conn, new_path):
        return False
    cur = conn.execute(
        "INSERT INTO nodes "
        "(path, title, type, source_type, date, status, has_raw_source, entity_subtype, ingest_version) "
        "SELECT ?, title, type, source_type, date, status, has_raw_source, entity_subtype, ingest_version "
        "FROM nodes WHERE path=?",
        (new_path, old_path),
    )
    if cur.rowcount == 0:
        return False
    conn.execute("UPDATE edges SET subject=? WHERE subject=?", (new_path, old_path))
    conn.execute("UPDATE edges SET object=? WHERE object=?", (new_path, old_path))
    conn.execute("DELETE FROM nodes WHERE path=?", (old_path,))
    return True


def _ensure_keyword(conn, token: str, full_name: str) -> str:
    display = full_name if token in full_name else f"{full_name}({token})"
    kid = gl.extract_keyword_id(display)
    gl.ensure_node(conn, kid, display, "entity", entity_subtype="keyword")
    gl.insert_aliases(conn, kid, [token])
    return kid


def _attach_occurrence(conn, occurrence: dict, kid: str) -> None:
    target = str(occurrence.get("path", ""))
    if not target or target == kid or not gl.node_exists(conn, target):
        return
    exists = conn.execute(
        "SELECT 1 FROM edges WHERE subject=? AND predicate='包含' AND object=?",
        (target, kid),
    ).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO edges (subject, predicate, object, confidence, source, is_sr) "
            "VALUES (?,?,?,?,?,?)",
            (target, "包含", kid, gl.DEFAULT_CONFIDENCE, "abbreviation-resolve", 0),
        )


def apply(conn, todo_entries: list[dict] | None = None, *, emit: bool = True) -> dict:
    """批量消解：对 raw 有定义的缩写，用 raw 全称建 keyword 节点 + 更新命题 path + 建包含边。

    仅消解 layer 2（raw 提取）命中的缩写；layer 1 已 resolve 的不重复处理。
    全称来自知识库内 raw，不拉外部知识。
    """
    pending = _collect_pending(conn, todo_entries)
    resolved = []
    for item in pending:
        if not item.get("raw"):
            continue
        token = item["token"]
        full_name = item["raw"][0]
        kid = _ensure_keyword(conn, token, full_name)
        for occurrence in item["occurrences"]:
            _attach_occurrence(conn, occurrence, kid)
        resolved.append({"token": token, "keyword": kid, "full_name": full_name,
                         "occurrences": len(item["occurrences"])})
        if emit:
            print(f"  ✅ {token} → keyword {kid}（raw: {full_name}）")
    conn.commit()
    report = {"status": "completed", "resolved": resolved,
              "resolved_count": len(resolved)}
    if emit:
        print(f"\n汇总：消解 {len(resolved)} 个缩写")
    return report


def apply_decisions(conn, decisions: list[dict], todo_entries: list[dict]) -> dict:
    """Validate an Agent decision batch, then apply it atomically."""
    errors = []
    normalized = []
    pending_tokens = {entry["token"] for entry in todo_entries}
    seen_tokens = set()
    for index, decision in enumerate(decisions):
        token = str(decision.get("token", "")).strip()
        kind = str(decision.get("resolution_kind", "")).strip()
        full_name = str(decision.get("full_name", "")).strip()
        if not re.fullmatch(r"[A-Z]{2,}[A-Za-z0-9]*", token):
            errors.append(f"decision {index}: invalid token")
        if kind not in RESOLUTION_KINDS:
            errors.append(f"decision {index}: invalid resolution_kind")
        if kind == "alias_to_full_name" and not full_name:
            errors.append(f"decision {index}: full_name required")
        if token not in pending_tokens:
            errors.append(f"decision {index}: token is not pending")
        if token in seen_tokens:
            errors.append(f"decision {index}: duplicate token")
        seen_tokens.add(token)
        normalized.append({"token": token, "resolution_kind": kind, "full_name": full_name})
    if errors:
        return {"status": "validation_error", "errors": errors, "applied": []}

    occurrences_by_token = {}
    for entry in todo_entries:
        occurrences_by_token.setdefault(entry["token"], []).append({
            "path": str(entry.get("object") or entry.get("subject") or ""),
            "title": str(entry.get("context") or entry.get("object") or entry["token"]),
            "page": str(entry.get("page", "")),
        })
    applied = []
    for decision in normalized:
        token = decision["token"]
        kind = decision["resolution_kind"]
        if kind == "ambiguous":
            continue
        if kind == "alias_to_full_name":
            kid = _ensure_keyword(conn, token, decision["full_name"])
        else:
            safe_token = re.sub(r"[^A-Za-z0-9_-]+", "-", token).strip("-")
            kid = f"canonical-abbreviation/{safe_token}"
            gl.ensure_node(
                conn, kid, token, "entity", entity_subtype="keyword",
                description=f"Agent-confirmed abbreviation kind: {kind}",
            )
            gl.insert_aliases(conn, kid, [token])
        for occurrence in occurrences_by_token.get(token, []):
            _attach_occurrence(conn, occurrence, kid)
        applied.append({"token": token, "resolution_kind": kind, "keyword": kid})
    conn.commit()
    return {"status": "completed", "applied": applied,
            "remaining": [item for item in normalized if item["resolution_kind"] == "ambiguous"]}


def main():
    ap = argparse.ArgumentParser(description="知识库内校验命题裸缩写，识别 warning")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="双层校验并列出待处理缩写")
    mode.add_argument("--apply", action="store_true", help="批量消解 raw 有定义的缩写")
    mode.add_argument("--apply-decisions", metavar="FILE", help="应用主 Agent 的类型化决策 JSON")
    ap.add_argument("--todo", default=str(DEFAULT_TODO), help="限定待办 JSONL（收尾阶段使用）")
    ap.add_argument("--maintenance-receipt",
                    help="apply-decisions 对应的 temp/inbox-maintenance 回执；成功后闭环更新")
    ap.add_argument("--json", action="store_true", help="只输出结构化 JSON")
    args = ap.parse_args()
    todo_path = Path(args.todo)
    if not todo_path.is_absolute():
        todo_path = REPO / todo_path
    todo_entries, todo_errors = _read_todo(todo_path)
    if todo_errors and (args.apply or args.apply_decisions or "--todo" in sys.argv):
        print(json.dumps({"status": "validation_error", "errors": todo_errors},
                         ensure_ascii=False))
        raise SystemExit(1)
    conn = None
    try:
        conn = gl.connect()
        if args.list:
            # Manual list remains full-graph by default; an explicit --todo bounds it.
            entries = todo_entries if "--todo" in sys.argv else None
            report = list_pending(conn, entries, emit=not args.json)
        elif args.apply:
            applied = apply(conn, todo_entries, emit=not args.json)
            resolved_tokens = {item["token"] for item in applied["resolved"]}
            remaining_entries = [
                entry for entry in todo_entries if entry["token"] not in resolved_tokens
            ]
            _write_todo(todo_path, remaining_entries)
            pending = list_pending(conn, remaining_entries, emit=False)
            report = {
                **applied,
                "status": pending["status"],
                "raw_resolvable": pending["raw_resolvable"],
                "warning_count": pending["warning_count"],
                "candidates": pending["candidates"],
            }
        else:
            decision_path = Path(args.apply_decisions)
            if not decision_path.is_absolute():
                decision_path = REPO / decision_path
            try:
                value = json.loads(decision_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                report = {"status": "validation_error", "errors": [str(exc)]}
            else:
                decisions = value.get("decisions", []) if isinstance(value, dict) else value
                if not isinstance(decisions, list):
                    report = {"status": "validation_error", "errors": ["decisions must be a list"]}
                else:
                    maintenance_scope = None
                    try:
                        if args.maintenance_receipt:
                            maintenance_scope = _load_maintenance_scope(args.maintenance_receipt)
                            decision_tokens = {
                                str(item.get("token") or "").strip()
                                for item in decisions if isinstance(item, dict)
                            }
                            expected_tokens = maintenance_scope[2]
                            if decision_tokens != expected_tokens:
                                missing = sorted(expected_tokens - decision_tokens)
                                extra = sorted(decision_tokens - expected_tokens)
                                raise ValueError(
                                    f"decisions must cover review tokens exactly; missing={missing}, extra={extra}"
                                )
                    except (OSError, ValueError, json.JSONDecodeError) as exc:
                        report = {"status": "validation_error", "errors": [str(exc)]}
                    else:
                        report = apply_decisions(conn, decisions, todo_entries)
                        if report["status"] == "completed":
                            applied_tokens = {item["token"] for item in report["applied"]}
                            remaining_entries = [
                                entry for entry in todo_entries
                                if entry["token"] not in applied_tokens
                            ]
                            _write_todo(todo_path, remaining_entries)
                            if maintenance_scope is not None:
                                receipt_path, receipt, review_tokens = maintenance_scope
                                report["maintenance"] = _close_maintenance_receipt(
                                    receipt_path, receipt, review_tokens, report, remaining_entries,
                                )
    except Exception as exc:
        report = {"status": "error", "errors": [f"{type(exc).__name__}: {exc}"]}
    finally:
        if conn is not None:
            conn.close()
    if args.json or report.get("status") in {"validation_error", "error"}:
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    if report.get("status") in {"validation_error", "error"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
