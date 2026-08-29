#!/usr/bin/env python3
"""normalize_core_triples_source.py — Core Triples section 内来源标注半角→全角归一

目标格式(与全局 triples-rag.md 一致): object(arg)（来源：[[src]]）
  半角 (来源:[[xxx]])  →  全角 （来源：[[xxx]]）
  半角 (来源:本文)      →  全角 （来源：本文）
仅在每文件的 '## Core Triples' 到下一 '## ' 之间替换,不误伤正文。
阻塞2(arg 括号位置页/全局不一致)的页面侧清理;阻塞1(section 谓词序)是
rebuild_triples.py 排序行为,非本脚本范围。
用法: normalize_core_triples_source.py [--apply]   (默认 dry-run)
"""
import re, sys, pathlib

PATTERN = re.compile(r"\(来源:([^)]*)\)")

def process(path, apply):
    t = path.read_text(encoding="utf-8")
    m = re.search(r'^(## Core Triples\s*\n)(.*?)(?=^## |\Z)', t, re.M|re.S)
    if not m:
        return 0
    section = m.group(2)
    new_section, n = PATTERN.subn(r"（来源：\1）", section)
    if n == 0:
        return 0
    if apply:
        nt = t[:m.start(2)] + new_section + t[m.end(2):]
        path.write_text(nt, encoding="utf-8")
    return n

def main():
    apply = "--apply" in sys.argv
    files = list(pathlib.Path('.').rglob('*/wiki/**/*.md'))
    total = 0; touched = 0
    for f in files:
        n = process(f, apply)
        if n: total += n; touched += 1; print(f"  {n:3d}  {f}")
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"\n[{mode}] 归一 {total} 条来源标注,涉及 {touched} 个文件")
    if not apply:
        print("加 --apply 实际写入")

if __name__ == "__main__":
    main()
