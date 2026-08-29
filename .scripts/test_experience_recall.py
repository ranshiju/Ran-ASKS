#!/usr/bin/env python3
import json
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / ".scripts" / "experience_recall.py"


def recall(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "recall", *args],
        capture_output=True,
        text=True,
        check=True,
    )


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
    result = recall(
        "--capability", "query",
        "--context", "中文人名 英文别名 主题",
        "--event", "start",
    )
    payload = json.loads(result.stdout)
    assert 1 <= len(payload["patterns"]) <= 3
    assert payload["patterns"][0]["id"] == "cross-language-entity-topic"
    assert payload["patterns"][0]["score"] > 0


def test_event_mismatch_is_not_recalled():
    result = recall(
        "--capability", "query",
        "--context", "中文人名 英文别名 主题",
        "--event", "retry",
    )
    payload = json.loads(result.stdout)
    assert payload["patterns"] == []
    assert payload["reason"] == "no_match"


def test_capability_files_are_bounded():
    directory = REPO / "memory" / "experiences"
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
