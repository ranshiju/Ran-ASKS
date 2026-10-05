#!/usr/bin/env python3
"""Run the deterministic offline Raw -> Wiki -> Graph -> evidence demonstration."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import graph_lib as gl
import source_locator as sl


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_demo_file(demo_root: Path, relative: str) -> Path:
    candidate = demo_root / relative
    resolved = candidate.resolve()
    if candidate.is_symlink() or not resolved.is_relative_to(demo_root.resolve()):
        raise ValueError(f"unsafe demo path: {relative}")
    if not resolved.is_file():
        raise ValueError(f"missing demo file: {relative}")
    return resolved


def load_scenario(workspace: Path) -> tuple[Path, dict]:
    demo_root = workspace / "examples" / "demo"
    scenario_path = safe_demo_file(demo_root, "scenario.json")
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    if scenario.get("schema") != "ran-asks-demo-v1":
        raise ValueError("unsupported demo scenario")
    for relative, expected in scenario.get("files", {}).items():
        actual = sha256(safe_demo_file(demo_root, relative))
        if actual != expected:
            raise ValueError(f"demo hash mismatch: {relative}")
    return demo_root, scenario


def build_navigation_graph(database: Path, scenario: dict) -> None:
    conn = gl.connect(database)
    try:
        gl.init_schema(conn)
        page = scenario["page"]
        concept = "examples/demo/concepts/cooling-failure"
        gl.ensure_node(conn, page, "Experiment B", "page", source_type="synthetic", has_raw=1)
        gl.ensure_node(conn, concept, "Cooling failure", "entity", source_type="synthetic")
        cursor = conn.execute(
            "INSERT INTO edges (subject,predicate,object,confidence,source,is_sr) VALUES (?,?,?,?,?,0)",
            (page, "failure-cause", concept, "traceable", scenario["source_locator"]),
        )
        gl.add_edge_evidence(conn, cursor.lastrowid, scenario["source_locator"])
        conn.commit()
    finally:
        conn.close()


def run_demo(workspace: Path = REPO) -> dict:
    root = workspace.resolve()
    demo_root, scenario = load_scenario(root)
    wiki_path = safe_demo_file(demo_root, "wiki/experiment-b.md")
    wiki_text = wiki_path.read_text(encoding="utf-8")
    locator = scenario["source_locator"]
    if locator not in wiki_text or scenario["answer"] not in wiki_text:
        raise ValueError("Wiki interpretation is not bound to the declared answer and source")
    source_relative, anchor = sl.split_locator(locator)
    source_path = (root / source_relative).resolve()
    if not source_path.is_relative_to(root) or source_path.is_symlink():
        raise ValueError("source locator escapes the demo workspace")
    if sl.locator_status(anchor, source_path) != "present":
        raise ValueError("source locator is not present")
    evidence = sl.read_locator_text(source_path, anchor)
    if not evidence or any(term not in evidence for term in scenario["evidence_contains"]):
        raise ValueError("source excerpt does not support the synthetic answer")

    with tempfile.TemporaryDirectory(prefix="ran-asks-demo-") as temporary:
        database = Path(temporary) / "graph.db"
        build_navigation_graph(database, scenario)
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        try:
            edge = conn.execute(
                "SELECT subject,predicate,object,source FROM edges WHERE subject=?",
                (scenario["page"],),
            ).fetchone()
        finally:
            conn.close()
    if not edge or edge["source"] != locator:
        raise ValueError("temporary graph did not preserve the evidence locator")
    return {
        "schema": "ran-asks-demo-result-v1",
        "status": "completed",
        "question": scenario["question"],
        "answer": scenario["answer"],
        "navigation": dict(edge),
        "wiki": wiki_path.relative_to(root).as_posix(),
        "evidence": {"locator": locator, "text": evidence},
        "authority": "Raw source excerpt",
        "network_calls": 0,
        "production_writes": 0,
    }


def render_text(result: dict) -> str:
    return "\n".join([
        "Ran-ASKS offline provenance demo", "",
        f"Question: {result['question']}",
        f"Graph: {result['navigation']['subject']} --{result['navigation']['predicate']}--> {result['navigation']['object']}",
        f"Wiki: {result['wiki']}",
        f"Raw: {result['evidence']['locator']}",
        f"Evidence: {result['evidence']['text'].replace(chr(10), ' ')}", "",
        f"Answer: {result['answer']}",
        "Authority: Raw source excerpt (Graph is navigation; Wiki is interpretation)",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=REPO)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_demo(args.workspace)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render_text(result))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
