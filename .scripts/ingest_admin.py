#!/usr/bin/env python3
"""ingest_admin.py — 行政文档摄入的薄包装。

实际逻辑已迁移至 ingest_document.py（通用文档摄入编排器，支持 admin/teaching/business）。
本文件保留为入口别名，兼容已有调用与测试。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".scripts"))

import ingest_document as _doc

# ===== 向后兼容：重导出旧接口名 =====
DOMAIN_CONFIG = _doc.DOMAIN_CONFIG
PAGE_TYPE_TO_SUBDIR = _doc.DOMAIN_CONFIG["admin"]["type_to_subdir"]
MAX_RETRIES = _doc.MAX_RETRIES
WIKI_DELIMITER = _doc.WIKI_DELIMITER
SLOTS_DELIMITER = _doc.SLOTS_DELIMITER

run = _doc.run
slugify = _doc.slugify
progress = _doc.progress
parse_delimited = _doc.parse_delimited
extract_doc_text = _doc.extract_doc_text
extract_admin_date = _doc.extract_admin_date
generate_admin_id = _doc.generate_admin_id
ensure_unique_admin_id = _doc.ensure_unique_admin_id
get_subdir = _doc.get_subdir
normalize_slots = _doc.normalize_slots
is_clearly_descriptive = _doc.is_clearly_descriptive
parse_delimited = _doc.parse_delimited

step_dedup_check = _doc.step_dedup_check
step_preprocess = _doc.step_preprocess
step_write_wiki = _doc.step_write_wiki
step_validate_wiki = _doc.step_validate_wiki
step_write_slots = _doc.step_write_slots
step_fill_semantics = _doc.step_fill_semantics
step_validate_semantics = _doc.step_validate_semantics
step_repair_slots = _doc.step_repair_slots
step_finalize = _doc.step_finalize
step_update_graph = _doc.step_update_graph
step_validate_graph = _doc.step_validate_graph
step_finalize_tail = _doc.step_finalize_tail
run_pipeline = _doc.run_pipeline


def build_admin_wiki_prompt(doc_text, admin_id, date_str, errors=None):
    """向后兼容：旧函数名 → ingest_document.build_doc_wiki_prompt(subproject='admin')。"""
    return _doc.build_doc_wiki_prompt(doc_text, admin_id, date_str, "admin", errors)


def build_admin_slots_prompt(wiki_content, errors=None):
    """向后兼容：旧函数名 → ingest_document.build_doc_slots_prompt(subproject='admin')。"""
    return _doc.build_doc_slots_prompt(wiki_content, "admin", errors)


def main():
    """CLI 入口：转发到 ingest_document.py --subproject admin。"""
    import argparse
    parser = argparse.ArgumentParser(description="行政文档摄入（薄包装 → ingest_document.py）")
    parser.add_argument("--file", help="inbox/ 下的行政文档文件路径")
    parser.add_argument("--resume", help="恢复已有事务 ID")
    parser.add_argument("--verbose", action="store_true", help="进度打印到 stdout")
    args = parser.parse_args()
    # 转发到 ingest_document，固定 subproject=admin
    fwd_args = [str(Path(__file__).resolve())]
    if args.file:
        fwd_args += ["--file", args.file, "--subproject", "admin"]
    if args.resume:
        fwd_args += ["--resume", args.resume]
    if args.verbose:
        fwd_args += ["--verbose"]
    # 直接调 ingest_document.main 的逻辑（复用 state 初始化）
    import ingest_document
    # 模拟 sys.argv
    old_argv = sys.argv
    sys.argv = fwd_args
    try:
        ingest_document.main()
    finally:
        sys.argv = old_argv


if __name__ == "__main__":
    main()
