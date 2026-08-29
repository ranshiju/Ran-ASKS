#!/usr/bin/env python3
"""Document-support guard for automatic direction edges."""
import importlib.util
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("direction_matcher.py")
spec = importlib.util.spec_from_file_location("direction_matcher", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_direction_requires_name_or_seed_in_document():
    assert module.direction_has_document_support(
        "量子信息", "We study a quantum circuit for quantum information processing."
    )


def test_load_direction_defs_accepts_standard_yaml_frontmatter():
    """合法 YAML 的未加引号 scalar 与块列表也必须被识别。"""
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        hubs = repo / "academic/wiki/hubs"
        hubs.mkdir(parents=True)
        (hubs / "子方向.md").write_text(
            "---\n"
            "title: 量子蒙特卡罗与神经量子态\n"
            "type: topic-hub\n"
            "parent: academic/wiki/hubs/纠缠与相变物理\n"
            "seeds:\n"
            "- 量子蒙特卡罗\n"
            "- 神经量子态\n"
            "- 费米子神经网络\n"
            "---\n",
            encoding="utf-8",
        )
        original_repo = module.REPO
        try:
            module.REPO = repo
            definitions = module._load_direction_defs()
        finally:
            module.REPO = original_repo
    subdirection = next(item for item in definitions if item["name"] == "量子蒙特卡罗与神经量子态")
    assert subdirection == {
        "name": "量子蒙特卡罗与神经量子态",
        "parent": "纠缠与相变物理",
        "seeds": ["量子蒙特卡罗", "神经量子态", "费米子神经网络"],
    }
    assert not module.direction_has_document_support(
        "量子信息", "We study hierarchical tensors for high-dimensional PDEs."
    )




def test_ensure_hub_seeds_supplements_from_keywords():
    """ensure_hub_seeds: seeds 不足时从 ## 关键词 质心自动补充。"""
    import importlib.util
    from pathlib import Path

    SCRIPT = Path(__file__).with_name("hub_split.py")
    spec = importlib.util.spec_from_file_location("hub_split_test", SCRIPT)
    hs = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(hs)

    REPO = Path(__file__).resolve().parent.parent
    hub_rel = "academic/wiki/hubs/test-ensure-seeds"
    hub_file = REPO / (hub_rel + ".md")

    try:
        hub_file.parent.mkdir(parents=True, exist_ok=True)
        # hub with keywords but empty seeds
        hub_file.write_text(
            "---\n"
            'title: "test-ensure-seeds"\n'
            "type: topic-hub\n"
            "hub_subtype: research-direction\n"
            'parent: "academic/wiki/hubs/quantum-info"\n'
            "seeds: []\n"
            "status: active\n"
            "---\n\n"
            "# test-ensure-seeds\n\n## 关键词\n\n"
            "- 量子计算\n- quantum computing\n- 量子门\n"
            "- quantum gate\n- 量子算法\n- quantum algorithm\n",
            encoding="utf-8",
        )
        assert hs._read_hub_seeds(hub_rel) == []

        seeds = hs.ensure_hub_seeds(hub_rel)
        assert len(seeds) >= 3, f"expected ≥3 seeds, got {len(seeds)}"
        assert len(seeds) <= hs.SUBHUB_SEED_COUNT

        re_read = hs._read_hub_seeds(hub_rel)
        assert re_read == seeds, "seeds not persisted to frontmatter"
    finally:
        if hub_file.exists():
            hub_file.unlink()


def test_ensure_hub_seeds_sufficient_returns_asis():
    """ensure_hub_seeds: seeds 充足时直接返回,不修改。"""
    import importlib.util
    from pathlib import Path

    SCRIPT = Path(__file__).with_name("hub_split.py")
    spec = importlib.util.spec_from_file_location("hub_split_test2", SCRIPT)
    hs = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(hs)

    REPO = Path(__file__).resolve().parent.parent
    hub_rel = "academic/wiki/hubs/test-ensure-sufficient"
    hub_file = REPO / (hub_rel + ".md")

    try:
        hub_file.parent.mkdir(parents=True, exist_ok=True)
        existing = ["seed-a", "seed-b", "seed-c", "seed-d"]
        hub_file.write_text(
            "---\n"
            'title: "test-ensure-sufficient"\n'
            "type: topic-hub\n"
            "hub_subtype: research-direction\n"
            'parent: "academic/wiki/hubs/quantum-info"\n'
            f"seeds: [{', '.join(existing)}]\n"
            "status: active\n"
            "---\n\n# test\n\n## 关键词\n\n- alpha\n",
            encoding="utf-8",
        )
        seeds = hs.ensure_hub_seeds(hub_rel)
        assert seeds == existing, f"expected {existing}, got {seeds}"
    finally:
        if hub_file.exists():
            hub_file.unlink()


def test_ensure_hub_seeds_no_keywords_returns_empty():
    """ensure_hub_seeds: seeds 和 keywords 都不足时返回原 seeds(空)。"""
    import importlib.util
    from pathlib import Path

    SCRIPT = Path(__file__).with_name("hub_split.py")
    spec = importlib.util.spec_from_file_location("hub_split_test3", SCRIPT)
    hs = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(hs)

    REPO = Path(__file__).resolve().parent.parent
    hub_rel = "academic/wiki/hubs/test-ensure-empty"
    hub_file = REPO / (hub_rel + ".md")

    try:
        hub_file.parent.mkdir(parents=True, exist_ok=True)
        hub_file.write_text(
            "---\n"
            'title: "test-ensure-empty"\n'
            "type: topic-hub\n"
            "hub_subtype: research-direction\n"
            'parent: "academic/wiki/hubs/quantum-info"\n'
            "seeds: []\n"
            "status: active\n"
            "---\n\n# test\n\n## 关键词\n\n",
            encoding="utf-8",
        )
        seeds = hs.ensure_hub_seeds(hub_rel)
        assert seeds == [], f"expected [], got {seeds}"
    finally:
        if hub_file.exists():
            hub_file.unlink()


def test_split_hub_accepts_graph_node_path_without_suffix():
    """dynamic_split 传入图节点路径（无 .md）时，split_hub 应正常处理。"""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).with_name("hub_split.py")
    spec = importlib.util.spec_from_file_location("hub_split_path_test", script)
    hs = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(hs)

    observed_paths = []
    original_threshold = hs.SPLIT_THRESHOLD
    original_parse = hs.parse_hub_keywords
    original_cluster = hs.cluster_hub_keywords
    try:
        hs.SPLIT_THRESHOLD = 2

        def parse_keywords(path):
            observed_paths.append(path)
            return ["keyword-a", "keyword-b"]

        hs.parse_hub_keywords = parse_keywords
        hs.cluster_hub_keywords = lambda _keywords: [
            {"seeds": ["seed-a"], "keywords": ["keyword-a"]},
            {"seeds": ["seed-b"], "keywords": ["keyword-b"]},
        ]
        report = hs.split_hub("academic/wiki/hubs/test-graph-path", dry_run=True)
        assert "skipped" not in report
        assert observed_paths == ["academic/wiki/hubs/test-graph-path.md"]
    finally:
        hs.SPLIT_THRESHOLD = original_threshold
        hs.parse_hub_keywords = original_parse
        hs.cluster_hub_keywords = original_cluster


def test_collect_all_hub_keywords_returns_list():
    """collect_all_hub_keywords: 返回非空 list(知识库已有 keyword)。"""
    kws = module.collect_all_hub_keywords()
    assert isinstance(kws, list)
    assert len(kws) > 0, "expected existing keywords, got empty"


def test_match_keyword_exact_match():
    """match_keyword_to_hub_keywords: 精确匹配命中(零 API 成本)。"""
    kws = module.collect_all_hub_keywords()
    if not kws:
        return
    target = kws[0]
    result = module.match_keyword_to_hub_keywords([target], existing_keywords=kws)
    assert target in result, f"exact match failed for {target!r}"
    assert result[target] == target


def test_match_keyword_merges_compatible_exact_chinese_component():
    existing = "矩阵乘积态matrix product state(MPS)"
    new = "矩阵乘积态（MPS）"
    result = module.match_keyword_to_hub_keywords([new], existing_keywords=[existing])
    assert result == {new: existing}


def test_match_keyword_keeps_conflicting_exact_component_unmerged():
    new = "状态thermal state"
    existing = ["状态quantum state"]
    assert module.exact_component_keyword_matches([new], existing) == {}
    assert module.has_conflicting_exact_component(new, existing)


def test_match_keyword_empty_inputs():
    """match_keyword_to_hub_keywords: 空输入返回空 dict。"""
    assert module.match_keyword_to_hub_keywords([]) == {}
    assert module.match_keyword_to_hub_keywords(["x"], existing_keywords=[]) == {}


def test_match_keyword_no_match_for_new_concept():
    """match_keyword_to_hub_keywords: 全新概念不命中(返回空 dict)。"""
    kws = module.collect_all_hub_keywords()
    if not kws:
        return
    result = module.match_keyword_to_hub_keywords(
        ["区块链blockchain"], existing_keywords=kws
    )
    assert "区块链blockchain" not in result, "should not match existing keywords"


def test_is_neg_excluded():
    """_is_neg_excluded: 精确匹配/子串包含 → True;无关 → False。"""
    from direction_matcher import _is_neg_excluded
    assert _is_neg_excluded("微分不等式", ["微分不等式"])
    assert _is_neg_excluded("微分不等式方法", ["微分不等式"])  # 子串包含
    assert not _is_neg_excluded("自动微分", ["微分不等式"])
    assert not _is_neg_excluded("量子纯度", ["微分不等式"])
    assert not _is_neg_excluded("任意", [])  # 空负向 seed
    assert not _is_neg_excluded("任意", None)


def main():
    test_direction_requires_name_or_seed_in_document()
    test_load_direction_defs_accepts_standard_yaml_frontmatter()
    test_ensure_hub_seeds_supplements_from_keywords()
    test_ensure_hub_seeds_sufficient_returns_asis()
    test_ensure_hub_seeds_no_keywords_returns_empty()
    test_split_hub_accepts_graph_node_path_without_suffix()
    test_collect_all_hub_keywords_returns_list()
    test_match_keyword_exact_match()
    test_match_keyword_merges_compatible_exact_chinese_component()
    test_match_keyword_keeps_conflicting_exact_component_unmerged()
    test_match_keyword_empty_inputs()
    test_match_keyword_no_match_for_new_concept()
    test_is_neg_excluded()
    print("direction matcher regression: PASS")


if __name__ == "__main__":
    main()
