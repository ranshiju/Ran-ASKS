#!/usr/bin/env python3
"""提示词路由与 trace 审计回归测试。"""
import json
import shlex
import subprocess
import sys
import os
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def run(*args):
    return subprocess.run(args, cwd=REPO, capture_output=True, text=True, check=True)


def has_routed_locator(text: str, file: str, section: str) -> bool:
    return any(
        line.startswith(f"--- {file}#md:") and section in line
        for line in text.splitlines()
    )


def audit(records):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", encoding="utf-8", delete=False) as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        path = f.name
    try:
        return json.loads(run("python3", ".scripts/check_trace_rules.py", path).stdout)
    finally:
        Path(path).unlink(missing_ok=True)


def test_profiles():
    expected = {
        "这个是什么": ["fact"],
        "列出所有经历": ["enumeration"],
        "关系如何": ["relation"],
        "为什么以及依据是什么": ["traceability"],
    }
    full = len(run("python3", ".scripts/route.py", "--task", "query", "--full").stdout)
    for query, profiles in expected.items():
        payload = json.loads(run("python3", ".scripts/route.py", "--task", "query", "--query", query, "--format", "json").stdout)
        assert payload["profiles"] == profiles
        assert payload["stage"] == "start"
        assert payload["estimated_chars"] == len(payload["prompt"])
        assert len(payload["prompt"]) < full
        assert not payload["warnings"]
        assert "工程上下文(按元图派发)" in payload["prompt"]

    mixed = json.loads(run(
        "python3", ".scripts/route.py", "--task", "query", "--query", "列出某人与谁的关系和依据", "--format", "json"
    ).stdout)
    assert mixed["profiles"] == ["enumeration", "relation", "traceability"]


def test_query_stage_dispatch():
    start = json.loads(run(
        "python3", ".scripts/route.py", "--task", "query", "--query", "关系如何", "--format", "json"
    ).stdout)
    assert "首轮定位步骤" in start["prompt"]
    assert "证据下钻步骤" not in start["prompt"]
    assert "回环规则" not in start["prompt"]
    assert "交付步骤" not in start["prompt"]
    assert len(start["prompt"]) < 13_000

    evidence = json.loads(run(
        "python3", ".scripts/route.py", "--task", "query", "--query", "关系如何",
        "--query-stage", "evidence", "--format", "json"
    ).stdout)
    assert "证据下钻步骤" in evidence["prompt"]
    assert "首轮定位步骤" not in evidence["prompt"]
    assert "回环规则" not in evidence["prompt"]

    continuation = json.loads(run(
        "python3", ".scripts/route.py", "--task", "query", "--query", "为什么以及依据是什么",
        "--query-stage", "continue", "--format", "json"
    ).stdout)
    assert "回环规则" in continuation["prompt"]
    assert "槽位清单与缺口回检" in continuation["prompt"]
    assert "交付步骤" not in continuation["prompt"]

    answer = json.loads(run(
        "python3", ".scripts/route.py", "--task", "query", "--query", "为什么以及依据是什么",
        "--query-stage", "answer", "--format", "json"
    ).stdout)
    assert "交付步骤" in answer["prompt"]
    assert "三种表述姿态" in answer["prompt"]
    assert "回环规则" not in answer["prompt"]

    for stage_card in (evidence, continuation, answer):
        assert "工程上下文(按元图派发)" not in stage_card["prompt"]
        assert "执行纪律（使用任务）" not in stage_card["prompt"]
        assert "任务执行边界（ingest/query）" not in stage_card["prompt"]
        assert "轻量经验层（事件触发）" not in stage_card["prompt"]


def test_lightweight_session_plan_contract():
    query = json.loads(run(
        "python3", ".scripts/route.py", "--task", "query", "--query", "比较两个方案并说明依据", "--format", "json"
    ).stdout)
    assert "会话级总计划（条件触发）" in query["prompt"]
    assert "轻量检索策略（start 前置）" in query["prompt"]
    assert "不得写脚本、检索词、页名、证据卡、预算细节" in query["prompt"]
    assert "API 中 API 仅处理当前派发阶段的最小受控上下文" in query["prompt"]

    ingest = run(
        "python3", ".scripts/route.py", "--task", "ingest", "--subproject", "academic", "--stage", "1"
    )
    assert "会话级总计划（条件触发）" in ingest.stdout
    assert "不得细化为命令、字段或工具清单" in ingest.stdout


def test_non_agent_backend_notices_every_stage():
    query_env = os.environ.copy()
    query_env["QUERY_BACKEND"] = "api"
    query_env["LLM_MODEL"] = "DeepSeek-V3.2"
    query = subprocess.run(
        ["python3", ".scripts/route.py", "--task", "query", "--query", "继续核验", "--query-stage", "continue"],
        cwd=REPO, capture_output=True, text=True, check=True, env=query_env,
    )
    assert "[query 后端] 阶段 continue：LLM=API（DeepSeek-V3.2）" in query.stderr

    ingest_env = os.environ.copy()
    ingest_env["INGEST_BACKEND"] = "api"
    ingest_env["LLM_MODEL"] = "DeepSeek-V3.2"
    ingest = subprocess.run(
        ["python3", ".scripts/route.py", "--task", "ingest", "--subproject", "academic", "--stage", "2"],
        cwd=REPO, capture_output=True, text=True, check=True, env=ingest_env,
    )
    assert "[ingest 后端] 阶段2巩固：LLM=API（DeepSeek-V3.2）" in ingest.stderr


def test_ingest_dispatch_parameter_contract():
    meeting = run(
        "python3", ".scripts/route.py", "--task", "ingest", "--subproject", "academic",
        "--source-kind", "meeting", "--stage", "1",
    )
    assert "content=other" in meeting.stderr
    assert "会议纪要预处理" in meeting.stdout
    bad = subprocess.run(
        ["python3", ".scripts/route.py", "--task", "ingest", "--subproject", "academic",
         "--source-kind", "meeting", "--content", "paper", "--stage", "1"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert bad.returncode != 0
    assert "不能派发论文模板" in bad.stderr


def test_task_specific_execution_guidance_is_dispatched():
    ingest = run(
        "python3", ".scripts/route.py", "--task", "ingest", "--subproject", "academic", "--stage", "1",
    )
    for requirement in ("全文只在首次 LLM 阅读时读取一次", "ingest_check.py --graph", "每个 create stage 仍须完成、落盘并重新路由后才能推进"):
        assert requirement in ingest.stdout

    query = json.loads(run(
        "python3", ".scripts/route.py", "--task", "query", "--query", "关系如何", "--format", "json",
    ).stdout)
    assert "任务执行边界（ingest/query）" in query["prompt"]
    assert "不读取脚本源码中的提示词模板" in query["prompt"]


def test_state_capability_tool_dispatch_is_explicit():
    listing = run("python3", ".scripts/route.py", "--list").stdout
    assert "可用 task/state（write 为兼容别名）:" in listing
    assert "[compat -> capability write/general]" in listing
    assert "可用 capability:" in listing
    assert "write: profiles=academic,general" in listing

    research = run("python3", ".scripts/route.py", "--task", "research").stdout
    assert "research 是持续状态，write 是按需能力" in research
    assert "--capability write --capability-profile academic" in research

    academic_write = run(
        "python3", ".scripts/route.py", "--capability", "write",
        "--capability-profile", "academic",
    ).stdout
    assert "共享落笔约定（可组合能力）" in academic_write
    assert "正面说明作用与范围" in academic_write
    assert "大写拉丁字母表示总数" in academic_write
    assert "# 物理论文写作讨论注意力清单" in academic_write
    assert "发言稿、公文" not in academic_write

    general_write = run("python3", ".scripts/route.py", "--capability", "write").stdout
    legacy_write = run("python3", ".scripts/route.py", "--task", "write").stdout
    for requirement in ("共享落笔约定（可组合能力）", "起草工作流", "能力边界"):
        assert requirement in general_write
        assert requirement in legacy_write

    mixed = subprocess.run(
        ["python3", ".scripts/route.py", "--task", "research", "--capability", "write"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert mixed.returncode != 0
    assert "请分两次调用" in mixed.stderr


def test_engineering_graph():
    result = run("python3", ".scripts/engineering_graph.py", "validate")
    assert "工程元图有效" in result.stdout
    impact = run("python3", ".scripts/engineering_graph.py", "impact", "graph_ingest")
    assert "operations/INGEST.md" in impact.stdout
    assert "cross-domain/graph.db" in impact.stdout
    verified = run("python3", ".scripts/engineering_graph.py", "impact", "graph_ingest", "--verify")
    assert "test_ingest_pipeline.py" in verified.stdout
    build_impact = run("python3", ".scripts/engineering_graph.py", "impact", "build", "--verify")
    assert "capability=build" in build_impact.stdout
    assert "test_prompt_audit.py" in build_impact.stdout

    frontier_route = run("python3", ".scripts/route.py", "--task", "frontier")
    assert "capability=frontier" in frontier_route.stdout
    assert "Frontier — 研究前沿层规范" in frontier_route.stdout
    assert "不得向事实 `graph.db` 写 Frontier 节点" in frontier_route.stdout
    contract = run("python3", ".scripts/engineering_graph.py", "contract", "graph_ingest")
    assert "cross-domain/graph.db" in contract.stdout
    query_contract = run("python3", ".scripts/engineering_graph.py", "contract", "query_graph").stdout
    assert "相邻 Wiki section 的 Raw 脚注回到 Raw" in query_contract
    query_card = run("python3", ".scripts/engineering_graph.py", "capability", "query", "--compact")
    assert "任务卡（不可跳过）" in query_card.stdout
    assert "下钻 Raw" in query_card.stdout
    for task in ("sync", "write", "scan", "inbox"):
        routed = run("python3", ".scripts/route.py", "--task", task)
        assert "工程上下文(按元图派发)" in routed.stdout


def test_meeting_workflow_documentation():
    # handbook 精简后不再承载会议流程细节；会议工作流由 INGEST.md + code-guidance 承担
    ingest = (REPO / "operations/INGEST.md").read_text(encoding="utf-8")
    guidance = (REPO / "operations/engineering/code-guidance.md").read_text(encoding="utf-8")
    for document in (ingest, guidance):
        assert "entity-resolution.json" in document
        assert "sources" in document and "raw" in document
    assert "步骤 1 起读 `corrected.md`" not in ingest


def test_inbox_single_read_documentation():
    inbox = (REPO / "operations/INBOX.md").read_text(encoding="utf-8")
    graph = (REPO / "operations/engineering/graph.yaml").read_text(encoding="utf-8")
    # 单次阅读 + 前置决策：避免重复全文 LLM 读取，sources 直接填最终 raw 路径
    for document in (inbox, graph):
        assert "全文只读一次" in document
        assert "前置决策" in document
    assert "不得写入正式 `sources`" in inbox
    assert "不得建立图边" in inbox
    assert "inbox_finalize" in inbox  # 落位步骤固化成脚本（机械归程序）
    assert "manifest.json" in inbox
    assert "默认拒绝覆盖" in inbox
    assert "inbox_finalize_test" in graph


def test_long_document_documentation():
    ingest = (REPO / "operations/INGEST.md").read_text(encoding="utf-8")
    guidance = (REPO / "operations/engineering/code-guidance.md").read_text(encoding="utf-8")
    graph = (REPO / "operations/engineering/graph.yaml").read_text(encoding="utf-8")
    for document in (ingest, guidance, graph):
        assert "long_document_plan" in document
    assert "密度、词频只用于产生候选" in ingest
    assert "不因字数直接拆页" in ingest


def test_ingest_route_guardrails():
    missing_domain = subprocess.run(
        ("python3", ".scripts/route.py", "--task", "ingest", "--stage", "1"),
        cwd=REPO, capture_output=True, text=True,
    )
    assert missing_domain.returncode != 0
    assert "必须指定 --subproject" in missing_domain.stderr
    missing_stage = subprocess.run(
        ("python3", ".scripts/route.py", "--task", "ingest", "--subproject", "academic"),
        cwd=REPO, capture_output=True, text=True,
    )
    assert missing_stage.returncode != 0
    assert "必须指定 --stage" in missing_stage.stderr
    api_routed = run(
        "python3", ".scripts/route.py", "--task", "ingest", "--subproject", "academic",
        "--content", "paper", "--stage", "1",
    )
    # api 后端路由断言：显式设 INGEST_BACKEND=api 隔离 .env 默认值影响
    api_env = dict(os.environ)
    api_env["INGEST_BACKEND"] = "api"
    api_routed = subprocess.run(
        ("python3", ".scripts/route.py", "--task", "ingest", "--subproject", "academic",
         "--content", "paper", "--stage", "1"),
        cwd=REPO, capture_output=True, text=True, env=api_env,
    )
    # API 后端路由断言：ingest_paper.py --raw 代码驱动流水线（已替代旧 api_ingest.py 证据卡路径）
    assert "ingest_paper.py --raw" in api_routed.stderr
    assert "INGEST_BACKEND=api" in api_routed.stderr
    assert "代码驱动" in api_routed.stderr
    batch_routed = run(
        "python3", ".scripts/route.py", "--task", "ingest", "--subproject", "academic",
        "--mode", "batch", "--content", "paper",
    )
    assert "inbox_ingest.py complete-batch" in batch_routed.stderr
    long_document_routed = run(
        "python3", ".scripts/route.py", "--task", "ingest", "--subproject", "academic",
        "--content", "other", "--stage", "1",
    )
    assert "长文动态颗粒度" in long_document_routed.stdout


def test_ingest_minimal_domain_dispatch():
    ordinary_stage_one = run(
        "python3", ".scripts/route.py", "--task", "ingest", "--subproject", "academic",
        "--mode", "create", "--content", "paper", "--stage", "1",
    ).stdout
    assert not has_routed_locator(ordinary_stage_one, "operations/INGEST.md", "会议纪要预处理")
    assert "entity-resolution.json" not in ordinary_stage_one
    assert "研究方向与关键词提取" not in ordinary_stage_one
    assert len(ordinary_stage_one) < 20_000

    meeting_stage_one = run(
        "python3", ".scripts/route.py", "--task", "ingest", "--subproject", "academic",
        "--mode", "create", "--content", "other", "--stage", "1", "--source-kind", "meeting",
    ).stdout
    assert has_routed_locator(meeting_stage_one, "operations/INGEST.md", "会议纪要预处理")
    assert "entity-resolution.json" in meeting_stage_one

    admin_update = run(
        "python3", ".scripts/route.py", "--task", "ingest", "--subproject", "admin",
        "--mode", "update", "--content", "other",
    ).stdout
    assert not has_routed_locator(admin_update, "operations/INGEST.md", "学术图边关系")
    assert not has_routed_locator(admin_update, "operations/INGEST.md", "会议纪要预处理")
    assert len(admin_update) < 22_000

    academic_stage_two = run(
        "python3", ".scripts/route.py", "--task", "ingest", "--subproject", "academic",
        "--mode", "create", "--content", "paper", "--stage", "2",
    ).stdout
    assert has_routed_locator(academic_stage_two, "operations/INGEST.md", "通用图边约束")
    assert has_routed_locator(academic_stage_two, "operations/INGEST.md", "学术图边关系")
    assert not has_routed_locator(academic_stage_two, "operations/INGEST.md", "会议图边关系")


def test_use_task_execution_discipline():
    discipline = "执行纪律（使用任务）"
    supplement = "摄入执行补充"
    experience_notice = "轻量经验层（事件触发）"
    query_payload = json.loads(run(
        "python3", ".scripts/route.py", "--task", "query", "--query", "这个是什么", "--format", "json"
    ).stdout)
    assert discipline in query_payload["prompt"]
    assert supplement not in query_payload["prompt"]
    assert experience_notice in query_payload["prompt"]

    ingest = run(
        "python3", ".scripts/route.py", "--task", "ingest", "--subproject", "admin",
        "--mode", "create", "--content", "other", "--stage", "1",
    ).stdout
    assert discipline in ingest
    assert supplement in ingest
    assert experience_notice in ingest

    admin_update = run(
        "python3", ".scripts/route.py", "--task", "ingest", "--subproject", "admin",
        "--mode", "update", "--content", "other",
    ).stdout
    assert has_routed_locator(admin_update, "admin/SCHEMA.md", "graphdb-边写作约束")

    for task in ("lint", "sync", "write", "scan", "inbox", "hub"):
        routed = run("python3", ".scripts/route.py", "--task", task).stdout
        assert discipline in routed
        assert supplement not in routed
        if task == "write":
            assert experience_notice in routed
        else:
            assert experience_notice not in routed

    build = run("python3", ".scripts/route.py", "--task", "build").stdout
    assert discipline not in build
    assert supplement not in build
    assert experience_notice in build
    assert "建设任务工程精确读取门" in build
    assert "推荐精确 locator" in build
    assert "list --prefix" in build
    assert "只把命中片段交给 Agent" in build
    assert "功能性任务不调用工程 locator" in build
    assert has_routed_locator(build, "operations/shared-conventions.md", "下游同步清单")
    assert has_routed_locator(build, "operations/shared-conventions.md", "建设交付的工程文档维护")
    assert "## 会议纪要文件命名规范" not in build

    impact = run(
        "python3", ".scripts/engineering_graph.py", "impact", "route", "--verify"
    ).stdout
    assert "推荐精确 locator（先直接 read）" in impact
    assert "graph.yaml#yaml:/nodes/route" in impact
    assert "graph.yaml#yaml:/script_contracts/route" in impact
    assert "过滤发现入口（推荐 locator 不足时）" in impact
    assert "list .scripts/route.py --prefix py:" in impact
    assert "list operations/engineering/graph.yaml" not in impact
    recommended_block = impact.split("推荐精确 locator（先直接 read）:", 1)[1].split(
        "过滤发现入口（推荐 locator 不足时）:", 1
    )[0]
    recommended_commands = [
        line.split(": ", 1)[1]
        for line in recommended_block.splitlines()
        if line.startswith("- ") and ": python3 " in line
    ]
    assert recommended_commands
    for command in recommended_commands:
        assert json.loads(run(*shlex.split(command)).stdout)["ok"]

    build_impact = run(
        "python3", ".scripts/engineering_graph.py", "impact", "build"
    ).stdout
    assert "graph.yaml#yaml:/capabilities/build" in build_impact


def test_arxiv_direction_config_compatibility():
    sys.path.insert(0, str(REPO / ".scripts"))
    import graph_ingest
    graph_ingest._ARXIV_DIRECTIONS = None
    directions = graph_ingest.load_arxiv_directions()
    assert "量子信息" in directions
    assert "机器学习" in directions


def test_trace_rules():
    valid = audit([
        {"action": "graph_search", "query_type": "fact", "output_summary": '{"count": 1}'},
        {"action": "read_section", "query_type": "fact", "input": {"page": "academic/wiki/authors/cnu-ran-shiju.md", "section": "Navigation"}},
        {"action": "answer", "query_type": "fact", "decision": "回答"},
    ])
    assert not any("R10" in item for item in valid["violations"])

    invalid = audit([
        {"action": "read_keyword_index", "query_type": "fact", "output_summary": "命中"},
        {"action": "answer", "query_type": "fact", "decision": "回答"},
    ])
    assert any("R3" in item for item in invalid["violations"])

    enum_invalid = audit([
        {"action": "graph_search", "query_type": "enumeration", "output_summary": '{"count": 1}'},
        {"action": "read_section", "query_type": "enumeration", "input": {"page": "academic/wiki/authors/cnu-ran-shiju.md", "section": "Navigation"}},
        {"action": "answer", "query_type": "enumeration", "decision": "回答"},
    ])
    assert any("R11" in item for item in enum_invalid["violations"])


if __name__ == "__main__":
    test_profiles()
    test_query_stage_dispatch()
    test_lightweight_session_plan_contract()
    test_non_agent_backend_notices_every_stage()
    test_ingest_dispatch_parameter_contract()
    test_task_specific_execution_guidance_is_dispatched()
    test_state_capability_tool_dispatch_is_explicit()
    test_query_stage_dispatch()
    test_engineering_graph()
    test_meeting_workflow_documentation()
    test_inbox_single_read_documentation()
    test_long_document_documentation()
    test_ingest_route_guardrails()
    test_ingest_minimal_domain_dispatch()
    test_use_task_execution_discipline()
    test_trace_rules()
    print("prompt audit regression: PASS")
