#!/usr/bin/env python3
"""Resumable inbox PDF transaction built from the existing guarded tools.

This command never guesses a batch route.  `plan` is read-only; `prepare` extracts
one PDF and creates a temporary wiki skeleton; `complete` atomically finalizes,
ingests graph semantics, verifies graph evidence, and clears only the
committed inbox source.  Each transition is persisted in temp/inbox-state/.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".scripts"))
import inbox_state
import trash_util


def repo_path(value: str, label: str) -> Path:
    path = (REPO / value).resolve()
    if REPO not in path.parents:
        raise ValueError(f"{label} 必须位于仓库内")
    return path


def run(command: list[str]) -> str:
    result = subprocess.run(command, cwd=REPO, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode:
        raise RuntimeError(f"命令失败({result.returncode}): {' '.join(command)}")
    return result.stdout


def write_manifest(extract_dir: Path) -> None:
    raw_files = [name for name in ("paper.pdf", "paper.md", "source.yaml", "parse_meta.yaml")
                 if (extract_dir / name).is_file()]
    if not {"paper.pdf", "paper.md"}.issubset(raw_files):
        raise ValueError("提取未生成 paper.pdf 和 paper.md")
    (extract_dir / "manifest.json").write_text(
        json.dumps({"raw_files": raw_files, "wiki_file": "wiki.md"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def cmd_plan(_: argparse.Namespace) -> None:
    run([sys.executable, str(REPO / ".scripts/inbox_plan.py")])


def cmd_prepare(args: argparse.Namespace) -> None:
    source = repo_path(args.source, "--source")
    inbox = (REPO / "inbox").resolve()
    if inbox not in source.parents or source.suffix.lower() != ".pdf":
        raise ValueError("--source 必须是 inbox/ 下的 PDF")
    extract_dir = REPO / "temp" / "inbox-extract" / args.paper_id
    if extract_dir.exists():
        raise ValueError(f"事务临时目录已存在，请使用 --resume 或新的 transaction-id: {extract_dir}")
    raw_dir = repo_path(args.raw_dir, "--raw-dir")
    wiki_path = repo_path(args.wiki_path, "--wiki-path")
    run([sys.executable, str(REPO / ".scripts/extractor.py"), "--external-pdf", str(source),
         "--paper", args.paper_id, "--papers-dir", "temp/inbox-extract"])
    write_manifest(extract_dir)
    run([sys.executable, str(REPO / ".scripts/wiki_skeleton.py"), "--page", wiki_path.relative_to(REPO).as_posix(),
         "--raw", (extract_dir / "paper.md").relative_to(REPO).as_posix(), "--source", args.source_ref,
         "--output", (extract_dir / "wiki.md").relative_to(REPO).as_posix()])
    inbox_state.save(args.transaction_id, {
        "status": "prepared", "source": source.relative_to(REPO).as_posix(), "paper_id": args.paper_id,
        "raw_dir": raw_dir.relative_to(REPO).as_posix(), "wiki_path": wiki_path.relative_to(REPO).as_posix(),
        "source_ref": args.source_ref, "extract_dir": extract_dir.relative_to(REPO).as_posix(),
    })
    print(f"✅ prepared: temp/inbox-state/{args.transaction_id}.json")


def cmd_complete(args: argparse.Namespace) -> None:
    state = inbox_state.load(args.transaction_id)
    if not state or state.get("status") not in {"prepared", "finalized"}:
        raise ValueError("事务不存在或不处于可完成状态；先运行 prepare")
    extract_dir = repo_path(state["extract_dir"], "extract_dir")
    wiki_path = repo_path(state["wiki_path"], "wiki_path")
    raw_dir = repo_path(state["raw_dir"], "raw_dir")
    source = repo_path(state["source"], "source")
    if state["status"] == "prepared":
        output = run([sys.executable, str(REPO / ".scripts/inbox_finalize.py"), "--paper-id", state["paper_id"],
                      "--raw-dir", str(raw_dir), "--wiki-path", str(wiki_path), "--extract-dir", str(extract_dir)])
        receipt_match = re.search(r"receipt:\s*(.+)", output)
        if not receipt_match:
            raise RuntimeError("inbox_finalize 未返回回执路径")
        receipt = Path(receipt_match.group(1).strip()).resolve()
        if not receipt.is_file() or REPO not in receipt.parents:
            raise RuntimeError(f"inbox_finalize 回执无效: {receipt}")
        state["status"] = "finalized"
        state["receipt"] = receipt.relative_to(REPO).as_posix()
        inbox_state.save(args.transaction_id, state)
    if args.semantic:
        semantic = repo_path(args.semantic, "--semantic")
    else:
        semantic = REPO / "temp" / "inbox-state" / f"{args.transaction_id}-semantic.txt"
        draft = REPO / "temp" / "inbox-state" / f"{args.transaction_id}-api-draft.json"
        command = [sys.executable, str(REPO / ".scripts/api_ingest.py"), "--raw",
                   (raw_dir / "paper.md").relative_to(REPO).as_posix(), "--output",
                   draft.relative_to(REPO).as_posix(), "--apply-page", wiki_path.relative_to(REPO).as_posix(),
                   "--semantic-output", semantic.relative_to(REPO).as_posix(), "--resolve-pending"]
        for candidate in args.candidate:
            command.extend(["--candidate", candidate])
        if args.agent_draft:
            command.extend(["--agent-draft", args.agent_draft])
        run(command)
    run([sys.executable, str(REPO / ".scripts/graph_ingest.py"), "ingest", "--page",
         wiki_path.relative_to(REPO).with_suffix("").as_posix(), "--semantic", str(semantic)])
    run([sys.executable, str(REPO / ".scripts/ingest_check.py"), str(wiki_path), "--graph"])
    if not source.is_file():
        raise ValueError(f"inbox 原文件不存在，拒绝清理: {source}")
    trash_util.trash_path(source)
    if extract_dir.exists():
        trash_util.trash_path(extract_dir)
    state["status"] = "completed"
    inbox_state.save(args.transaction_id, state)
    print(f"✅ completed: {args.transaction_id}")


def cmd_complete_batch(args: argparse.Namespace) -> None:
    """Complete prepared items sequentially; each item retains its own recoverable state."""
    for transaction_id in args.transaction_id:
        cmd_complete(argparse.Namespace(
            transaction_id=transaction_id,
            semantic=None,
            candidate=args.candidate,
            agent_draft=None,
        ))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan")
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--transaction-id", required=True)
    prepare.add_argument("--source", required=True, help="inbox/ 下的 PDF")
    prepare.add_argument("--paper-id", required=True)
    prepare.add_argument("--raw-dir", required=True)
    prepare.add_argument("--wiki-path", required=True)
    prepare.add_argument("--source-ref", required=True, help="写入 sources 的最终相对 raw/paper.md 路径")
    complete = commands.add_parser("complete")
    complete.add_argument("--transaction-id", required=True)
    complete.add_argument("--semantic", help="已验证的 graph_ingest 语义槽；省略时自动调用 api_ingest")
    complete.add_argument("--candidate", action="append", default=[], help="api_ingest 可选择的既有关键词")
    complete.add_argument("--agent-draft", help="API pending 后的受限 Agent JSON 草稿")
    complete_batch = commands.add_parser("complete-batch")
    complete_batch.add_argument("--transaction-id", action="append", required=True,
                                help="已 prepare 的事务 ID；按给定顺序逐项完成")
    complete_batch.add_argument("--candidate", action="append", default=[], help="各项共用的 API 候选关键词")
    args = parser.parse_args()
    try:
        {"plan": cmd_plan, "prepare": cmd_prepare, "complete": cmd_complete,
         "complete-batch": cmd_complete_batch}[args.command](args)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}")


if __name__ == "__main__":
    main()
