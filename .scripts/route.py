#!/usr/bin/env python3
"""route.py — 按指令类型+任务分发 prompt(壳核分离第二步)

LLM 判断指令类型后,调本脚本截取对应规范 section,拼成 prompt。
路由判断是语义任务(LLM 做);规范截取是机械任务(脚本做,原则4)。

用法:
  route.py --task ingest [--subproject admin|teaching|business] [--mode create|update|batch] [--content paper|other] [--source-kind ordinary|meeting] [--stage 1|2|3]
  route.py --task query
  route.py --task query --query "列出我的出国经历" [--query-stage start|evidence|continue|answer] [--profile auto]
  route.py --task query --full   # 兼容审阅：输出完整 QUERY 规范
  route.py --task lint
  route.py --task sync
  route.py --task write    # 兼容入口，等价于 general write capability
  route.py --task scan
  route.py --task inbox  # 先运行 inbox_plan.py；按 manifest 分流，禁止直接猜 batch
  route.py --task hub
  route.py --task build    # 建设:输出 shared-conventions
  route.py --task research # 研究:输出 RESEARCH 规范
  route.py --task frontier # 研究前沿:输出 FRONTIER 规范
  route.py --capability write --capability-profile academic # research 状态内按需加载论文落笔能力
  route.py --list           # 分类列出 task/state 与 capability

ingest 参数说明:
  --mode   create(默认)/update/batch
  --content paper(默认,仅旧流程非inbox来源)/other(非论文,不拉研究方向段)
  --source-kind ordinary(默认)/meeting；meeting 才派发会议预处理与会议建边规则
  --stage   1=编码/2=巩固/3=收尾(create 模式 3 stage 步进派发;update/batch 不切 stage)
  mode/content/stage 均由 LLM 语义判断后传入,脚本不读 log 推断。

query 参数说明:
  --query-stage start(默认)/evidence/continue/answer；每阶段完成后按当前缺口调用下一阶段
  --profile auto(默认) 会输出可组合意图；仅兼容调用时手动指定单一 profile
"""
import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".scripts"))
from llm_structured import configured_model, execution_mode, ingest_backend_notice
import engineering_locator as engineering_loc
from engineering_graph import capability as engineering_capability
from engineering_graph import load as load_engineering_graph
from engineering_graph import validate as validate_engineering_graph

QUERY_PROFILE_SECTIONS = {
    "fact": [],
    "enumeration": [],
    "relation": ["混合查询规则"],
    "traceability": ["第四层"],
}

QUERY_STAGE_SECTIONS = {
    "start": ["会话级总计划", "轻量检索策略", "触发方式", "全局规则", "section 读取", "第零层", "第一层", "第二层", "首轮定位步骤"],
    "evidence": ["最小证据与披露", "Evidence Profile", "证据下钻步骤"],
    "continue": ["回环规则", "回环计数", "槽位清单与缺口回检", "stop_reason", "续查与停止步骤"],
    "answer": ["三种表述姿态", "高层摘要定位约束", "交付步骤"],
}

QUERY_PROFILE_HINTS = {
    "enumeration": ("列出", "有哪些", "全部", "所有", "经历"),
    "relation": ("关系", "区别", "差异", "比较", "对比", "相关", "影响"),
    "traceability": ("为什么", "依据", "怎么决定", "过程", "冲突", "版本", "演进"),
}

USE_TASK_EXECUTION_DISCIPLINE = """--- 执行纪律（使用任务） ---
- 以本次派发的任务卡和规范为当前步骤边界；先执行其中明确的下一步，完成后再推进。
- 不为理解实现而读取脚本源码，不预读完整规范；只有派发内容要求、参数无法判定或命令报错时，才做最小定向补读。
- 不做探索性 `--help`、全库扫描、全量测试或无关检查；验证只运行任务卡指定的最小集合。
- 达到任务卡的验收条件后停止，不因附带发现自动扩面。
"""

TASK_EXECUTION_BOUNDARY = """--- 任务执行边界（ingest/query） ---
- 执行期间只读本次派发的规范段与程序 JSON 的 `prompt` 字段；除非报错或阻塞需调试，不读取脚本源码中的提示词模板。
- 连续执行至完成条件；仅在完成、用户明确暂停或出现无法安全决定的真实阻塞时结束。中断时说明已完成范围、未完成校验、阻塞原因与所需输入；阶段切换、执行卡、`pipeline_plan`、`CONTINUE`、`next_action` 与中间输出均属内部流程，不中断。
"""

INGEST_EXECUTION_SUPPLEMENT = """--- 摄入执行补充 ---
- 当前 `create` stage 完成并落盘后，再调用下一 stage，直至 stage 3；不得由日志推断或跳过当前 stage。
- 会议纪要或长文档全文只在首次 LLM 阅读时读取一次，并同步完成归类、命名与 wiki 编码；不为分类、纠错或建边重复阅读全文。
- 编码落盘后仅运行一次 `ingest_check.py --graph`，以同时验证结构与图边；不要先重复运行无 `--graph` 的结构校验。
- 程序给出 prefill、骨架或语义槽模板时，直接按模板填写；不读取解析器源码反推格式。
"""

EXPERIENCE_TRIGGER_NOTICE = """--- 轻量经验层（事件触发） ---
- 先查 playbook；playbook 命中且未明确允许补充经验时，禁止调用 experience。
- 仅在 playbook 未覆盖或存在策略/解析/写作/施工歧义时，运行 experience_recall.py recall，最多采纳 3 条；无命中立即继续主流程。
- 经验只提示策略，不是事实源；不得绕过 raw、schema、graph、stage guard 或回归。
"""

BUILD_ENGINEERING_LOCATOR_DISCIPLINE = """--- 建设任务工程精确读取门 ---
- 先运行 `python3 .scripts/engineering_graph.py impact <target> --verify`，把 node/contract/capability、code-guidance section、推荐精确 locator 与最小验证命令作为影响卡；不要先枚举整个 YAML。
- 建设域有 DSH capability seam：`dsh.build_tools.BuildLocatorCockpit` 只暴露 build_engineering_impact / build_locator_read / build_locator_list；guard 强制 impact 完成后才可精读，list 必须带 prefix。
- 推荐精确 locator（先直接 read）直接 `read`；只有推荐不足时才调用 filtered `list --prefix`，再用返回的 `md:`/`yaml:`/`py:`/`Lx-Ly` locator 执行 `read`，只把命中片段交给 Agent。
- `rg` 只用于定位候选文件或符号。仅 locator 明确不支持、报错，或所需上下文本身跨多个块时才定向扩大读取，并在工作更新中说明原因。
- Raw、Wiki 各用专用 locator；功能性任务不调用工程 locator。
"""


def classify_query(query: str) -> list[str]:
    """返回可组合查询意图；证据需求不再被关键词顺序单选覆盖。"""
    facets = [profile for profile, hints in QUERY_PROFILE_HINTS.items() if any(hint in query for hint in hints)]
    return facets or ["fact"]


def engineering_context(task):
    """返回 task 的最小工程上下文包；元图失效即路由失败，避免静默走旧入口。"""
    nodes, edges, capabilities, contracts, verification, script_contracts, untracked = load_engineering_graph()
    failures, _warnings = validate_engineering_graph(nodes, edges, capabilities, contracts, verification, script_contracts, untracked)
    if failures:
        print("ERROR: 工程元图无效: " + "; ".join(failures), file=sys.stderr)
        sys.exit(1)
    if task not in capabilities:
        return ""
    return engineering_capability(nodes, capabilities, task, compact=True)

# paper-only schema section: content != paper 时过滤掉
PAPER_ONLY_SCHEMA = ["研究方向定位与 Hub Scope"]
# 论文专属的 ingest section(content != paper 时移除)
PAPER_ONLY_INGEST = ["引文节点", "auto-merge:引文节点自动吸收"]
OPTIONAL_SECTIONS = {"会议纪要预处理", "引文节点", "auto-merge:引文节点自动吸收", "会议纪要 keyword", "简历摄入", "可疑确认", "混合查询规则"}

# Stage-aware 段截断(避免 stage 1 派发超长段全量,只在指定阶段取前 N chars)。
# 格式: (filepath, prefix) -> {stage_num: max_chars}
# 取整(粗略指导,不精确): 1 个中文字符 ≈ 1 char(UTF-8 多字节按 char 计)。
STAGE_TRUNCATE = {
    ("operations/INGEST.md", "会议纪要预处理"): {1: 4000},  # stage 1 仅流水线核心
    ("operations/INGEST.md", "阶段二"): {1: 3000},  # stage 1 不展开巩固细节
}

# ===== 映射表(壳:task → 该读的规范 section,前缀匹配) =====
ROUTES = {
    "ingest": {
        "file": "operations/INGEST.md",
        "needs_schema": True,
        "modes": {
            "create": {
                "stages": [
                    # stage 1: 编码 (Encoding) — 读 raw 写 summary
                    {
                        "name": "编码 (Encoding)",
                        "ingest": ["会话级总计划", "两种模式", "阶段一", "长文动态颗粒度", "编码阶段最小读取", "通用 raw 路径约束",
                                   "通用来源标记", "重要约束"],
                        "schema": ["页面类型", "Frontmatter 模板", "标准 section 结构"],
                    },
                    # stage 2: 巩固 (Consolidation) — 识别节点建边 hub
                    {
                        "name": "巩固 (Consolidation)",
                        "ingest": ["阶段二", "巩固模式", "节点类型",
                                   "描述性短语校验", "graph.db 边格式", "引文节点",
                                   "auto-merge:引文节点自动吸收", "级联创建策略",
                                   "Raw 文档包节点与 Wiki 直连"],
                        # 研究方向/keyword 的完整执行规则已由 INGEST「阶段二」派发；
                        # 不重复加载 academic SCHEMA 同义段，避免弱模型在两份规则间漂移。
                        "schema": ["graph.db 边写作约束"],
                    },
                    # stage 3: 收尾 (Finalize) — log/check/自检
                    {
                        "name": "收尾 (Finalize)",
                        "ingest": ["收尾", "index.md 格式", "log.md 格式"],
                        "schema": [],
                    },
                ],
            },
            "update": {
                "sections": ["会话级总计划", "两种模式", "更新模式", "冲突处理分支", "通用 raw 路径约束",
                             "节点类型", "描述性短语校验",
                             "graph.db 边格式", "通用来源标记", "重要约束",
                             "Raw 文档包节点与 Wiki 直连"],
                "schema": ["页面类型", "Frontmatter 模板", "标准 section 结构",
                           "graph.db 边写作约束"],
            },
            "batch": {
                "sections": ["会话级总计划", "两种模式", "批量摄入子流程", "通用 raw 路径约束", "重要约束"],
                "schema": ["页面类型", "Frontmatter 模板", "标准 section 结构",
                           "graph.db 边写作约束", "研究方向定位与 Hub Scope"],
            },
        },
    },
    "query": {
        "file": "operations/QUERY.md",
        "sections": None,  # --full 审阅/调试时输出完整 QUERY 规范
    },
    "lint": {
        "file": "operations/LINT.md",
        "sections": ["触发方式", "检查清单", "SR 交叉验证", "冲突分级",
                     "时效性判定", "图相关检查", "输出"],
    },
    "sync": {
        "file": "operations/SYNC.md",
        "sections": ["定位", "触发方式", "核心思路", "执行步骤", "重要约束"],
    },
    "write": {
        "file": "operations/WRITE.md",
        "sections": ["触发与任务判定", "总体原则", "共享落笔约定", "事实与主张控制",
                     "起草工作流", "修改工作流", "输出、存放与日志", "能力边界"],
    },
    "scan": {
        "file": "operations/SCAN.md",
        "sections": ["触发方式", "执行步骤", "版本分组判定", "重要约束", "inbox"],
    },
    "inbox": {
        "file": "operations/INBOX.md",
        "sections": ["用途", "工作流程", "约束", "用户申明事实"],
    },
    "hub": {
        "file": "operations/HUB.md",
        "sections": ["最小模型", "成员归属", "Hub 动力学", "论文路由",
                     "Agent 与代码职责", "inbox 收尾自动维护",
                     "分裂", "合并", "兼容与迁移"],
    },
    "build": {
        "file": "operations/shared-conventions.md",
        "sections": ["下游同步清单", "建设交付的工程文档维护", "系统设计原则"],
    },
    "research": {
        "file": "operations/RESEARCH.md",
        "sections": None,  # 全文输出
    },
    "frontier": {
        "file": "operations/FRONTIER.md",
        "sections": None,  # 全文输出
    },
}

# 能力是工作状态内按需加载的规范包；它不改变当前 task/state。
# 顶层 --task write 继续作为 general profile 的兼容入口。
CAPABILITY_ROUTES = {
    "write": {
        "general": [
            (ROUTES["write"]["file"], ROUTES["write"]["sections"]),
        ],
        "academic": [
            ("operations/WRITE.md", ["总体原则", "共享落笔约定", "事实与主张控制"]),
            ("operations/research/physics-manuscript-editing.md", None),
        ],
    },
}


def extract_section(text, prefix):
    """前缀匹配截取 section(含标题行,到下一个同级或更高级标题为止)。

    即 ## 段会包含其下全部 ### / #### 子段;### 段只到下一个 ### / ##。
    """
    pat = r'^(#{2,4}) ([^\n]*' + re.escape(prefix) + r'[^\n]*)\n'
    m = re.search(pat, text, re.M)
    if not m:
        return None
    level = len(m.group(1))
    start = m.start()
    body_start = m.end()
    rest = text[body_start:]
    stop_pat = re.compile(r'^#{2,' + str(level) + r'} ', re.M)
    sm = stop_pat.search(rest)
    body = rest[:sm.start()] if sm else rest
    return (text[start:body_start] + body).rstrip()


def load_sections(filepath, prefixes, optional=False, stage=None):
    """从文件截取多个 section(前缀匹配)。返回 (命中列表, 未命中列表)。

    stage: 当前 ingest stage 编号(1/2/3);用于应用 STAGE_TRUNCATE 截断规则,
    避免 stage 1 派发超长段全量。
    """
    p = REPO / filepath
    if not p.exists():
        return [], [(filepath, "文件不存在")]
    if prefixes is None:
        text = p.read_text(encoding="utf-8")
        return [(filepath + " (全文)", text)], []
    hits = []
    misses = []
    for pf in prefixes:
        located = engineering_loc.read_markdown_query(filepath, pf)
        if located.get("ok"):
            content = located["content"]
            # 应用 stage-aware 截断
            truncate_cfg = STAGE_TRUNCATE.get((filepath, pf), {})
            if stage in truncate_cfg:
                max_chars = truncate_cfg[stage]
                if len(content) > max_chars:
                    note = (
                        "\n\n<!-- [stage " + str(stage) + " 截断] 完整段 "
                        + str(len(content)) + " chars; 此处仅取前 "
                        + str(max_chars) + " chars。 -->\n"
                    )
                    content = content[:max_chars] + note
            label = (
                f"{filepath}#{located['locator']} "
                f"[L{located['start_line']}-L{located['end_line']}]"
            )
            hits.append((label, content))
        else:
            misses.append((filepath, pf))
    return hits, misses


def split_misses(misses):
    """旧规范改名时，非核心段落告警但不阻断派发。"""
    required = [m for m in misses if m[1] not in OPTIONAL_SECTIONS]
    optional = [m for m in misses if m[1] in OPTIONAL_SECTIONS]
    return required, optional


def emit_query_profile(query, profiles, stage, output_format):
    sections = list(QUERY_STAGE_SECTIONS[stage])
    if stage == "start":
        for profile in profiles:
            sections.extend(QUERY_PROFILE_SECTIONS[profile])
    hits, misses = load_sections("operations/QUERY.md", sections)
    warnings = [{"file": f, "section": s} for f, s in misses]
    rules = "\n\n".join(f"--- {label} ---\n{body}" for label, body in hits)
    if stage == "start":
        context = engineering_context("query")
        text = f"--- 工程上下文(按元图派发) ---\n{context}\n\n{USE_TASK_EXECUTION_DISCIPLINE}\n{TASK_EXECUTION_BOUNDARY}\n{EXPERIENCE_TRIGGER_NOTICE}\n{rules}"
    else:
        text = rules
    backend_notice = ""
    if execution_mode() == "api":
        backend_notice = f"阶段 {stage}：LLM=API（{configured_model()}）；输出须遵守当前任务卡并经既有校验。"
    result = {
        "task": "query", "profile": "+".join(profiles), "profiles": profiles, "stage": stage, "query": query,
        "sections": [label for label, _ in hits], "warnings": warnings,
        "backend_notice": backend_notice,
        "estimated_chars": len(text), "estimated_tokens": max(1, len(text) // 3), "prompt": text,
    }
    if output_format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"[query 路由] profile={'+'.join(profiles)} stage={stage} sections={len(hits)} chars={len(text)}", file=sys.stderr)
        if backend_notice:
            print(f"[query 后端] {backend_notice}", file=sys.stderr)
        print(text)
    if warnings:
        print(f"[query 路由] WARN: {len(warnings)} 个可选 section 未命中", file=sys.stderr)
    return


def filter_schema(schema_list, content):
    """过滤 paper-only schema sections(content != paper 时移除)。"""
    if content == "paper":
        return schema_list
    return [s for s in schema_list if s not in PAPER_ONLY_SCHEMA]


def filter_ingest(ingest_list, content, subproject, source_kind):
    """按内容、子项目和来源类型派发最小 ingest 规则包。"""
    sections = list(ingest_list)
    if content != "paper":
        sections = [section for section in sections if section not in PAPER_ONLY_INGEST]

    is_encoding = "阶段一" in sections
    is_graph_work = "阶段二" in sections or "更新模式" in sections
    if is_encoding and source_kind == "meeting":
        sections.append("会议纪要预处理")
    if subproject == "academic":
        sections.append("学术 raw 目录路径")
    if source_kind == "meeting":
        sections.append("会议来源与 SR 约束")
    if is_graph_work:
        sections.extend(["通用图边约束", "核心导航判据"])
        if subproject == "academic" and content == "paper":
            sections.append("学术图边关系")
        if source_kind == "meeting":
            sections.append("会议图边关系")
    return sections


def resolve_ingest(route, args):
    """ingest:按 mode/content/source-kind 取最小规范包。"""
    mode = args.mode or "create"
    content = args.content or ("other" if args.source_kind == "meeting" else
                               ("paper" if args.subproject == "academic" else "other"))
    modes = route["modes"]
    if mode not in modes:
        print(f"ERROR: 未知 mode '{mode}'。可用: {list(modes.keys())}", file=sys.stderr)
        sys.exit(1)

    mode_cfg = modes[mode]

    # create 模式有 stages;update/batch 用固定 sections
    if "stages" in mode_cfg:
        stages = mode_cfg["stages"]
        total = len(stages)
        if args.stage:
            si = args.stage - 1
            if si < 0 or si >= total:
                print(f"ERROR: stage {args.stage} 超范围(create 共 {total} 个 stage)", file=sys.stderr)
                sys.exit(1)
            selected = [(si, stages[si])]
        else:
            selected = list(enumerate(stages))
        return selected, content, mode, total
    else:
        return None, content, mode, 0


def emit_capability(name: str, profile: str) -> None:
    """输出可在当前工作状态内组合的能力规范，不改变 task/state。"""
    profiles = CAPABILITY_ROUTES.get(name)
    if profiles is None or profile not in profiles:
        available = sorted(profiles or {})
        print(
            f"ERROR: 未知 capability/profile '{name}/{profile}'。可用 profile: {available}",
            file=sys.stderr,
        )
        sys.exit(1)

    all_hits = []
    all_misses = []
    for filepath, sections in profiles[profile]:
        hits, misses = load_sections(filepath, sections)
        all_hits.extend(hits)
        all_misses.extend(misses)

    print(f"=== capability: {name}  profile: {profile} ===", file=sys.stderr)
    context = engineering_context(name)
    if context:
        print("--- 工程上下文(按元图派发) ---")
        print(context)
        print()
    print(USE_TASK_EXECUTION_DISCIPLINE)
    print(EXPERIENCE_TRIGGER_NOTICE)
    for label, content_text in all_hits:
        print(f"--- {label} ---")
        print(content_text)
        print()

    if all_misses:
        print("--- 未命中 section(可能已改名,请检查) ---", file=sys.stderr)
        for filepath, miss in all_misses:
            print(f"  {filepath} :: {miss}", file=sys.stderr)
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="按 task 分发规范 prompt")
    ap.add_argument("--task", help="任务类型(ingest/query/lint/sync/write/scan/inbox/hub/build/research/frontier)")
    ap.add_argument("--capability", choices=sorted(CAPABILITY_ROUTES),
                    help="在当前 task/state 内按需加载的能力")
    ap.add_argument("--capability-profile", default="general",
                    help="能力 profile（write: general/academic）")
    ap.add_argument("--subproject", help="子项目(admin/teaching/business/private;cross-domain 跨域),ingest 时用（论文 PDF 走 ingest_paper.py playbook,不走 route.py；非inbox academic 论文走 ingest_paper.py --raw；private 物理隔离独立 graph.db）")
    ap.add_argument("--mode", help="ingest 模式: create(默认)/update/batch")
    ap.add_argument("--content", help="ingest 内容类型: paper(默认)/other")
    ap.add_argument("--source-kind", choices=["ordinary", "meeting"], default="ordinary",
                    help="ingest 来源类型: ordinary(默认)/meeting")
    ap.add_argument("--stage", type=int, help="ingest create 模式的 stage: 1/2/3")
    ap.add_argument("--list", action="store_true", help="分类列出 task/state 与 capability")
    ap.add_argument("--query", default="", help="query 任务的用户问题")
    ap.add_argument("--profile", choices=["auto", *QUERY_PROFILE_SECTIONS], default="auto")
    ap.add_argument("--query-stage", choices=QUERY_STAGE_SECTIONS, default="start",
                    help="query 派发阶段: start(默认)/evidence/continue/answer")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--full", action="store_true", help="query 任务输出完整规范(审阅/调试)")
    args = ap.parse_args()

    if args.list:
        print("可用 task/state（write 为兼容别名）:")
        for k, v in ROUTES.items():
            suffix = " [compat -> capability write/general]" if k == "write" else ""
            print(f"  {k}: {v['file']}{suffix}")
        print("可用 capability:")
        for name, profiles in CAPABILITY_ROUTES.items():
            print(f"  {name}: profiles={','.join(sorted(profiles))}")
        return


    if args.capability:
        if args.task:
            print("ERROR: --task 与 --capability 分别表示状态路由和按需能力，请分两次调用。", file=sys.stderr)
            sys.exit(1)
        emit_capability(args.capability, args.capability_profile)
        return

    if not args.task or args.task not in ROUTES:
        print(f"ERROR: 未知 task '{args.task}'。可用: {list(ROUTES.keys())}", file=sys.stderr)
        sys.exit(1)

    route = ROUTES[args.task]

    if args.task == "ingest":
        valid_subprojects = {"academic", "admin", "teaching", "business", "private"}  # academic 仅旧手动流程(非inbox来源);private 物理隔离独立 graph.db
        if args.subproject not in valid_subprojects:
            print(
                "ERROR: ingest 必须指定 --subproject "
                f"({', '.join(sorted(valid_subprojects))})，不得依赖默认域。",
                file=sys.stderr,
            )
            sys.exit(1)
        if (args.mode or "create") == "create" and args.stage is None:
            print(
                "ERROR: create ingest 必须指定 --stage 1|2|3；每阶段落盘后再调用下一阶段。",
                file=sys.stderr,
            )
            sys.exit(1)
        if args.source_kind == "meeting" and args.content == "paper":
            print("ERROR: meeting 来源必须使用 --content other，不能派发论文模板。", file=sys.stderr)
            sys.exit(1)
        if (args.mode or "create") != "create" and args.stage is not None:
            print("ERROR: --stage 仅适用于 create ingest。", file=sys.stderr)
            sys.exit(1)

    if args.task == "query" and not args.full:
        profiles = classify_query(args.query) if args.profile == "auto" else [args.profile]
        emit_query_profile(args.query, profiles, args.query_stage, args.format)
        return

    # ===== ingest 特殊处理(stage 化) =====
    if args.task == "ingest":
        selected_stages, content, mode, total_stages = resolve_ingest(route, args)
        print(
            f"[ingest 路由] mode={mode}  content={content}  source-kind={args.source_kind}  "
            f"subproject={args.subproject or '(未指定)'}",
            file=sys.stderr,
        )
        notice_stage = args.stage if args.stage else None
        backend_notice = ingest_backend_notice(stage=notice_stage)
        if backend_notice:
            print(f"[ingest 后端] {backend_notice}", file=sys.stderr)
        if content == "paper" and args.subproject == "academic":
            print(
                "[ingest 重定向] academic 论文（非 inbox 来源）现已支持代码驱动流水线：\n"
                "  python3 .scripts/ingest_paper.py --raw <raw/paper.md 路径>\n"
                "复用 inbox 全流程（wiki 生成→语义槽→校验→命题→图→收尾），无需手动 stage 1/2/3。\n"
                "下方 stage 任务卡仅在需手动介入或 agent 模式时参考。",
                file=sys.stderr,
            )
            from llm_structured import ingest_mode
            if ingest_mode() == "api":
                print(
                    "[ingest API 后端] 当前 INGEST_BACKEND=api，--raw 模式全自动代码驱动。",
                    file=sys.stderr,
                )
        if selected_stages:
            print(f"[ingest stage] {len(selected_stages)} 个 stage (create 共 {total_stages})", file=sys.stderr)
        elif mode == "batch":
            print("[ingest batch] 每项完成编码后，以 inbox_ingest.py complete-batch 连续执行落位、"
                  "API 受控语义、建图、校验和清理；不要为 batch 传 --stage。", file=sys.stderr)

        print("--- 工程上下文(按元图派发) ---")
        print(engineering_context("ingest"))
        print()
        print(USE_TASK_EXECUTION_DISCIPLINE)
        print(TASK_EXECUTION_BOUNDARY)
        print(EXPERIENCE_TRIGGER_NOTICE)
        print(INGEST_EXECUTION_SUPPLEMENT)
        all_hits = []
        all_misses = []

        if selected_stages:
            # create 模式:按 stage 输出
            for si, stage in selected_stages:
                stage_num = si + 1
                print(f"{'═' * 20} STAGE {stage_num}/{total_stages}: {stage['name']} {'═' * 20}\n")
                main_sections = filter_ingest(stage["ingest"], content, args.subproject, args.source_kind)
                schema_sections = filter_schema(stage["schema"], content)

                hits, misses = load_sections(route["file"], main_sections, stage=stage_num)
                all_hits.extend(hits)
                required, optional = split_misses(misses)
                all_misses.extend(required)
                for f, miss in optional:
                    print(f"WARN: 可选 section 未命中 {f} :: {miss}", file=sys.stderr)

                if route.get("needs_schema") and args.subproject and schema_sections:
                    schema_file = f"{args.subproject}/SCHEMA.md"
                    sch_hits, sch_misses = load_sections(schema_file, schema_sections)
                    all_hits.extend(sch_hits)
                    required, optional = split_misses(sch_misses)
                    all_misses.extend(required)
                    for f, miss in optional:
                        print(f"WARN: 可选 section 未命中 {f} :: {miss}", file=sys.stderr)
                print()
        else:
            # update/batch 模式:固定 sections
            mode_cfg = route["modes"][mode]
            main_sections = filter_ingest(mode_cfg["sections"], content, args.subproject, args.source_kind)
            schema_sections = filter_schema(mode_cfg["schema"], content)

            hits, misses = load_sections(route["file"], main_sections)
            all_hits.extend(hits)
            required, optional = split_misses(misses)
            all_misses.extend(required)
            for f, miss in optional:
                print(f"WARN: 可选 section 未命中 {f} :: {miss}", file=sys.stderr)

            if route.get("needs_schema") and args.subproject and schema_sections:
                schema_file = f"{args.subproject}/SCHEMA.md"
                sch_hits, sch_misses = load_sections(schema_file, schema_sections, stage=None)  # update/batch 不切 stage
                all_hits.extend(sch_hits)
                required, optional = split_misses(sch_misses)
                all_misses.extend(required)
                for f, miss in optional:
                    print(f"WARN: 可选 section 未命中 {f} :: {miss}", file=sys.stderr)

        # 输出
        for label, content_text in all_hits:
            print(f"--- {label} ---")
            print(content_text)
            print()

        if all_misses:
            print("--- 未命中 section(可能已改名,请检查) ---", file=sys.stderr)
            for f, miss in all_misses:
                print(f"  {f} :: {miss}", file=sys.stderr)
            sys.exit(1)
        return

    # ===== 非 ingest 任务(原有逻辑) =====
    main_sections = route.get("sections")
    all_hits = []
    all_misses = []

    hits, misses = load_sections(route["file"], main_sections)
    all_hits.extend(hits)
    all_misses.extend(misses)

    print(f"=== task: {args.task} ===", file=sys.stderr)
    context = engineering_context(args.task)
    if context:
        print("--- 工程上下文(按元图派发) ---")
        print(context)
        print()
    if args.task != "build":
        print(USE_TASK_EXECUTION_DISCIPLINE)
    else:
        print(BUILD_ENGINEERING_LOCATOR_DISCIPLINE)
    if args.task in {"query", "ingest", "write", "build"}:
        print(EXPERIENCE_TRIGGER_NOTICE)
    for label, content_text in all_hits:
        print(f"--- {label} ---")
        print(content_text)
        print()

    if all_misses:
        print("--- 未命中 section(可能已改名,请检查) ---", file=sys.stderr)
        for f, miss in all_misses:
            print(f"  {f} :: {miss}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
