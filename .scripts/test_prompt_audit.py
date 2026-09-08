#!/usr/bin/env python3
"""提示词路由与 trace 审计回归测试。"""
import ast
import importlib.util
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


def test_focused_prompt_review_task():
    payload = json.loads(run(
        "python3", ".scripts/lint_specs.py", "--format", "json"
    ).stdout)
    assert payload["schema"] == "prompt-audit-task-v4"
    assert payload["status"] == "prepared"
    assert payload["review_profile"] == "full-semantic-review"
    assert payload["semantic_review"]["status"] == "required"
    structural = {item["id"]: item for item in payload["structural_checks"]}
    assert all(structural[check_id]["status"] == "passed" for check_id in (
        "repository_path_scope", "governor_references_resolved",
        "governor_graph_acyclic", "location_schema_and_mode",
    ))
    assert structural["llm_visible_surface_only"]["status"] == "unknown"
    assert [item["id"] for item in payload["criteria"]] == [
        "placement", "necessity", "consistency", "currency", "conciseness", "ambiguity", "negation",
    ]
    currency = next(item for item in payload["criteria"] if item["id"] == "currency")
    assert "当前上位 capability/task/script contract" in currency["question"]
    assert "实际 CLI、Schema/validator 与实现" in currency["question"]
    assert "已退役约定" in currency["question"]
    assert "governed_by" in payload["version_basis"]["governing_sources"]
    assert "组件责任边界" in payload["version_basis"]["implementation_evidence"]
    assert "不证明提示语义符合当前版本" in payload["version_basis"]["freshness_limit"]
    assert any("不得只凭文件日期或哈希" in rule for rule in payload["review_rules"])
    conciseness = next(item for item in payload["criteria"] if item["id"] == "conciseness")
    assert "目标 Agent 已稳定具备" in conciseness["question"]
    assert "项目判断与执行" in conciseness["question"]

    locations = payload["locations"]
    assert payload["summary"]["locations"] == len(locations)
    assert payload["summary"]["files"] == len({item["path"] for item in locations})
    assert payload["summary"]["relations"] == len(payload["relations"])
    assert len({item["id"] for item in locations}) == len(locations)
    assert all(not Path(item["path"]).name.startswith("test_") for item in locations)
    assert all(item["llm_visible"] for item in locations)
    assert all(not ({"raw", "wiki", "outputs", "state"} & set(Path(item["path"]).parts))
               for item in locations)
    assert locations[0]["path"] == "AGENTS.md"
    assert locations[1]["path"] == "operations/shared-conventions.md"
    assert payload["review_rules"][0].startswith("沿 governed_by 关系按总到分审查")

    position = {item["id"]: index for index, item in enumerate(locations)}
    assert locations[0]["governed_by"] == []
    assert all(item["governed_by"] for item in locations[1:])
    for item in locations:
        for parent in item["governed_by"]:
            assert parent in position
            assert position[parent] < position[item["id"]]

    assert any(item["path"] == "AGENTS.md" and item["layer"] == "global-instruction"
               for item in locations)
    assert any(item["path"] == "projects/ASKS/AGENTS.md"
               and item["layer"] == "scoped-instruction" for item in locations)
    assert any(item["id"] == "graph:capability:build" for item in locations)
    assert any(item["id"] == "graph:contract:route"
               and item["artifact_type"] == "contract" for item in locations)
    assert any(item["path"] == "academic/SCHEMA.md"
               and item["artifact_type"] == "contract" for item in locations)
    assert any(item["path"] == "agents/writer/AGENT.md" for item in locations)
    assert any(item["path"] == "agents/writer/checklist.md"
               and item["governed_by"] == ["doc:agents/writer/AGENT.md"]
               for item in locations)
    assert any(item["path"] == "operations/RECOVERY.md"
               and item["artifact_type"] == "prompt-resource"
               and item["governed_by"] == ["doc:operations/QUERY.md"]
               for item in locations)
    assert any(item["path"] == "operations/templates/paper-summary.md"
               and item["artifact_type"] == "prompt-resource"
               and item["governed_by"] == [
                   "doc:operations/INGEST.md", "doc:academic/SCHEMA.md",
               ]
               for item in locations)
    assert any(item["path"] == "operations/templates/admin-templates.md"
               and item["governed_by"] == [
                   "doc:operations/INGEST.md", "doc:admin/SCHEMA.md",
               ]
               for item in locations)
    assert any(item["path"] == "operations/engineering/code-guidance.md"
               and item["layer"] == "build-instruction"
               and item["governed_by"] == ["graph:capability:build"]
               for item in locations)
    assert any(item["path"] == "operations/engineering/engineering-handbook.md"
               and item["layer"] == "build-instruction"
               and item["governed_by"] == ["graph:capability:build"]
               for item in locations)
    assert all(item["path"] != "operations/DISCUSSION.md" for item in locations)
    assert any(item["path"] == "memory/MEMORY.md"
               and item["artifact_type"] == "memory" for item in locations)
    memory = (REPO / "memory/MEMORY.md").read_text(encoding="utf-8")
    assert "# 用户级长期记忆" in memory
    legacy_home = Path("/", "Users", "apple").as_posix() + "/"
    assert legacy_home not in memory
    assert '当用户发出"汇总"指令时' not in memory
    academic_schema = (REPO / "academic/SCHEMA.md").read_text(encoding="utf-8")
    assert academic_schema.count("## Raw 目录与论文包") == 1
    assert "MinerU > Docling > PyMuPDF" not in academic_schema
    paper_template = (REPO / "operations/templates/paper-summary.md").read_text(encoding="utf-8")
    admin_templates = (REPO / "operations/templates/admin-templates.md").read_text(encoding="utf-8")
    assert "只提供论文页正文的可选组织方式" in paper_template
    assert "只帮助选择正文组织方式" in admin_templates
    writer_agent = (REPO / "agents/writer/AGENT.md").read_text(encoding="utf-8")
    assert "独立 Writer Agent 实例" in writer_agent
    assert "academic write profile" in writer_agent
    assert any(item["path"] == ".scripts/ingest_paper.py"
               and item.get("symbol") == "build_wiki_prompt"
               and item["governed_by"] == ["graph:contract:ingest_paper"]
               for item in locations)
    answer_judge_prompts = [
        item for item in locations
        if item["path"] == ".scripts/answer_judge.py"
    ]
    assert len(answer_judge_prompts) == 2
    assert all(item["governed_by"] == ["graph:contract:answer_judge"]
               for item in answer_judge_prompts)
    ingest_common_prompts = [
        item for item in locations
        if item["path"] == ".scripts/ingest_common.py"
        and item["artifact_type"] == "runtime-prompt"
    ]
    assert len(ingest_common_prompts) == 2
    assert all(item["governed_by"] == ["graph:contract:ingest_common"]
               for item in ingest_common_prompts)
    assert any(item["path"] == ".scripts/ingest_paper.py"
               and item.get("symbol") == "build_paper_semantic_contract" for item in locations)
    assert any(item["path"] == "dsh/meeting_compiler_agent.py"
               and item.get("symbol", "").endswith(":system") for item in locations)
    assert any(item["path"] == "dsh/agent_loop.py"
               and item.get("symbol") == "_build_prompt" and item["mode"] == "api"
               for item in locations)
    assert any(item["path"] == "projects/ASKS/experiments/e2b-agent-navigation-audit/run.py"
               and item.get("symbol") == "SYSTEM_PROMPT"
               and item["governed_by"] == ["doc:projects/ASKS/AGENTS.md"]
               for item in locations)
    assert any(item["path"] == "projects/asks-ai-agent/notes/02-knowledge-drift-storyboard.yaml"
               and item["selector"] == "yaml:/assets/0/prompt"
               and item["artifact_type"] == "runtime-prompt"
               for item in locations)

    focused = run(
        "python3", ".scripts/lint_specs.py", "--paths",
        "AGENTS.md", ".scripts/ingest_paper.py",
    ).stdout
    for title in (
        "出现位置", "必要性", "矛盾或重复", "版本一致性",
        "表述冗余", "表述歧义", "否定式必要性",
    ):
        assert title in focused
    assert "## 当前版本依据" in focused
    assert "上位规范：当次位置地图中的 governed_by" in focused
    assert "实现证据：当前 CLI 参数、Schema/validator、组件责任边界" in focused
    assert "新鲜度边界：文件签名、mtime、缓存命中与 SHA-256" in focused
    assert "AGENTS.md#document" in focused
    assert ".scripts/ingest_paper.py#L" in focused
    assert "operations/INGEST.md" not in focused


def test_trusted_agent_build_profile_skips_semantic_review():
    command = (
        "python3", ".scripts/lint_specs.py",
        "--task", "build", "--backend", "agent",
        "--paths", "AGENTS.md", ".scripts/route.py",
    )
    payload = json.loads(run(*command, "--format", "json").stdout)
    assert payload["schema"] == "prompt-audit-task-v4"
    assert payload["status"] == "structural_only"
    assert payload["execution_context"] == {"task": "build", "backend": "agent"}
    assert payload["review_profile"] == "trusted-agent-build"
    assert payload["semantic_review"]["status"] == "skipped"
    assert payload["criteria"] == []
    assert payload["review_rules"] == []
    assert payload["locations"]
    structural = {item["id"]: item for item in payload["structural_checks"]}
    assert all(structural[check_id]["status"] == "passed" for check_id in (
        "repository_path_scope", "governor_references_resolved",
        "governor_graph_acyclic", "location_schema_and_mode",
    ))
    assert structural["llm_visible_surface_only"]["status"] == "unknown"

    rendered = run(*command).stdout
    assert rendered.startswith("# 项目提示词结构检查")
    assert "审查配置：trusted-agent-build" in rendered
    assert "## 确定性检查" in rendered
    assert "## 审查标准" not in rendered
    assert "表述冗余" not in rendered
    assert "最小修改建议" not in rendered

    api_payload = json.loads(run(
        "python3", ".scripts/lint_specs.py",
        "--task", "build", "--backend", "api",
        "--paths", "AGENTS.md", ".scripts/route.py",
        "--format", "json",
    ).stdout)
    assert api_payload["status"] == "prepared"
    assert api_payload["review_profile"] == "full-semantic-review"
    assert api_payload["semantic_review"]["status"] == "required"
    assert [item["id"] for item in api_payload["criteria"]] == [
        "placement", "necessity", "consistency", "currency",
        "conciseness", "ambiguity", "negation",
    ]

    incomplete = subprocess.run(
        ("python3", ".scripts/lint_specs.py", "--task", "build"),
        cwd=REPO, capture_output=True, text=True,
    )
    assert incomplete.returncode == 2
    assert "--task 与 --backend 必须同时提供" in incomplete.stderr


def test_runtime_prompt_surface_boundaries():
    payload = json.loads(run(
        "python3", ".scripts/lint_specs.py", "--no-cache", "--format", "json",
        "--paths", "dsh/tools.py", ".scripts/query_orchestrate.py",
        ".scripts/ingest_paper.py",
    ).stdout)
    locations = payload["locations"]

    tool_definitions = [
        item for item in locations
        if item["path"] == "dsh/tools.py"
        and item.get("symbol", "").startswith("ToolDefinition:")
    ]
    assert len(tool_definitions) == 14
    assert all(item["mode"] == "api" for item in tool_definitions)
    assert all(item["governed_by"] == ["graph:contract:dsh_tools"]
               for item in tool_definitions)

    by_symbol = {
        item.get("symbol"): item
        for item in locations
        if item["path"] in {
            ".scripts/query_orchestrate.py", ".scripts/ingest_paper.py",
        }
    }
    assert by_symbol["_build_api_prompt"]["mode"] == "api"
    assert by_symbol["build_agent_wiki_slots_prompt"]["mode"] == "agent"
    assert by_symbol["build_api_paper_workspace_prompt"]["mode"] == "api"
    assert by_symbol["build_paper_semantic_contract"]["mode"] == "shared"

    spec = importlib.util.spec_from_file_location(
        "prompt_audit_under_test", REPO / ".scripts/lint_specs.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    checks = {item["id"]: item for item in module._structural_checks([{
        "id": "invalid",
        "path": "../outside.md",
        "layer": "runtime-prompt",
        "mode": "wrong",
        "selector": "L1-L1",
        "artifact_type": "runtime-prompt",
        "llm_visible": True,
        "governed_by": ["missing"],
    }])}
    assert checks["repository_path_scope"]["status"] == "failed"
    assert checks["governor_references_resolved"]["status"] == "failed"
    assert checks["location_schema_and_mode"]["status"] == "failed"
    assert checks["llm_visible_surface_only"]["status"] == "unknown"


def test_incremental_prompt_map_cache():
    with tempfile.TemporaryDirectory() as directory:
        cache_path = Path(directory) / "map-cache.json"
        command = (
            "python3", ".scripts/lint_specs.py", "--format", "json",
            "--cache-path", str(cache_path), "--paths",
            "AGENTS.md", ".scripts/ingest_paper.py",
        )

        first = json.loads(run(*command).stdout)
        first_cache = first["summary"]["cache"]
        assert first_cache["hits"] == 0
        assert first_cache["misses"] == 1
        assert first_cache["parsed"] == 1
        assert first_cache["entries"] == 1
        assert first_cache["written"] is True

        second = json.loads(run(*command).stdout)
        second_cache = second["summary"]["cache"]
        assert second_cache["hits"] == 1
        assert second_cache["misses"] == 0
        assert second_cache["parsed"] == 0
        assert second_cache["written"] is False
        assert second["locations"] == first["locations"]

        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        cache["files"][".scripts/ingest_paper.py"]["signature"]["mtime_ns"] -= 1
        cache_path.write_text(json.dumps(cache), encoding="utf-8")
        metadata_changed = json.loads(run(*command).stdout)
        metadata_cache = metadata_changed["summary"]["cache"]
        assert metadata_cache["misses"] == 1
        assert metadata_cache["hash_hits"] == 1
        assert metadata_cache["parsed"] == 0
        assert metadata_changed["locations"] == first["locations"]

        refreshed = json.loads(run(*command, "--refresh-cache").stdout)
        refresh_cache = refreshed["summary"]["cache"]
        assert refresh_cache["misses"] == 1
        assert refresh_cache["parsed"] == 1
        assert refreshed["locations"] == first["locations"]

        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        cache["files"]["projects/deleted-prompt-fixture.py"] = {
            "kind": "python",
            "signature": {"size": 1, "mtime_ns": 1, "ctime_ns": 1},
            "sha256": "missing",
            "locations": [],
        }
        cache_path.write_text(json.dumps(cache), encoding="utf-8")
        deleted = json.loads(run(
            "python3", ".scripts/lint_specs.py", "--format", "json",
            "--cache-path", str(cache_path), "--paths",
            "AGENTS.md", "projects/deleted-prompt-fixture.py",
        ).stdout)
        assert deleted["summary"]["cache"]["removed"] == 1
        remaining = json.loads(cache_path.read_text(encoding="utf-8"))["files"]
        assert "projects/deleted-prompt-fixture.py" not in remaining

        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        cache["scanner_sha256"] = "stale-scanner"
        cache_path.write_text(json.dumps(cache), encoding="utf-8")
        invalidated = json.loads(run(*command).stdout)
        invalidated_cache = invalidated["summary"]["cache"]
        assert invalidated_cache["invalidated"] is True
        assert invalidated_cache["hits"] == 0
        assert invalidated_cache["parsed"] == 1
        assert invalidated_cache["entries"] == 1


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
    assert "API 后端由程序维护计划" in query["prompt"]
    assert "API LLM 只处理当前派发阶段的最小受控上下文" in query["prompt"]

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


def test_engineering_graph_target_resolution():
    scripts_path = str(REPO / ".scripts")
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)
    import engineering_graph

    nodes = {
        "solo_node": {"path": "tools/solo.py", "role": "solo"},
        "worker_a": {"path": "a/worker.py", "role": "worker-a"},
        "worker_b": {"path": "b/worker.py", "role": "worker-b"},
        "build_node": {"path": "tools/build.py", "role": "build-node"},
    }
    capabilities = {"build": {"required": []}}
    assert engineering_graph.resolve_target(
        nodes, capabilities, "tools/solo.py") == ("node", "solo_node")
    assert engineering_graph.resolve_target(
        nodes, capabilities, "solo.py") == ("node", "solo_node")
    assert engineering_graph.resolve_target(
        nodes, capabilities, "solo") == ("node", "solo_node")
    assert engineering_graph.resolve_target(
        nodes, capabilities, "build", allow_capability=True) == ("capability", "build")

    for ambiguous in ("worker.py", "worker"):
        try:
            engineering_graph.resolve_target(nodes, capabilities, ambiguous)
        except engineering_graph.TargetResolutionError as exc:
            assert "不唯一" in str(exc)
            assert "worker_a" in str(exc) and "worker_b" in str(exc)
        else:
            raise AssertionError(f"ambiguous target unexpectedly resolved: {ambiguous}")

    try:
        engineering_graph.resolve_target(nodes, capabilities, "/tmp/solo.py")
    except engineering_graph.TargetResolutionError as exc:
        assert "未知工程目标" in str(exc)
    else:
        raise AssertionError("unregistered path unexpectedly resolved by basename")

    path_impact = run(
        "python3", ".scripts/engineering_graph.py", "impact",
        ".scripts/graph_ingest.py", "--verify")
    assert "[建设影响面] graph_ingest:" in path_impact.stdout
    filename_status = run(
        "python3", ".scripts/engineering_graph.py", "status", "graph_ingest.py")
    assert filename_status.stdout.startswith("graph_ingest: .scripts/graph_ingest.py")
    absolute_contract = run(
        "python3", ".scripts/engineering_graph.py", "contract",
        str(REPO / ".scripts/graph_ingest.py"))
    assert "[脚本契约] graph_ingest:" in absolute_contract.stdout

    unknown = subprocess.run(
        ["python3", ".scripts/engineering_graph.py", "impact", "/tmp/graph_ingest.py"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert unknown.returncode == 2
    assert "未知工程目标" in unknown.stderr


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
    assert "谓词由 LLM 选" not in academic_stage_two


def test_audited_prompt_currency_regressions():
    ingest = (REPO / "operations/INGEST.md").read_text(encoding="utf-8")
    inbox = (REPO / "operations/INBOX.md").read_text(encoding="utf-8")
    query = (REPO / "operations/QUERY.md").read_text(encoding="utf-8")
    query_graph = (REPO / ".scripts/query_graph.py").read_text(encoding="utf-8")

    assert "谓词由 LLM 选" not in ingest
    assert "LLM 不选择方向谓词" in ingest
    assert "Core Triples" not in inbox
    assert "`wiki_updates`" in inbox and "`relations`" in inbox
    assert "`--predicate` 在 SQLite 中使用精确相等" in query
    assert "predicate LIKE" not in query
    assert "relations --predicate 合作者" not in query
    assert "人 → 作者 → 论文/专利 ← 作者 ← 人" in query
    assert "graph_build.py" not in query_graph


def test_use_task_execution_discipline():
    discipline = "执行纪律（使用任务）"
    supplement = "摄入执行补充"
    experience_notice = "轻量经验层（事件触发）"
    backend_gate = "模型后端兼容性门"
    query_payload = json.loads(run(
        "python3", ".scripts/route.py", "--task", "query", "--query", "这个是什么", "--format", "json"
    ).stdout)
    assert discipline in query_payload["prompt"]
    assert supplement not in query_payload["prompt"]
    assert experience_notice in query_payload["prompt"]
    assert backend_gate not in query_payload["prompt"]

    ingest = run(
        "python3", ".scripts/route.py", "--task", "ingest", "--subproject", "admin",
        "--mode", "create", "--content", "other", "--stage", "1",
    ).stdout
    assert discipline in ingest
    assert supplement in ingest
    assert experience_notice in ingest
    assert backend_gate not in ingest

    admin_update = run(
        "python3", ".scripts/route.py", "--task", "ingest", "--subproject", "admin",
        "--mode", "update", "--content", "other",
    ).stdout
    assert has_routed_locator(admin_update, "admin/SCHEMA.md", "graphdb-边写作约束")

    for task in ("lint", "sync", "write", "scan", "inbox", "hub"):
        routed = run("python3", ".scripts/route.py", "--task", task).stdout
        assert discipline in routed
        assert supplement not in routed
        assert backend_gate not in routed
        if task == "write":
            assert experience_notice in routed
        else:
            assert experience_notice not in routed

    build = run("python3", ".scripts/route.py", "--task", "build").stdout
    assert discipline not in build
    assert supplement not in build
    assert experience_notice not in build
    assert backend_gate not in build
    assert "[WikiGraph build 方法]" in build
    agents = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    assert "对用户目标负责，保持独立判断" in agents
    assert "给出真实、专业的建议" in agents
    assert "对用户目标负责，保持独立判断" not in build
    assert "Raw 为事实层" in build
    assert "Wiki 为面向理解且桥接 Raw 的语义层" in build
    assert "graph.db 为发现与到达所需的关键导航边" in build
    assert "Agent 模式由当前宿主 Agent 持有控制循环" in build
    assert "API 模式由程序持有控制循环" in build
    assert "DSH、模型调用、重试和预算只存在于 API adapter" in build
    assert "共享输入解析与定位、schema、validator、IR/Graph、报告与事务等确定性内核" in build
    route_tree = ast.parse((REPO / ".scripts/route.py").read_text(encoding="utf-8"))
    top_level_imports = {
        alias.name
        for node in route_tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in route_tree.body
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "llm_structured" not in top_level_imports
    assert "提示词撰写原则" in build
    assert "否定表述仅用于红线和必须明确的边界" in build
    assert "在首次编辑前明确说明" in build
    assert "症状 → 最早错误状态 → 产生机制 → 责任组件" in build
    assert "回归测试覆盖该责任边界" in build
    assert "适用范围和退出条件" in build
    assert "验证不会掩盖其他错误" in build
    assert "impact <target> --verify" in build
    assert "shared、agent-only 或 api-only" in build
    assert "建设任务工程精确读取门" not in build
    assert "operations/shared-conventions.md#" not in build
    assert "## 下游同步清单" not in build
    assert len(build) < 1800, len(build)

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
    test_focused_prompt_review_task()
    test_trusted_agent_build_profile_skips_semantic_review()
    test_runtime_prompt_surface_boundaries()
    test_incremental_prompt_map_cache()
    test_profiles()
    test_query_stage_dispatch()
    test_lightweight_session_plan_contract()
    test_non_agent_backend_notices_every_stage()
    test_ingest_dispatch_parameter_contract()
    test_task_specific_execution_guidance_is_dispatched()
    test_state_capability_tool_dispatch_is_explicit()
    test_query_stage_dispatch()
    test_engineering_graph()
    test_engineering_graph_target_resolution()
    test_meeting_workflow_documentation()
    test_inbox_single_read_documentation()
    test_long_document_documentation()
    test_ingest_route_guardrails()
    test_ingest_minimal_domain_dispatch()
    test_audited_prompt_currency_regressions()
    test_use_task_execution_discipline()
    test_trace_rules()
    print("prompt audit regression: PASS")
