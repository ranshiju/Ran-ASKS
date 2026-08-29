#!/usr/bin/env python3
"""check_wikilinks.py — 校验文档中 [[wikilink]] 是否指向存在的 wiki 页(只查悬空)

用于写作(WRITE)输出引用 wiki 时的可追溯性校验。复用 ingest_check 的解析逻辑
(build_wiki_index/resolve_wikilink/extract_wikilinks),不查 frontmatter/section
(写作输出是 projects/ 普通 md,无 wiki frontmatter)。符合 3.7 壳核分工:
悬空是确定性结构检查(壳),写作语义质量仍由 LLM 自检。

用法: check_wikilinks.py <file1> [file2 ...]
退出码: 0 = 无悬空; 1 = 有悬空
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest_check import build_wiki_index, resolve_wikilink, extract_wikilinks


def check(path):
    text = Path(path).read_text(encoding="utf-8")
    links = extract_wikilinks(text)
    if not links:
        return []
    rel_paths, basenames = build_wiki_index()
    dangling = []
    for target in sorted(links):
        if not resolve_wikilink(target, rel_paths, basenames):
            dangling.append(target)
    return dangling


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    total = 0
    for a in sys.argv[1:]:
        p = Path(a)
        if not p.exists():
            print(f"WARN: 文件不存在: {a}", file=sys.stderr)
            continue
        dangling = check(p)
        if dangling:
            print(f"⚠ {a}:悬空 wikilink {len(dangling)} 个")
            for d in dangling:
                print(f"  - [[{d}]]")
            total += len(dangling)
        else:
            links = extract_wikilinks(p.read_text(encoding="utf-8"))
            print(f"✅ {a}:无悬空({len(links)} 个 wikilink 全部可解析)")
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    main()
