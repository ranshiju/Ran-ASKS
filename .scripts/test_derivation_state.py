import tempfile
from pathlib import Path

import ingest_plan
from derivation_state import is_stale, provenance


def test_provenance_changes_only_when_inputs_change():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "raw.txt"
        path.write_text("one", encoding="utf-8")
        current = provenance(path, schema_version="s1", rule_version="r1", prompt_version="p1", model="m1")
        assert not is_stale(current, dict(current))
        changed = dict(current, prompt_version="p2")
        assert is_stale(current, changed)


def test_incremental_plan_only_reports_changed_raw():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        raw = root / "raw"
        wiki = root / "wiki"
        raw.mkdir()
        wiki.mkdir()
        (raw / "a.txt").write_text("a", encoding="utf-8")
        (raw / "b.txt").write_text("b", encoding="utf-8")
        state = {"raw": {"a.txt": ingest_plan.sha256_file(raw / "a.txt")}}
        plan = ingest_plan.make_plan(raw, wiki, state)
        assert plan["changed_raw"] == ["b.txt"]
        assert plan["removed_raw"] == []
