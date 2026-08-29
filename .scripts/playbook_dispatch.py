#!/usr/bin/env python3
"""playbook_dispatch.py — 检索 playbook 命中条目，只返回相关局部内容

agent 收到指令后调本脚本，传入指令关键词；程序匹配触发词，
只输出命中的条目文本（## 到下一个 ---），不输出全文。

用法:
  playbook_dispatch.py "摄入 inbox"        # 检索命中条目
  playbook_dispatch.py "更新工程文档"
  playbook_dispatch.py --list              # 列出所有条目触发词
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLAYBOOK = REPO / "memory" / "playbooks" / "index.md"


def _normalize(s):
    """去空格、转小写，用于宽松匹配。"""
    return re.sub(r"\s+", "", s).lower()


def _parse_entries(text):
    """把 playbook 拆成条目列表。返回 [{title, triggers, body}]。"""
    # 按顶层 --- 分隔；第一段是头部说明，跳过
    raw = re.split(r"\n---\n", text)
    entries = []
    for chunk in raw[1:]:
        title_m = re.search(r"^##\s+(.+)", chunk, re.M)
        tw_m = re.search(r"\*\*触发词\*\*[：:]\s*(.+)", chunk)
        if not (title_m and tw_m):
            continue
        triggers = re.findall(r'「([^」]+)」', tw_m.group(1))
        entries.append({
            "title": title_m.group(1).strip(),
            "triggers": triggers,
            "body": chunk.strip(),
        })
    return entries


def dispatch(query):
    """根据查询词检索 playbook，返回命中条目文本（可能多条）。无命中返回 None。"""
    if not PLAYBOOK.exists():
        return None
    text = PLAYBOOK.read_text(encoding="utf-8")
    entries = _parse_entries(text)
    q = _normalize(query)
    if len(q) < 2:
        return None
    matches = []
    for e in entries:
        for t in e["triggers"]:
            tn = _normalize(t)
            if len(tn) < 2:
                continue
            # 双向子串匹配：查询含触发词，或触发词含查询
            if tn in q or q in tn:
                matches.append(e["body"])
                break
    if not matches:
        return None
    return "\n\n---\n\n".join(matches)


def list_entries():
    """列出所有条目的标题 + 触发词。"""
    if not PLAYBOOK.exists():
        return
    text = PLAYBOOK.read_text(encoding="utf-8")
    for e in _parse_entries(text):
        triggers = " / ".join(e["triggers"])
        print(f"  {e['title']}  ←  {triggers}")


def main():
    if len(sys.argv) < 2:
        print("用法: playbook_dispatch.py '指令关键词'  或  --list", file=sys.stderr)
        sys.exit(1)
    if sys.argv[1] == "--list":
        list_entries()
        return
    query = " ".join(sys.argv[1:])
    result = dispatch(query)
    if result:
        print(result)
    else:
        print("未命中 playbook，正常推进", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
