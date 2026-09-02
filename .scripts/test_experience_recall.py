#!/usr/bin/env python3
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / ".scripts" / "experience_recall.py"
SPEC = importlib.util.spec_from_file_location("experience_recall_test", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def recall(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "recall", *args],
        capture_output=True,
        text=True,
        check=True,
    )


def query_experience_fixture(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "query.md").write_text(
        "```yaml\n"
        "patterns:\n"
        "  - id: cross-language-entity-topic\n"
        "    status: active\n"
        "    events: [start]\n"
        "    triggers: [中文人名, 英文别名, 主题]\n"
        "    advice: resolve aliases before topic lookup\n"
        "    boundaries: navigation only\n"
        "    source_trace: test-fixture\n"
        "```\n",
        encoding="utf-8",
    )
    return directory


def test_playbook_hit_blocks_experience():
    result = recall(
        "--capability", "query",
        "--context", "人名 量子纠缠 别名",
        "--event", "start",
        "--playbook-hit",
    )
    payload = json.loads(result.stdout)
    assert payload["patterns"] == []
    assert payload["skipped_reason"] == "playbook_priority"


def test_query_recall_is_capped_and_scored():
    with tempfile.TemporaryDirectory() as directory:
        payload = MODULE.recall(
            capability="query",
            context="中文人名 英文别名 主题",
            event="start",
            experience_dir=query_experience_fixture(Path(directory)),
        )
    assert 1 <= len(payload["patterns"]) <= 3
    assert payload["patterns"][0]["id"] == "cross-language-entity-topic"
    assert payload["patterns"][0]["score"] > 0


def test_event_mismatch_is_not_recalled():
    with tempfile.TemporaryDirectory() as directory:
        payload = MODULE.recall(
            capability="query",
            context="中文人名 英文别名 主题",
            event="retry",
            experience_dir=query_experience_fixture(Path(directory)),
        )
    assert payload["patterns"] == []
    assert payload["reason"] == "no_match"


def test_capability_files_are_bounded():
    directory = REPO / "memory" / "experiences"
    if not directory.exists():
        return
    for capability in ("query", "ingest", "write", "build"):
        path = directory / f"{capability}.md"
        assert path.stat().st_size <= 6144
        content = path.read_text(encoding="utf-8")
        assert "raw/" not in content
        assert "status:" in content


def test_unknown_capability_rejected():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "recall", "--capability", "unknown"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "invalid choice" in result.stderr


if __name__ == "__main__":
    test_playbook_hit_blocks_experience()
    test_query_recall_is_capped_and_scored()
    test_event_mismatch_is_not_recalled()
    test_capability_files_are_bounded()
    test_unknown_capability_rejected()
    print("experience recall regression: PASS")
