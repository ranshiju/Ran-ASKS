#!/usr/bin/env python3
"""wg.py 横向能力面薄包回归测试。

测试分层：
- 纯函数：envelope 契约、collect_sources_from_edges、build_parser
- 错误处理：query_graph_json 对 error dict 的检测（修复项）
- read-raw locator 逻辑：用 temp/ 下临时 md 验证 present/missing/全篇/二进制
"""
import importlib.util
import io
import json
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).with_name("wg.py")
spec = importlib.util.spec_from_file_location("wg", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

REPO = Path(__file__).resolve().parent.parent
TEMP_TEST_DIR = REPO / "temp" / "test_wg_tmp"


def setup_temp_file(name: str, content: str) -> Path:
    """在 temp/test_wg_tmp/ 下建临时 md，返回 repo 相对路径（无后缀）。"""
    TEMP_TEST_DIR.mkdir(parents=True, exist_ok=True)
    p = TEMP_TEST_DIR / f"{name}.md"
    p.write_text(content, encoding="utf-8")
    return p


def cleanup():
    if TEMP_TEST_DIR.exists():
        shutil.rmtree(TEMP_TEST_DIR)


def capture_call(func, *args, **kw) -> dict:
    """调用 cmd_* 函数，捕获 stdout JSON 并解析。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = func(*args, **kw)
    out = buf.getvalue().strip()
    data = json.loads(out)
    data["_rc"] = rc
    return data


# ============ envelope 契约 ============

def test_envelope_has_all_fields():
    """envelope 输出含 6 个必需字段 + rc=0。"""
    d = capture_call(module.envelope, "lookup", ["a"], sources=["s"], status="ok")
    for k in ("ok", "action", "result", "sources", "status", "error"):
        assert k in d, f"缺字段 {k}"
    assert d["action"] == "lookup"
    assert d["result"] == ["a"]
    assert d["sources"] == ["s"]
    assert d["status"] == "ok"
    assert d["error"] == ""
    assert d["_rc"] == 0


def test_envelope_error_status_implies_ok_false():
    """status=error 默认 ok=False。"""
    d = capture_call(module.envelope, "x", None, status="error", error="boom")
    assert d["ok"] is False
    assert d["error"] == "boom"


def test_envelope_explicit_ok_overrides_status():
    """status=empty + ok=False：missing locator 场景（read-raw 修复项）。"""
    d = capture_call(module.envelope, "read-raw", {}, status="empty", ok=False, error="missing")
    assert d["ok"] is False
    assert d["status"] == "empty"


# ============ collect_sources_from_edges ============

def test_collect_sources_from_edges_normal():
    """从 edges 列表提取 source locator，去重。"""
    obj = {"edges": [
        {"source": "a#nav"},
        {"source": "b#full"},
        {"source": "a#nav"},  # 重复
    ]}
    srcs = module.collect_sources_from_edges(obj)
    assert srcs == ["a#nav", "b#full"]


def test_collect_sources_empty():
    """无 edges 或非 dict → 空列表。"""
    assert module.collect_sources_from_edges({"edges": []}) == []
    assert module.collect_sources_from_edges({}) == []
    assert module.collect_sources_from_edges(None) == []


def test_collect_sources_skips_missing_source():
    """edge 无 source 字段 → 跳过。"""
    obj = {"edges": [{"predicate": "x"}, {"source": "a#nav"}]}
    assert module.collect_sources_from_edges(obj) == ["a#nav"]


# ============ build_parser ============

def test_parser_has_all_subcommands():
    """横向能力全部注册。"""
    ap = module.build_parser()
    subactions = {a.option_strings[0].lstrip("-") if a.option_strings else a.dest
                  for a in ap._subparsers._actions
                  if hasattr(a, "choices") and a.choices}
    # 子命令名在 choices
    for cmd in ("lookup", "neighbors", "relations", "hub-of", "read-section",
                "read-raw", "recall", "remember", "abbr", "frontier"):
        assert cmd in _subcommand_names(ap), f"缺子命令 {cmd}"


def _subcommand_names(ap):
    for a in ap._actions:
        if hasattr(a, "choices") and a.choices:
            return set(a.choices)
    return set()


def test_parser_defaults():
    """neighbors depth 默认 2，relations predicate 默认空。"""
    ap = module.build_parser()
    ns = ap.parse_args(["neighbors", "somepage"])
    assert ns.depth == 2
    ns = ap.parse_args(["relations", "p"])
    assert ns.predicate == ""
    ns = ap.parse_args(["read-section", "academic/wiki/demo.md#method"])
    assert ns.section == ""


# ============ Wiki section locator + Raw footnotes ============

def setup_locator_wiki() -> tuple[Path, Path]:
    raw = TEMP_TEST_DIR / "raw" / "document.md"
    wiki = TEMP_TEST_DIR / "wiki" / "page.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    wiki.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("# Raw\n\n第一条事实。\n第二条事实。\n第三条事实。\n", encoding="utf-8")
    raw_rel = str(raw.relative_to(REPO))
    wiki.write_text(
        "# Demo\n\n"
        "## Retrieval Control\n\n"
        "系统按证据缺口继续检索。[^r1]\n\n"
        "### Stop rule\n\n证据充分时停止。[^r2]\n\n"
        "## Other\n\n不应进入上一节。\n\n"
        "## Sources\n\n"
        f"[^r1]: {raw_rel}#L3\n"
        f"[^r2]: {raw_rel}#L4\n",
        encoding="utf-8",
    )
    return raw, wiki


def test_wiki_locator_reads_one_section_and_raw_citations():
    _raw, wiki = setup_locator_wiki()
    try:
        rel = str(wiki.relative_to(REPO))
        d = capture_call(module.cmd_read_section,
                         type("A", (), {"page": f"{rel}#retrieval-control", "section": ""})())
        assert d["ok"] is True
        assert d["result"]["section"] == "retrieval-control"
        assert "Stop rule" in d["result"]["text"]
        assert "不应进入上一节" not in d["result"]["text"]
        assert d["result"]["raw_citations"][0].endswith("#L3")
        assert d["sources"] == d["result"]["raw_citations"]
    finally:
        cleanup()


def test_wiki_locator_minimal_validation():
    _raw, wiki = setup_locator_wiki()
    try:
        assert module.wl.validate_wiki_page(wiki, require_citations=True) == []
        text = wiki.read_text(encoding="utf-8").replace(
            "## Other", "## Retrieval Control", 1).replace("[^r2]:", "[^missing]:", 1)
        wiki.write_text(text, encoding="utf-8")
        errors = module.wl.validate_wiki_page(wiki, require_citations=True)
        assert any("heading slug 重复" in error for error in errors)
        assert any("脚注未定义" in error for error in errors)
    finally:
        cleanup()


def test_wiki_graph_source_points_to_cited_section():
    _raw, wiki = setup_locator_wiki()
    try:
        source, evidence = module.wl.graph_wiki_source(wiki, "证据缺口")
        assert source.endswith("/wiki/page#retrieval-control")
        assert evidence and evidence[0].endswith("#L3")
    finally:
        cleanup()


def test_abbr_envelope():
    """abbr 复用图 search，并在无命中时返回 empty envelope。"""
    orig = module.subprocess.run
    module.subprocess.run = lambda *a, **kw: FakeCompleted(
        json.dumps({"keyword": "XYZ", "count": 0, "nodes": []}))
    try:
        d = capture_call(module.cmd_abbr, type("A", (), {"term": "XYZ"})())
        assert d["ok"] is False
        assert d["status"] == "empty"
        assert d["result"]["matches"] == []
    finally:
        module.subprocess.run = orig


def test_frontier_wrapper_json_envelope():
    """frontier ask 复用 frontier.py 并保留 Raw evidence sources。"""
    orig = module.subprocess.run
    module.subprocess.run = lambda *a, **kw: FakeCompleted(json.dumps({
        "status": "captured", "question_id": "Q-test",
        "raw_evidence": ["academic/raw/references/demo/paper.md#L7"],
    }))
    try:
        args = type("A", (), {
            "frontier_cmd": "ask", "question": "问题", "topk": 6, "no_ai": True,
        })()
        d = capture_call(module.cmd_frontier, args)
        assert d["ok"] is True
        assert d["result"]["question_id"] == "Q-test"
        assert d["sources"] == ["academic/raw/references/demo/paper.md#L7"]
    finally:
        module.subprocess.run = orig


def test_frontier_answer_wrapper():
    orig = module.subprocess.run
    module.subprocess.run = lambda *a, **kw: FakeCompleted(json.dumps({
        "id": "Q-test", "status": "completed", "kb_state": "partial",
    }))
    try:
        args = type("A", (), {
            "frontier_cmd": "answer", "record_id": "Q-test", "no_ai": False,
        })()
        d = capture_call(module.cmd_frontier, args)
        assert d["ok"] is True
        assert d["result"]["kb_state"] == "partial"
    finally:
        module.subprocess.run = orig


# ============ query_graph_json error 检测（修复项） ============

class FakeCompleted:
    def __init__(self, stdout, stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_query_graph_json_detects_error_dict():
    """query_graph 返回 {"error":...} 但 rc=0 时，wg 识别为 _error（修复项）。"""
    orig = module.subprocess.run
    module.subprocess.run = lambda *a, **kw: FakeCompleted(json.dumps({"error": "节点不存在: foo"}))
    try:
        res = module.query_graph_json("neighbors", ["foo"])
        assert "_error" in res, "应检测 error dict"
        assert "节点不存在" in res["_error"]
    finally:
        module.subprocess.run = orig


def test_query_graph_json_normal_dict():
    """正常 JSON 无 error key → 原样返回。"""
    orig = module.subprocess.run
    module.subprocess.run = lambda *a, **kw: FakeCompleted(json.dumps({"nodes": [{"path": "x"}]}))
    try:
        res = module.query_graph_json("search", ["x"])
        assert "_error" not in res
        assert res["nodes"] == [{"path": "x"}]
    finally:
        module.subprocess.run = orig


# ============ read-raw locator 逻辑 ============

def test_read_raw_present_section():
    """locator 指向存在的 section → 返回该 section 正文。"""
    content = "# Title\n\n## Navigation\n\n这是导航内容。\n\n## Content\n\n正文。\n"
    p = setup_temp_file("present_test", content)
    try:
        rel = str(p.resolve().relative_to(REPO))
        d = capture_call(module.cmd_read_raw,
                         type("A", (), {"locator": f"{rel}#Navigation"})())
        assert d["ok"] is True
        assert d["status"] == "ok"
        assert "导航内容" in d["result"]["text"]
        assert "正文。" not in d["result"]["text"]
    finally:
        cleanup()


def test_read_raw_missing_section():
    """locator 指向不存在的 section → status=empty, ok=False（修复项）。"""
    content = "## Navigation\n\n导航。\n"
    p = setup_temp_file("missing_test", content)
    try:
        rel = str(p.resolve().relative_to(REPO))
        d = capture_call(module.cmd_read_raw,
                         type("A", (), {"locator": f"{rel}#不存在段"})())
        assert d["ok"] is False
        assert d["status"] == "empty"
        assert d["result"]["locator_status"] == "missing"
    finally:
        cleanup()


def test_read_raw_full_doc():
    """locator=全篇 仅是文件级 provenance，不得把全文交给 LLM。"""
    content = "# Full Doc\n\n全文内容在这里。\n"
    p = setup_temp_file("full_test", content)
    try:
        rel = str(p.resolve().relative_to(REPO))
        d = capture_call(module.cmd_read_raw,
                         type("A", (), {"locator": f"{rel}#全篇"})())
        assert d["ok"] is False
        assert d["status"] == "error"
        assert "不向 LLM 返回全文" in d["error"]
    finally:
        cleanup()


def test_read_raw_authors_is_precise_header_block():
    """#authors 只返回标题后的作者块，不静默预览 Abstract/正文。"""
    content = "# Paper\n\nAlice, Bob\nUniversity\n\n## Abstract\n\nSECRET ABSTRACT\n"
    p = setup_temp_file("authors_test", content)
    try:
        rel = str(p.resolve().relative_to(REPO))
        d = capture_call(module.cmd_read_raw,
                         type("A", (), {"locator": f"{rel}#authors"})())
        assert d["ok"] is True
        assert "Alice, Bob" in d["result"]["text"]
        assert "SECRET ABSTRACT" not in d["result"]["text"]
    finally:
        cleanup()


def test_read_raw_participants_is_precise_line():
    """#participants 无标题时只返回参会人结构行。"""
    content = "会议主题：测试\n参会人员：张三、李四\n讨论：SECRET TIMELINE\n"
    p = setup_temp_file("participants_test", content)
    try:
        rel = str(p.resolve().relative_to(REPO))
        d = capture_call(module.cmd_read_raw,
                         type("A", (), {"locator": f"{rel}#participants"})())
        assert d["ok"] is True
        assert d["result"]["text"] == "参会人员：张三、李四"
        assert "SECRET TIMELINE" not in d["result"]["text"]
    finally:
        cleanup()


def test_read_raw_line_range_exact():
    """Lx-Ly 只返回指定行，不回退全文。"""
    p = setup_temp_file("line_range_test", "alpha\nbeta\ngamma\ndelta\n")
    try:
        rel = str(p.resolve().relative_to(REPO))
        d = capture_call(module.cmd_read_raw,
                         type("A", (), {"locator": f"{rel}#L2-L3"})())
        assert d["ok"] is True
        assert d["result"]["text"] == "beta\ngamma"
    finally:
        cleanup()


def test_read_raw_line_range_out_of_bounds():
    """越界行号明确失败。"""
    p = setup_temp_file("line_range_missing", "one\ntwo\n")
    try:
        rel = str(p.resolve().relative_to(REPO))
        d = capture_call(module.cmd_read_raw,
                         type("A", (), {"locator": f"{rel}#L2-L3"})())
        assert d["ok"] is False
        assert d["result"]["locator_status"] == "missing"
    finally:
        cleanup()


def test_read_raw_oversized_locator_requires_refinement():
    """命中片段过大时拒绝返回半截内容。"""
    content = "## Large\n\n" + ("x" * (module.RAW_PREVIEW_CHARS + 1)) + "\n"
    p = setup_temp_file("oversized_locator", content)
    try:
        rel = str(p.resolve().relative_to(REPO))
        d = capture_call(module.cmd_read_raw,
                         type("A", (), {"locator": f"{rel}#Large"})())
        assert d["ok"] is False
        assert d["status"] == "error"
        assert "请细化 locator" in d["error"]
        assert "x" * 100 not in d["error"]
    finally:
        cleanup()


def test_read_raw_pdf_page_native():
    """有文本层 PDF 使用原始页码 locator，不需要 Markdown companion。"""
    import fitz
    TEMP_TEST_DIR.mkdir(parents=True, exist_ok=True)
    p = TEMP_TEST_DIR / "native_pages.pdf"
    document = fitz.open()
    document.new_page().insert_text((72, 72), "first page")
    document.new_page().insert_text((72, 72), "second page")
    document.save(str(p))
    document.close()
    try:
        rel = str(p.resolve().relative_to(REPO))
        d = capture_call(module.cmd_read_raw,
                         type("A", (), {"locator": f"{rel}#page-2"})())
        assert d["ok"] is True
        assert "second page" in d["result"]["text"]
        assert "first page" not in d["result"]["text"]
    finally:
        cleanup()


def test_read_raw_unresolvable_path():
    """路径不存在 → error。"""
    d = capture_call(module.cmd_read_raw,
                     type("A", (), {"locator": "nonexistent/path/xyz#全篇"})())
    assert d["ok"] is False
    assert d["status"] == "error"


# ============ main() 分发 ============

def test_main_unknown_command_errors():
    """未知子命令 → argparse SystemExit(2)，不静默成功。"""
    try:
        module.main(["nope"])
        assert False, "未知子命令应 SystemExit"
    except SystemExit as e:
        assert e.code == 2, f"期望退出码 2，实际 {e.code}"


def main():
    test_envelope_has_all_fields()
    test_envelope_error_status_implies_ok_false()
    test_envelope_explicit_ok_overrides_status()
    test_collect_sources_from_edges_normal()
    test_collect_sources_empty()
    test_collect_sources_skips_missing_source()
    test_parser_has_all_subcommands()
    test_parser_defaults()
    test_abbr_envelope()
    test_frontier_wrapper_json_envelope()
    test_frontier_answer_wrapper()
    test_query_graph_json_detects_error_dict()
    test_query_graph_json_normal_dict()
    test_read_raw_present_section()
    test_read_raw_missing_section()
    test_read_raw_full_doc()
    test_read_raw_authors_is_precise_header_block()
    test_read_raw_participants_is_precise_line()
    test_read_raw_line_range_exact()
    test_read_raw_line_range_out_of_bounds()
    test_read_raw_oversized_locator_requires_refinement()
    test_read_raw_pdf_page_native()
    test_read_raw_unresolvable_path()
    test_main_unknown_command_errors()
    print("wg regression: PASS")


if __name__ == "__main__":
    main()
