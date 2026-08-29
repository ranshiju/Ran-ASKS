#!/usr/bin/env bash
# read_section.sh — wiki 页面 section-level 读取(物理共置 + 逻辑分离)
#
# 用途:只把指定 ## section 送入 LLM 上下文以省 token,而非整文件读取。
# 标准 section:Navigation | Core Triples | Content(见各子项目 SCHEMA.md「标准 section 结构」)
#
# 用法:
#   read_section.sh <page_path> <section_name>
#   read_section.sh academic/wiki/papers/sarthi-2024-raptor.md Navigation
#
# 防退化规则(见 SCHEMA.md):
#   1. section 名精确匹配(大小写敏感,去首尾空白)
#   2. 同级 ## section 不重名(规范保证,脚本不检测)
#   3. 代码块内禁止写 ## 标题(规范保证,脚本不解析代码块)
#   4. 找不到 section → 非零退出 + stderr 报错 + 列出可用 section
#   5. 截取失败绝不静默降级为整文件读取

set -euo pipefail

page="${1:-}"
section="${2:-}"

if [[ -z "$page" || -z "$section" ]]; then
  echo "Usage: read_section.sh <page_path> <section_name>" >&2
  echo "Standard sections: Navigation | Core Triples | Content" >&2
  exit 2
fi

if [[ ! -f "$page" ]]; then
  echo "ERROR: page not found: $page" >&2
  exit 3
fi

# 提取 `## <section>` 标题行到下一个 `^## ` 之间的内容(含标题行)
content=$(awk -v sec="$section" '
  /^##[[:space:]]/ {
    if (in_sec) exit
    name = $0
    sub(/^##[[:space:]]+/, "", name)
    sub(/[[:space:]]*$/, "", name)
    if (name == sec) { in_sec = 1; print $0; next }
  }
  in_sec { print }
' "$page")

if [[ -z "$content" ]]; then
  echo "ERROR: section '$section' not found in $page" >&2
  echo "Available ## sections:" >&2
  if ! grep -n "^##[[:space:]]" "$page" >&2; then
    echo "  (none — page has no ## sections)" >&2
  fi
  exit 1
fi

printf '%s\n' "$content"
