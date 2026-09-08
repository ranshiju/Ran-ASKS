#!/usr/bin/env python3
"""Compatibility facade for the legacy project research-memory interface.

New persistent workspaces use workspace_state.py and .workspace/. Existing
research projects keep their CLI, Python API, IDs and .research-memory layout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".scripts"))

import agent_task
import workspace_state as workspace

PROJECTS_DIR = REPO / "projects"

INTENTS = {
    "decision": "研究决策（方向选择、方法取舍、实验设计）",
    "insight": "关键发现（重要洞察、对比结论、新认知）",
    "literature_judgment": "文献判断（论文质量评估、威胁度、相关性）",
    "research_direction": "研究方向（脉络调整、阶段推进、下步计划）",
}
PROFILE_STAGES = frozenset({
    "ideation", "writing", "experiment", "revision", "submission", "unknown",
})


def project_root(project: str) -> Path:
    """Resolve a legacy research project, including nested project paths."""
    base = PROJECTS_DIR.resolve()
    raw = Path(project)
    if raw.is_absolute():
        candidate = raw.resolve()
    else:
        parts = PurePosixPath(project).parts
        if parts and parts[0] == "projects":
            parts = parts[1:]
        if not parts or ".." in parts:
            raise SystemExit(f"ERROR: 非法研究项目路径: {project}")
        candidate = base.joinpath(*parts).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise SystemExit(f"ERROR: 研究项目必须位于 projects/ 内: {project}") from exc
    if candidate == base or not candidate.is_dir():
        raise SystemExit(f"ERROR: 研究项目不存在: {project}（在 projects/ 下未找到）")
    return candidate


def memory_dir(project: str) -> Path:
    return project_root(project) / ".research-memory"


def ensure_memory_dir(project: str) -> Path:
    directory = memory_dir(project)
    (directory / "entries").mkdir(parents=True, exist_ok=True)
    return directory


def load_index(project: str) -> list[dict]:
    return workspace.read_jsonl_strict(memory_dir(project) / "index.jsonl")


def save_index(project: str, entries: list[dict]) -> None:
    workspace.write_jsonl_atomic(ensure_memory_dir(project) / "index.jsonl", entries)


def next_mem_id(project: str) -> str:
    """Preserve sequential IDs only for the legacy compatibility surface."""
    max_number = 0
    for entry in load_index(project):
        match = re.fullmatch(r"MEM-(\d+)", str(entry.get("id") or ""))
        if match:
            max_number = max(max_number, int(match.group(1)))
    return f"MEM-{max_number + 1:04d}"


def load_profile(project: str) -> dict:
    path = memory_dir(project) / "profile.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise workspace.WorkspaceError(f"{path}: profile JSON 无法解析: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise workspace.WorkspaceError(f"{path}: profile 必须是 JSON object")
    return value


def save_profile(project: str, profile: dict) -> None:
    workspace.atomic_write_json(ensure_memory_dir(project) / "profile.json", profile)


def collect_project_text(project: str) -> str:
    return workspace.collect_bounded_text(
        project_root(project), excluded_managed={".research-memory"}, max_chars=20_000,
    )


def validate_profile(value: dict) -> list[str]:
    return workspace.validate_profile_value(value, "research")


def _profile_agent_task(project: str, text: str) -> dict:
    digest = hashlib.sha256((project + "\0" + text).encode("utf-8")).hexdigest()[:16]
    directory = REPO / "temp" / "research-memory"
    directory.mkdir(parents=True, exist_ok=True)
    input_path = directory / f"{digest}-project.txt"
    output_path = directory / f"{digest}-profile.json"
    workspace._atomic_write_text(input_path, text[:15_000])
    return agent_task.make_task(
        kind="research_profile",
        transaction_id=f"research-profile-{digest}",
        inputs=[{
            "name": "project_snapshot",
            "path": input_path.resolve().relative_to(REPO.resolve()).as_posix(),
            "role": "bounded_project_evidence",
            "read": "full",
        }],
        outputs=[{
            "name": "profile",
            "path": output_path.resolve().relative_to(REPO.resolve()).as_posix(),
            "format": "research-profile-v1",
        }],
        protocol={
            "name": "research-profile-v1",
            "fields": {
                "topic": "one sentence",
                "keywords": "5-10 strings",
                "stage": sorted(PROFILE_STAGES),
                "active_questions": "2-5 strings",
            },
            "validator": "research_memory.validate_profile",
        },
        commands={
            "commit": (
                "python3 .scripts/research_memory.py profile "
                f"{json.dumps(project, ensure_ascii=False)} --apply-profile "
                f"{output_path.resolve().relative_to(REPO.resolve()).as_posix()}"
            ),
        },
        context={"project": project},
    )


def _normalize_profile(profile: dict) -> dict:
    errors = validate_profile(profile)
    if errors:
        raise ValueError("; ".join(errors))
    return {
        "topic": profile["topic"].strip(),
        "keywords": list(dict.fromkeys(item.strip() for item in profile["keywords"] if item.strip())),
        "stage": profile["stage"],
        "active_questions": list(dict.fromkeys(
            item.strip() for item in profile["active_questions"] if item.strip()
        )),
        "updated_at": datetime.now().isoformat(),
    }


def apply_profile(project: str, path: Path) -> dict:
    resolved = agent_task.resolve_temp_artifact(REPO, path, "research-memory")
    profile = json.loads(resolved.read_text(encoding="utf-8"))
    normalized = _normalize_profile(profile)
    save_profile(project, normalized)
    return {"status": "completed", "project": project, "profile": normalized}


def refresh_profile(project: str) -> dict:
    text = collect_project_text(project)
    if not text.strip():
        return {"topic": "", "keywords": [], "stage": "unknown", "active_questions": [],
                "updated_at": datetime.now().isoformat(), "note": "项目内容为空"}
    if agent_task.research_backend() == "agent":
        return _profile_agent_task(project, text)
    try:
        from llm_structured import call_text
        result = call_text(
            workspace._profile_prompt("research", text), max_tokens=2048, retries=1,
            operation="research_profile",
            system="你是研究分析助手，从项目材料中提取结构化研究画像。",
        )
        if result.get("ok"):
            raw = str(result.get("text") or "").strip()
            start, end = raw.find("{"), raw.rfind("}")
            if start >= 0 and end >= start:
                normalized = _normalize_profile(json.loads(raw[start:end + 1]))
                save_profile(project, normalized)
                return normalized
        return {"status": "failed", "error": "API 未返回合法 JSON", "profile": load_profile(project)}
    except Exception as exc:
        return {"status": "failed", "error": str(exc), "profile": load_profile(project)}


def cmd_recall(args: argparse.Namespace) -> None:
    project = args.project
    profile = load_profile(project)
    entries = load_index(project)
    recent = entries[-10:]
    lines: list[str] = []
    if profile:
        lines.extend(["[研究画像]", f"- topic: {profile.get('topic', '-')}",
                      f"- stage: {profile.get('stage', '-')}"])
        if profile.get("keywords"):
            lines.append(f"- keywords: {', '.join(profile['keywords'][:8])}")
        if profile.get("active_questions"):
            lines.append("- active_questions:")
            lines.extend(f"  - {question}" for question in profile["active_questions"][:5])
        lines.extend([f"- updated: {profile.get('updated_at', '-')}", ""])
    if recent:
        lines.append(f"[近期记忆]（共 {len(entries)} 条，显示最近 {len(recent)} 条）")
        for entry in recent:
            lines.append(f"  {entry['id']} [{entry.get('intent', '?')}] {entry.get('title', '')} ({entry.get('at', '')[:10]})")
        lines.extend(["", "提示：用 `get <project> <MEM-xxxx>` 拉取完整条目。"])
    else:
        lines.append("[近期记忆] 无记忆条目。")
    status_path = project_root(project) / "notes" / "status.md"
    if status_path.exists():
        lines.extend(["", "[status.md]", status_path.read_text(encoding="utf-8")[:1500]])
    print("\n".join(lines))


def cmd_add(args: argparse.Namespace) -> None:
    if args.intent not in INTENTS:
        raise SystemExit(f"ERROR: intent 必须是 {list(INTENTS)}")
    content = sys.stdin.read() if args.stdin else args.content
    if not content:
        raise SystemExit("ERROR: 需 --content 或 --stdin 提供内容")
    memory_id = next_mem_id(args.project)
    directory = ensure_memory_dir(args.project)
    timestamp = datetime.now().isoformat()
    text = f"# {memory_id}: {args.title}\n\n- intent: {args.intent}\n- at: {timestamp}\n"
    if args.tags:
        text += f"- tags: {args.tags}\n"
    text += f"\n{content}\n"
    workspace._atomic_write_text(directory / "entries" / f"{memory_id}.md", text)
    entries = load_index(args.project)
    entries.append({"id": memory_id, "title": args.title, "intent": args.intent,
                    "at": timestamp, "tags": args.tags.split(",") if args.tags else []})
    save_index(args.project, entries)
    print(json.dumps({"status": "saved", "id": memory_id, "project": args.project},
                     ensure_ascii=False, indent=2))


def cmd_list(args: argparse.Namespace) -> None:
    entries = load_index(args.project)
    if args.intent:
        entries = [entry for entry in entries if entry.get("intent") == args.intent]
    items = [{"id": entry["id"], "title": entry.get("title", ""),
              "intent": entry.get("intent", ""), "at": entry.get("at", "")[:10],
              "tags": entry.get("tags", [])} for entry in entries]
    print(json.dumps({"project": args.project, "count": len(items), "entries": items},
                     ensure_ascii=False, indent=2))


def cmd_get(args: argparse.Namespace) -> None:
    path = memory_dir(args.project) / "entries" / f"{args.mem_id}.md"
    if not path.exists():
        raise SystemExit(f"ERROR: 记忆条目不存在: {args.mem_id}")
    print(path.read_text(encoding="utf-8"))


def cmd_profile(args: argparse.Namespace) -> None:
    if args.apply_profile:
        profile = apply_profile(args.project, Path(args.apply_profile))
    elif args.refresh:
        profile = refresh_profile(args.project)
    else:
        profile = load_profile(args.project) or refresh_profile(args.project)
    print(json.dumps(profile, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    recall = sub.add_parser("recall"); recall.add_argument("project"); recall.set_defaults(func=cmd_recall)
    add = sub.add_parser("add"); add.add_argument("project"); add.add_argument("--title", required=True)
    add.add_argument("--intent", required=True, choices=list(INTENTS)); add.add_argument("--content", default="")
    add.add_argument("--stdin", action="store_true"); add.add_argument("--tags", default=""); add.set_defaults(func=cmd_add)
    listing = sub.add_parser("list"); listing.add_argument("project")
    listing.add_argument("--intent", choices=list(INTENTS)); listing.set_defaults(func=cmd_list)
    get = sub.add_parser("get"); get.add_argument("project"); get.add_argument("mem_id"); get.set_defaults(func=cmd_get)
    profile = sub.add_parser("profile"); profile.add_argument("project")
    profile.add_argument("--refresh", action="store_true"); profile.add_argument("--apply-profile", default="")
    profile.set_defaults(func=cmd_profile)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
