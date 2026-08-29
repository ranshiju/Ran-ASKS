#!/usr/bin/env python3
"""predicate_governance.py 纯代码回归测试。"""
import importlib.util
import json
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("predicate_governance.py")
spec = importlib.util.spec_from_file_location("predicate_governance", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def records(predicate, count, prefix="paper"):
    return [{
        "predicate": predicate,
        "subject": "本论文",
        "object": f"对象-{index}",
        "wiki_path": f"academic/wiki/papers/{prefix}-{index}",
        "paper_id": f"{prefix}-{index}",
    } for index in range(count)]


def test_alias_normalizes_to_registered_predicate():
    entries = module.classify(records("用于", 1), module.DEFAULT_CONFIG)
    assert set(entries) == {"应用于"}
    assert entries["应用于"]["aliases"] == ["用于"]
    assert module.normalize_predicate("用于") == "应用于"


def test_load_config_overrides_thresholds():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "config.yaml"
        path.write_text("formal_min_pages: 4\n", encoding="utf-8")
        assert module.load_config(path)["formal_min_pages"] == 4


def test_three_consistent_pages_enter_observation():
    entries = module.classify(records("表示", 3), module.DEFAULT_CONFIG)
    assert entries["表示"]["status"] == "observation"


def test_ten_consistent_pages_enter_formal():
    entries = module.classify(records("表示", 10), module.DEFAULT_CONFIG)
    assert entries["表示"]["status"] == "formal"


def test_inconsistent_subjects_remain_candidate():
    rows = records("表示", 3)
    rows[1]["subject"] = "概念A"
    rows[2]["subject"] = "概念B"
    entries = module.classify(rows, module.DEFAULT_CONFIG)
    assert entries["表示"]["status"] == "candidate"


def test_govern_writes_auditable_state_and_registry():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        queue, state, registry = root / "queue.jsonl", root / "state.json", root / "registry.json"
        queue.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in records("表示", 10)) + "\n", encoding="utf-8")
        summary = module.govern(queue, state, registry)
        assert summary == {"candidates": 1, "observation": 0, "formal": 1}
        assert json.loads(state.read_text(encoding="utf-8"))["predicates"]["表示"]["status"] == "formal"
        assert json.loads(registry.read_text(encoding="utf-8"))["formal"] == ["表示"]


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"predicate governance regression: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()
