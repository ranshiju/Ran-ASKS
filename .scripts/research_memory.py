#!/usr/bin/env python3
"""research_memory.py — 研究项目级结构化记忆工具。

记忆绑定到 projects/<name>/.research-memory/，独立于 ingest（研究内容不入库）。
agent 进入研究项目时 recall 恢复上下文；研究过程中自动 add 记忆条目。

用法:
  python3 .scripts/research_memory.py recall <project>
  python3 .scripts/research_memory.py add <project> --title "..." --intent decision [--content "..." | --stdin]
  python3 .scripts/research_memory.py list <project> [--intent decision]
  python3 .scripts/research_memory.py get <project> <MEM-0001>
  python3 .scripts/research_memory.py profile <project> [--refresh]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".scripts"))

PROJECTS_DIR = REPO / "projects"

# 记忆意图分类
INTENTS = {
    "decision": "研究决策（方向选择、方法取舍、实验设计）",
    "insight": "关键发现（重要洞察、对比结论、新认知）",
    "literature_judgment": "文献判断（论文质量评估、威胁度、相关性）",
    "research_direction": "研究方向（脉络调整、阶段推进、下步计划）",
}


def project_root(project: str) -> Path:
    """解析项目名到 projects/<name> 路径。"""
    p = PROJECTS_DIR / project
    if not p.is_dir():
        raise SystemExit(f"ERROR: 研究项目不存在: {project}（在 projects/ 下未找到）")
    return p


def memory_dir(project: str) -> Path:
    """返回项目的记忆目录路径。"""
    d = project_root(project) / ".research-memory"
    return d


def ensure_memory_dir(project: str) -> Path:
    d = memory_dir(project)
    (d / "entries").mkdir(parents=True, exist_ok=True)
    return d


def load_index(project: str) -> list[dict]:
    idx_path = memory_dir(project) / "index.jsonl"
    if not idx_path.exists():
        return []
    entries = []
    for line in idx_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def save_index(project: str, entries: list[dict]) -> None:
    d = ensure_memory_dir(project)
    idx_path = d / "index.jsonl"
    lines = [json.dumps(e, ensure_ascii=False) for e in entries]
    idx_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def next_mem_id(project: str) -> str:
    entries = load_index(project)
    max_n = 0
    for e in entries:
        m = re.match(r"MEM-(\d+)", e.get("id", ""))
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"MEM-{max_n + 1:04d}"


def load_profile(project: str) -> dict:
    p = memory_dir(project) / "profile.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_profile(project: str, profile: dict) -> None:
    d = ensure_memory_dir(project)
    p = d / "profile.json"
    p.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def collect_project_text(project: str) -> str:
    """收集项目内容供画像提取（排除 .research-memory 和二进制文件）。"""
    root = project_root(project)
    parts = []
    skip_dirs = {".research-memory", "archive", "__pycache__", ".git", "node_modules"}
    skip_exts = {".pdf", ".gz", ".aux", ".log", ".out", ".blg", ".bbl", ".spl",
                 ".synctex.gz", ".dvi", ".DS_Store", ".zip", ".tar"}
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        if any(part in skip_dirs for part in f.parts):
            continue
        if f.suffix.lower() in skip_exts:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if not text.strip():
            continue
        rel = f.relative_to(root)
        parts.append(f"=== {rel} ===\n{text[:3000]}")
        if len("\n".join(parts)) > 20000:
            break
    return "\n\n".join(parts)


def refresh_profile(project: str) -> dict:
    """LLM 从项目内容提取研究画像。"""
    text = collect_project_text(project)
    if not text.strip():
        return {"topic": "", "keywords": [], "stage": "unknown", "active_questions": [],
                "updated_at": datetime.now().isoformat(), "note": "项目内容为空"}
    try:
        from llm_structured import call_text
        prompt = f"""分析以下研究项目内容，提取结构化研究画像。输出严格 JSON（不要 markdown 包裹）。

要求提取：
- topic: 一句话描述研究主题
- keywords: 5-10 个核心关键词
- stage: 当前阶段（ideation/writing/experiment/revision/submission/unknown）
- active_questions: 当前正在探索的 2-5 个核心问题（列表）

[项目内容]
{text[:15000]}

[输出格式]
{{"topic": "...", "keywords": ["..."], "stage": "...", "active_questions": ["..."]}}"""
        result = call_text(prompt, max_tokens=2048, retries=1, operation="research_profile",
                           system="你是研究分析助手，从项目材料中提取结构化研究画像。")
        if result.get("ok"):
            import re as _re
            raw = result.get("text", "").strip()
            m = _re.search(r'\{[\s\S]*\}', raw)
            if m:
                profile = json.loads(m.group(0))
                profile["updated_at"] = datetime.now().isoformat()
                save_profile(project, profile)
                return profile
    except Exception as exc:
        pass
    # fallback：只更新时间戳
    profile = load_profile(project)
    profile["updated_at"] = datetime.now().isoformat()
    profile["note"] = f"LLM 提取失败，保留旧画像"
    save_profile(project, profile)
    return profile


def cmd_recall(args):
    """渲染紧凑上下文：profile + 近期条目摘要。供会话恢复。"""
    project = args.project
    profile = load_profile(project)
    entries = load_index(project)
    # 近 10 条
    recent = entries[-10:] if len(entries) > 10 else entries

    lines = []
    if profile:
        lines.append("[研究画像]")
        lines.append(f"- topic: {profile.get('topic', '-')}")
        lines.append(f"- stage: {profile.get('stage', '-')}")
        kws = profile.get("keywords", [])
        if kws:
            lines.append(f"- keywords: {', '.join(kws[:8])}")
        aqs = profile.get("active_questions", [])
        if aqs:
            lines.append("- active_questions:")
            for q in aqs[:5]:
                lines.append(f"  - {q}")
        lines.append(f"- updated: {profile.get('updated_at', '-')}")
        lines.append("")

    if recent:
        lines.append(f"[近期记忆]（共 {len(entries)} 条，显示最近 {len(recent)} 条）")
        for e in recent:
            lines.append(f"  {e['id']} [{e.get('intent', '?')}] {e.get('title', '')} ({e.get('at', '')[:10]})")
        lines.append("")
        lines.append("提示：用 `get <project> <MEM-xxxx>` 拉取完整条目。")
    else:
        lines.append("[近期记忆] 无记忆条目。")

    # 读最新 status.md（如有）
    status_path = project_root(project) / "notes" / "status.md"
    if status_path.exists():
        lines.append("")
        lines.append("[status.md]")
        lines.append(status_path.read_text(encoding="utf-8")[:1500])

    print("\n".join(lines))


def cmd_add(args):
    """新增记忆条目。"""
    project = args.project
    if args.intent not in INTENTS:
        raise SystemExit(f"ERROR: intent 必须是 {list(INTENTS.keys())}")
    content = args.content
    if not content and args.stdin:
        content = sys.stdin.read()
    if not content:
        raise SystemExit("ERROR: 需 --content 或 --stdin 提供内容")
    mem_id = next_mem_id(project)
    d = ensure_memory_dir(project)
    # 写完整条目
    entry_path = d / "entries" / f"{mem_id}.md"
    entry_text = f"# {mem_id}: {args.title}\n\n"
    entry_text += f"- intent: {args.intent}\n"
    entry_text += f"- at: {datetime.now().isoformat()}\n"
    if args.tags:
        entry_text += f"- tags: {args.tags}\n"
    entry_text += f"\n{content}\n"
    entry_path.write_text(entry_text, encoding="utf-8")
    # 更新索引
    entries = load_index(project)
    entries.append({
        "id": mem_id,
        "title": args.title,
        "intent": args.intent,
        "at": datetime.now().isoformat(),
        "tags": args.tags.split(",") if args.tags else [],
    })
    save_index(project, entries)
    print(json.dumps({"status": "saved", "id": mem_id, "project": project},
                     ensure_ascii=False, indent=2))


def cmd_list(args):
    """列出记忆条目。"""
    project = args.project
    entries = load_index(project)
    if args.intent:
        entries = [e for e in entries if e.get("intent") == args.intent]
    if not entries:
        print(json.dumps({"project": project, "count": 0, "entries": []}))
        return
    items = [{"id": e["id"], "title": e.get("title", ""), "intent": e.get("intent", ""),
              "at": e.get("at", "")[:10], "tags": e.get("tags", [])}
             for e in entries]
    print(json.dumps({"project": project, "count": len(items), "entries": items},
                     ensure_ascii=False, indent=2))


def cmd_get(args):
    """读取完整记忆条目。"""
    project = args.project
    entry_path = memory_dir(project) / "entries" / f"{args.mem_id}.md"
    if not entry_path.exists():
        raise SystemExit(f"ERROR: 记忆条目不存在: {args.mem_id}")
    print(entry_path.read_text(encoding="utf-8"))


def cmd_profile(args):
    """显示或刷新研究画像。"""
    project = args.project
    if args.refresh:
        profile = refresh_profile(project)
    else:
        profile = load_profile(project)
        if not profile:
            profile = refresh_profile(project)
    print(json.dumps(profile, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_recall = sub.add_parser("recall", help="渲染紧凑上下文（profile + 近期条目）")
    p_recall.add_argument("project", help="projects/ 下的项目名")
    p_recall.set_defaults(func=cmd_recall)

    p_add = sub.add_parser("add", help="新增记忆条目")
    p_add.add_argument("project", help="projects/ 下的项目名")
    p_add.add_argument("--title", required=True, help="条目标题")
    p_add.add_argument("--intent", required=True, choices=list(INTENTS.keys()),
                       help=f"意图: {INTENTS}")
    p_add.add_argument("--content", help="条目内容")
    p_add.add_argument("--stdin", action="store_true", help="从 stdin 读取内容")
    p_add.add_argument("--tags", help="逗号分隔的标签")
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="列出记忆条目")
    p_list.add_argument("project", help="projects/ 下的项目名")
    p_list.add_argument("--intent", choices=list(INTENTS.keys()), help="按意图过滤")
    p_list.set_defaults(func=cmd_list)

    p_get = sub.add_parser("get", help="读取完整记忆条目")
    p_get.add_argument("project", help="projects/ 下的项目名")
    p_get.add_argument("mem_id", help="条目 ID（如 MEM-0001）")
    p_get.set_defaults(func=cmd_get)

    p_profile = sub.add_parser("profile", help="显示或刷新研究画像")
    p_profile.add_argument("project", help="projects/ 下的项目名")
    p_profile.add_argument("--refresh", action="store_true", help="LLM 重新提取画像")
    p_profile.set_defaults(func=cmd_profile)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
