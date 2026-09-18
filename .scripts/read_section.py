#!/usr/bin/env python3
"""read_section.py — wiki 页面 section-level 读取(跨平台，等价于 read_section.sh)

用途:只把指定 ## section 送入 LLM 上下文以省 token,而非整文件读取。
标准 section:Navigation | Core Triples | Content(见各子项目 SCHEMA.md「标准 section 结构」)

用法:
  python .scripts/read_section.py <page_path> <section_name>
  python .scripts/read_section.py academic/wiki/papers/sarthi-2024-raptor.md Navigation

与 read_section.sh 行为一致(同样的退出码与 stderr 文案);Windows 无 bash 时用本文件。

防退化规则(见 SCHEMA.md):
  1. section 名精确匹配(大小写敏感,去首尾空白)
  2. 同级 ## section 不重名(规范保证,脚本不检测)
  3. 代码块内禁止写 ## 标题(规范保证,脚本不解析代码块)
  4. 找不到 section → 非零退出 + stderr 报错 + 列出可用 section
  5. 截取失败绝不静默降级为整文件读取
"""
from __future__ import annotations

import sys
from pathlib import Path

USAGE = "Usage: read_section.py <page_path> <section_name>"
STANDARD = "Standard sections: Navigation | Core Triples | Content"


def is_heading(line: str) -> bool:
    """`## ` 开头(## 之后是空白)的二级标题行。"""
    return line.startswith("##") and len(line) > 2 and line[2] in " \t"


def heading_name(line: str) -> str:
    return line[2:].strip()


def extract(text: str, section: str) -> str:
    lines = text.splitlines()
    collected: list[str] = []
    inside = False
    for line in lines:
        if is_heading(line):
            if inside:
                break
            if heading_name(line) == section:
                inside = True
                collected.append(line)
                continue
        elif inside:
            collected.append(line)
    return "\n".join(collected)


def main() -> int:
    if len(sys.argv) < 3 or not sys.argv[1] or not sys.argv[2]:
        print(USAGE, file=sys.stderr)
        print(STANDARD, file=sys.stderr)
        return 2
    page = Path(sys.argv[1])
    section = sys.argv[2]
    if not page.is_file():
        print(f"ERROR: page not found: {page}", file=sys.stderr)
        return 3
    text = page.read_text(encoding="utf-8")
    content = extract(text, section)
    if not content.strip():
        print(f"ERROR: section '{section}' not found in {page}", file=sys.stderr)
        print("Available ## sections:", file=sys.stderr)
        found = False
        for number, line in enumerate(text.splitlines(), start=1):
            if is_heading(line):
                print(f"{number}:{line}", file=sys.stderr)
                found = True
        if not found:
            print("  (none — page has no ## sections)", file=sys.stderr)
        return 1
    sys.stdout.write(content + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
