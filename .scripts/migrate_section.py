#!/usr/bin/env python3
"""migrate_section.py — 将旧 wiki 页迁移到标准 section 结构

做两件事:
1. 在第一个 ## section 前插入 ## Navigation(占位,待填)和 ## Core Triples(占位,待填)
2. 将所有二级 ## 标题降为 ### (包裹进 ## Content)

不做:
- 不生成 Navigation/Core Triples 内容(需人工/LLM 语义,留占位)
- 不改 frontmatter
- 不动锚点 {:#slug}
- 不改描述性 section 名(如 ## 核心决策 保留为 ### 核心决策,仅降级)

用法: migrate_section.py <page_path>
"""
import re
import sys
from pathlib import Path

def migrate(page_path: str) -> str:
    p = Path(page_path)
    if not p.exists():
        return f"❌ 文件不存在: {page_path}"
    c = p.read_text(encoding="utf-8")

    # 已迁移检测
    if re.search(r"^## Navigation\b", c, re.M):
        return f"⏭ 已含 ## Navigation,跳过: {page_path}"

    # 找 frontmatter 结束
    fm_end = 0
    if c.startswith("---\n"):
        m = re.search(r"^---\n", c[4:], re.M)
        if m:
            fm_end = 4 + m.end()

    body = c[fm_end:]

    # 找第一个 ## 标题(一级 section)
    first_h2 = re.search(r"^## .+", body, re.M)
    if not first_h2:
        return f"⚠ 无 ## section,无法迁移: {page_path}"

    # 拆分:首个 ## 前的内容(标题/导言) + section 区
    insert_pos = first_h2.start()
    preamble = body[:insert_pos]
    sections = body[insert_pos:]

    # 1. 在 sections 前插入 Navigation + Core Triples 占位
    nav_block = """## Navigation

<!-- TODO: 填导航概述(2-4 句,80-200 tokens,答"页面讲什么/解决什么问题/是否值得读") -->

## Core Triples

<!-- TODO: 填路由级关系(3-8 条,从 triples*.md 对应条目复制或新提取) -->

"""
    # 2. 所有 ## 降为 ### (包裹进 Content)
    migrated_sections = re.sub(r"^## ", "### ", sections, flags=re.M)

    # 3. 在 Navigation 块前加 ## Content 包裹标记? 不——Content 是旧 sections 的容器
    #    旧 sections 降为 ### 后,它们隐含在 ## Navigation/Core Triples 之后的正文区
    #    但标准结构要求 ## Content 显式包裹。加 ## Content 在第一个 ### 前
    migrated_sections = "## Content\n\n" + migrated_sections

    new_body = preamble + nav_block + migrated_sections
    new_content = c[:fm_end] + new_body

    p.write_text(new_content, encoding="utf-8")
    return f"✅ 迁移: {page_path} (插入 Navigation/Core Triples 占位 + ## Content 包裹 + 旧 ## 降 ###)"

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("用法: migrate_section.py <page_path>")
        sys.exit(2)
    print(migrate(sys.argv[1]))
