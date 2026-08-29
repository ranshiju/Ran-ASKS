#!/usr/bin/env python3
"""query_orchestrate.py — A+ v3 编排层(程序侧,不裁决语义)

v3 边界(GPT 修正):程序不裁决开放语义,但核验客观事实、约束执行过程、要求 LLM 对证据不足作显式说明。
- Evidence Profile:程序读 frontmatter 返回**原始证据事实**(source_presence/source_types/version_status/conflict_markers),不返回"权威充分/时间匹配"等语义结论
- 去六维门控:不判 C_slot/C_consistency(语义二维降为 LLM 内部检查清单,不程序输出不机械触发回环)
- 回环:LLM 自主决定 + 每轮须提交 gap+candidate+candidate_basis+action+section+expected_gain;程序只做可机械检查(action key 重复/预算/回环上限),不判"是否可能获新信息"
- stop_reason + required_disclosures:诚实边界可追溯
- v4(2026-07-23):槽位清单与缺口回检——LLM 声明预期槽位(init --slots),每轮报告覆盖(exec --covered),程序机械算缺口;stop_reason 细化 sufficient_complete/sufficient_partial。修 pilot 漏停(M1/M3/M4)。见 operations/RECOVERY.md 候补策略

入口(init/exec/finalize),由 Codex 对话里的 LLM 显式调用。
"""
from __future__ import annotations
import argparse
import json
import re
import inspect
import sys
import time
import uuid
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / ".scripts"))
import query_actions as actions
import ingest_common
import wiki_locator as wl

DEFAULT_WINDOW = 128_000
BUDGET_RATIO = 0.5
BUDGET_WARN_RATIO = 0.7  # 预算用到 70% 触发降级提示
MAX_LOOP = 3
# ADR-003: 多轮渐进式相似边召回——第一轮纯知识边,续轮逐步放开相似边
# loop_count=0 → 0(排除), 1 → 3(保守动态K), 2+ → 5(完整动态K)
SIMILAR_TOPK_PROGRESSION = [0, 3, 5]
def _similar_topk_for_round(loop_count: int) -> int:
    """按回环轮次返回相似边上限(渐进式:首轮排除,续轮逐步放开)。"""
    idx = min(loop_count, len(SIMILAR_TOPK_PROGRESSION) - 1)
    return SIMILAR_TOPK_PROGRESSION[idx]

# DSH cockpit stage guard: API 模式下按查询阶段约束允许的动作
# start=只许发现类(graph_search/recall/neighbors),evidence/continue=许 read_section,answer=不许再读
_DISCOVERY = {"graph_search", "node_resolve", "semantic_search",
               "graph_neighbors", "graph_relations", "graph_hub_of",
               "admin_recall", "wiki_recall"}
STAGE_ACTIONS = {
    "start": _DISCOVERY,
    "evidence": _DISCOVERY | {"read_section", "read_raw"},
    "continue": _DISCOVERY | {"read_section", "read_raw"},
    "answer": set(),
}

# 从 query_actions 函数签名派生 action→参数(名,默认值)，消除 _build_step 的 if/elif 硬编码。
# 新增工具只需加函数并注册到 DISPATCH，_build_step 自动适配。
_ACTION_SIGS: dict[str, list[tuple[str, str]]] = {
    name: [(p, (v.default if v.default is not inspect.Parameter.empty else ""))
           for p, v in inspect.signature(fn).parameters.items()]
    for name, fn in [
        ("read_section", actions.read_section),
        ("read_raw", actions.read_raw),
        ("graph_search", actions.graph_search),
        ("graph_neighbors", actions.graph_neighbors),
        ("graph_relations", actions.graph_relations),
        ("graph_hub_of", actions.graph_hub_of),
        ("node_resolve", actions.node_resolve),
        ("semantic_search", actions.semantic_search),
        ("admin_recall", actions.admin_recall),
        ("wiki_recall", actions.wiki_recall),
    ]
}
SESSIONS_DIR = _REPO / ".scripts" / "query_sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class QuerySession:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    query: str = ""
    query_type: str = ""
    visited_action_keys: set[str] = field(default_factory=set)   # action 级去重("page#section" 等)
    visited_pages: list[str] = field(default_factory=list)
    evidence_profile: dict = field(default_factory=lambda: {
        "source_presence": [], "source_types": [], "version_status": [], "conflict_markers": [],
    })
    required_disclosures: list[str] = field(default_factory=list)
    token_used: int = 0
    token_budget: int = 0
    plan_count: int = 0
    loop_count: int = 0
    hard_stopped: bool = False
    stop_reason: str = ""
    slot_checklist: list[str] = field(default_factory=list)   # v4: LLM 声明预期槽位
    covered_slots: list[str] = field(default_factory=list)   # v4: LLM 报告已覆盖槽位
    search_strategy: dict = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)
    ts: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    stage: str = "start"       # DSH cockpit: start→evidence→continue→answer
    mode: str = "agent"        # agent=原有行为不变, api=启用 stage 守卫
    read_sources: list[str] = field(default_factory=list)   # 成功读取的 raw locator(Batch 3 read_raw 填充)

    def slot_gaps(self) -> list[str]:
        """已声明但未覆盖的槽位(机械集合减法,不判语义)。"""
        return [s for s in self.slot_checklist if s not in self.covered_slots]

    def remaining_budget(self) -> int:
        return max(0, self.token_budget - self.token_used)

    def __post_init__(self):
        if not self.token_budget:
            self.token_budget = int(DEFAULT_WINDOW * BUDGET_RATIO)

    # ---- 硬否决(可机械检查,不判语义) ----
    def deny_reason(self, step: dict, is_continuation: bool = False) -> Optional[str]:
        act = step.get("action"); inp = step.get("input", {}) or {}
        # 1. 动作合法性
        ALLOWED = set(_ACTION_SIGS.keys())
        if act not in ALLOWED:
            return f"非法动作 {act}(允许: {ALLOWED})"
        # 1b. DSH cockpit stage guard: API 模式下按阶段约束动作
        if self.mode == "api" and self.stage in STAGE_ACTIONS:
            allowed_in_stage = STAGE_ACTIONS[self.stage]
            if act not in allowed_in_stage:
                return f"STAGE_GUARD: 阶段 {self.stage} 不允许 {act}(允许: {allowed_in_stage or '仅 answer'})"
        # 2. action key 重复(同一定位动作不重复执行)
        if act in set(_ACTION_SIGS.keys()) and isinstance(inp, dict):
            if act == "read_section":
                key = f'{act}:{inp.get("page","")}#{inp.get("section","")}'
            elif act in {"admin_recall", "wiki_recall"}:
                key = f'{act}:{inp.get("query", "")}'
            else:
                key = f"{act}:{json.dumps(inp, sort_keys=True, ensure_ascii=False)}"
            if key in self.visited_action_keys:
                return f"ACTION_ALREADY_VISITED: {key}(已读过,换 section 或换页)"
        # 3. 续检索须提交具体候选(可机械检查字段存在性,不判内容好坏)
        #    API 模式跳过：LLM 结构化决策(discover/read/answer)即继续理由,stage guard 已约束动作
        if is_continuation and self.mode != "api":
            for f in ("gap", "candidate", "action", "expected_gain"):
                if not step.get(f):
                    return f"续检索缺字段: {gap_required_hint(f)}(须 gap+candidate+action+expected_gain)"
        # 4. 预算
        if self.token_used > self.token_budget:
            return f"BUDGET_EXHAUSTED({self.token_used}>{self.token_budget})"
        # 5. 回环上限
        if self.loop_count > MAX_LOOP:
            return f"LOOP_LIMIT({self.loop_count}>{MAX_LOOP})"
        return None

    def record_visit(self, action: str, inp: dict):
        if action in set(_ACTION_SIGS.keys()):
            if action == "read_section":
                key = f'{action}:{inp.get("page","")}#{inp.get("section","")}'
            elif action in {"admin_recall", "wiki_recall"}:
                key = f'{action}:{inp.get("query", "")}'
            else:
                key = f"{action}:{json.dumps(inp, sort_keys=True, ensure_ascii=False)}"
            self.visited_action_keys.add(key)
        if action == "read_section":
            pg = inp.get("page", "")
            if pg and pg not in self.visited_pages:
                self.visited_pages.append(pg)

    def add_tokens(self, n: int):
        self.token_used += n

    def record_plan(self, plan: list):
        self.plan_count += 1
        self.loop_count = max(0, self.plan_count - 1)

    def record_strategy(self, strategy: dict):
        self.search_strategy = strategy
        summary = "跳过轻量检索策略" if strategy.get("status") == "skipped" else "制定轻量检索策略"
        reason = strategy.get("reason", "")
        self.record_step({"action": "search_strategy", "input": strategy,
                          "output_summary": f"{summary}: {reason}" if reason else summary,
                          "decision": "已记录"})

    def record_step(self, step: dict):
        step.setdefault("step", len(self.steps) + 1)
        step.setdefault("query_type", self.query_type)
        self.steps.append(step)

    def allowed_next_actions(self) -> list[str]:
        if self.hard_stopped or self.token_used > self.token_budget or self.loop_count > MAX_LOOP:
            return ["answer"]
        return [*_ACTION_SIGS.keys(), "answer"]

    def finalize_stop_reason(self, llm_decision: str):
        if self.token_used > self.token_budget:
            self.stop_reason = "budget_exhausted"; self.hard_stopped = True
        elif self.loop_count > MAX_LOOP:
            self.stop_reason = "loop_limit"; self.hard_stopped = True
        elif llm_decision == "no_actionable_candidate":
            self.stop_reason = "no_actionable_candidate"
        else:
            # v4: 槽位回检——有声明槽位且有缺口 → sufficient_partial(须附缺口说明)
            gaps = self.slot_gaps()
            if self.slot_checklist and gaps:
                self.stop_reason = "sufficient_partial"
                self.required_disclosures.append(
                    f"槽位未覆盖: {', '.join(gaps)}(LLM 判断缺失槽无可行候选,已明确缺口)"
                )
            else:
                self.stop_reason = "sufficient_complete"

    def to_trace_jsonl(self) -> str:
        return "\n".join(json.dumps(s, ensure_ascii=False) for s in self.steps)

    def budget_warned(self) -> bool:
        return self.token_used > self.token_budget * BUDGET_WARN_RATIO

    def snapshot(self) -> dict:
        return {
            "query_type": self.query_type,
            "visited_pages": self.visited_pages,
            "token_used": self.token_used, "token_budget": self.token_budget,
            "budget_warned": self.budget_warned(),
            "plan_count": self.plan_count, "loop_count": self.loop_count,
            "hard_stopped": self.hard_stopped, "stop_reason": self.stop_reason,
            "evidence_profile": self.evidence_profile,
            "required_disclosures": self.required_disclosures,
            "slot_checklist": self.slot_checklist,
            "covered_slots": self.covered_slots,
            "slot_gaps": self.slot_gaps(),
            "search_strategy": self.search_strategy,
            "remaining_budget": self.remaining_budget(),
            "allowed_next_actions": self.allowed_next_actions(),
            "stage": self.stage, "mode": self.mode,
            "read_sources": self.read_sources,
        }


def gap_required_hint(f: str) -> str:
    return {"gap": "具体缺口", "candidate": "明确候选来源", "action": "具体动作", "expected_gain": "预期信息增益"}.get(f, f)


# ============ Evidence Profile(读 frontmatter,返回原始事实,不判语义) ============
def read_frontmatter(page: str) -> dict:
    p = wl.resolve_wiki_path(page)
    if p is None:
        return {"_error": "file not found"}
    c = p.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---", c, re.S)
    if not m:
        return {"_error": "no frontmatter"}
    try:
        fm = yaml.safe_load(m.group(1)) or {}
        return fm if isinstance(fm, dict) else {"_error": "fm not dict"}
    except Exception as e:
        return {"_error": f"yaml: {e}"}


def collect_evidence_facts(session: QuerySession, page: str):
    """读 frontmatter,把原始事实追加进 evidence_profile。不判语义(不返回 satisfied/unsatisfied)。"""
    fm = read_frontmatter(page)
    if "_error" in fm:
        session.required_disclosures.append(f"页 {page} frontmatter 读取失败: {fm['_error']}")
        return {"page": page, "error": fm["_error"]}
    sources = fm.get("sources", [])
    src_cnt = len(sources) if isinstance(sources, list) else (1 if sources else 0)
    source_type = fm.get("source_type", "unknown")
    status = fm.get("status", "unknown")
    superseded_by = fm.get("superseded_by", "")
    related = fm.get("related", [])
    # 追加原始事实(非语义结论)
    if src_cnt > 0:
        session.evidence_profile["source_presence"].append({"page": page, "count": src_cnt})
    else:
        session.required_disclosures.append(f"页 {page} 无 sources(无 raw 回溯链)")
    session.evidence_profile["source_types"].append({"page": page, "type": source_type})
    session.evidence_profile["version_status"].append({"page": page, "status": status, "superseded_by": superseded_by})
    if status == "deprecated" and not superseded_by:
        session.evidence_profile["conflict_markers"].append({"page": page, "marker": "deprecated 无 superseded_by"})
        session.required_disclosures.append(f"页 {page} deprecated 但无 superseded_by(版本链未闭合)")
    if isinstance(related, list) and related:
        session.evidence_profile["conflict_markers"].append({"page": page, "marker": "has related", "targets": related})
    return {"page": page, "source_count": src_cnt, "source_type": source_type, "status": status}


def execute_plan(session: QuerySession, plan: list[dict], is_continuation: bool = False) -> dict:
    session.record_plan(plan)
    results, denials = [], []
    # ADR-003: graph_neighbors 按 loop_count 渐进式注入 similar_topk
    _sim_topk = _similar_topk_for_round(session.loop_count)
    for step in plan:
        action = step.get("action"); inp = dict(step.get("input", {}) or {})
        if action == "graph_neighbors" and "similar_topk" not in inp:
            inp["similar_topk"] = str(_sim_topk)
        deny = session.deny_reason(step, is_continuation)
        if deny:
            allowed = ["choose_different_section", "answer_with_limitation"] if "VISITED" in deny else ["answer_with_limitation"]
            denials.append({"action": action, "input": inp, "denial_reason": deny, "allowed_options": allowed})
            session.record_step({"action": action, "input": inp, "output_summary": f"否决: {deny}",
                                  "decision": "否决(硬约束)"})
            continue
        r = actions.execute(action, inp)
        session.add_tokens(r["tokens"])
        session.record_visit(action, inp)
        if action == "read_section" and isinstance(inp, dict) and r["ok"]:
            collect_evidence_facts(session, inp.get("page", ""))
        if action == "read_raw" and r["ok"]:
            loc = inp.get("locator", "")
            if loc and loc not in session.read_sources:
                session.read_sources.append(loc)
        result_entry = {"action": action, "input": inp, "ok": r["ok"], "tokens": r["tokens"],
                        "text_preview": r["text"][:200], "error": r["error"]}
        # gap2: 预算感知降级提示(程序提示,LLM 自主决定——不强制降级,因"是否须全文"是语义判断)
        if action == "read_section" and r["ok"] and session.budget_warned():
            sec = inp.get("section", "")
            if sec == "Content":
                result_entry["budget_hint"] = (
                    f"预算紧张({session.token_used}/{session.token_budget}),已读 Content 全文。"
                    f"后续候选页建议先读 Navigation(~100 tok)判相关再决定是否读 Content,或用锚点 [[page#slug]] 读子段"
                )
        results.append(result_entry)
        session.record_step({"action": action, "input": inp, "output_summary": r["text"][:120] if r["ok"] else f"ERROR: {r['error'][:120]}",
                             "tokens_est": {"in": r["tokens"], "out": 0}, "decision": "已执行"})
    if session.loop_count >= MAX_LOOP:
        session.hard_stopped = True
    return {"results": results, "denials": denials, "snapshot": session.snapshot()}


# ============ 入口 ============
def _save(session: QuerySession):
    d = {"session_id": session.session_id, "query": session.query, "query_type": session.query_type,
         "visited_action_keys": list(session.visited_action_keys), "visited_pages": session.visited_pages,
         "evidence_profile": session.evidence_profile, "required_disclosures": session.required_disclosures,
         "token_used": session.token_used, "token_budget": session.token_budget,
         "plan_count": session.plan_count, "loop_count": session.loop_count,
         "hard_stopped": session.hard_stopped, "stop_reason": session.stop_reason,
         "slot_checklist": session.slot_checklist, "covered_slots": session.covered_slots,
         "search_strategy": session.search_strategy,
         "steps": session.steps, "stage": session.stage, "mode": session.mode,
         "read_sources": session.read_sources}
    (SESSIONS_DIR / f"{session.session_id}.json").write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

def _load(sid: str) -> QuerySession:
    f = SESSIONS_DIR / f"{sid}.json"
    if not f.exists():
        print(json.dumps({"error": f"session {sid} 不存在"}, ensure_ascii=False)); sys.exit(1)
    d = json.loads(f.read_text(encoding="utf-8"))
    s = QuerySession(query=d["query"], query_type=d["query_type"])
    s.session_id = d["session_id"]; s.visited_action_keys = set(d["visited_action_keys"])
    s.visited_pages = d["visited_pages"]; s.evidence_profile = d["evidence_profile"]
    s.slot_checklist = d.get("slot_checklist", []); s.covered_slots = d.get("covered_slots", [])
    s.required_disclosures = d["required_disclosures"]; s.token_used = d["token_used"]
    s.plan_count = d["plan_count"]; s.loop_count = d["loop_count"]; s.hard_stopped = d["hard_stopped"]
    s.stop_reason = d["stop_reason"]; s.steps = d["steps"]
    s.search_strategy = d.get("search_strategy", {})
    s.stage = d.get("stage", "start"); s.mode = d.get("mode", "agent")
    s.read_sources = d.get("read_sources", [])
    return s

def _result(session, out):
    return {"session_id": session.session_id, "executed": len(out["results"]),
            "denials": out["denials"], "snapshot": session.snapshot(),
            "results_preview": [{"action": r["action"], "input": r["input"], "tokens": r["tokens"],
                                 "text_preview": r["text_preview"][:300], "ok": r["ok"]} for r in out["results"]]}

def cmd_init(args):
    session = QuerySession(query=args.query, query_type=args.query_type or "简单事实",
                           stage=getattr(args, "stage", "start") or "start",
                           mode=getattr(args, "mode", "agent") or "agent")
    if getattr(args, "slots", "") and args.slots.strip():
        session.slot_checklist = [x.strip() for x in args.slots.split(",") if x.strip()]
    strategy = json.loads(args.strategy) if getattr(args, "strategy", "") else {}
    if strategy:
        if not _valid_search_strategy(strategy):
            print(json.dumps({"error": "invalid search strategy"}, ensure_ascii=False)); sys.exit(1)
        session.record_strategy(strategy)
    plan = json.loads(args.plan).get("plan", []) if args.plan else []
    out = execute_plan(session, plan, is_continuation=False)
    _save(session)
    print(json.dumps(_result(session, out), ensure_ascii=False, indent=2))

def cmd_exec(args):
    session = _load(args.session)
    if getattr(args, "stage", "") and args.stage.strip() and args.stage != session.stage:
        session.stage = args.stage.strip()
    if getattr(args, "covered", "") and args.covered.strip():
        for x in args.covered.split(","):
            x = x.strip()
            if x and x not in session.covered_slots:
                session.covered_slots.append(x)
    plan = json.loads(args.plan).get("plan", [])
    out = execute_plan(session, plan, is_continuation=True)
    _save(session)
    print(json.dumps(_result(session, out), ensure_ascii=False, indent=2))

def cmd_finalize(args):
    session = _load(args.session)
    session.finalize_stop_reason(args.decision or "sufficient")
    _save(session)
    # 查询后轻量裸缩写消解：只查图 alias，不扫 raw（零 LLM，毫秒级）
    normalization = None
    try:
        import ingest_common as ic
        normalization = ic.lightweight_abbr_resolve(REPO)
        people_candidates = ic.detect_people_page_candidates(REPO)
        if people_candidates.get("candidates"):
            normalization["people_page_candidates"] = len(people_candidates["candidates"])
        if normalization.get("resolved"):
            print(f"[normalization] 消解 {normalization['resolved']} 条裸缩写 "
                  f"(剩余 {normalization.get('remaining', 0)})", file=sys.stderr)
    except Exception:
        pass
    evidence = [{"step": s.get("step"), "action": s.get("action"), "summary": s.get("output_summary", "")[:200]}
                for s in session.steps if s.get("action", "").startswith("read")]
    result = {"session_id": session.session_id, "snapshot": session.snapshot(),
              "evidence_bundle": evidence, "trace": session.to_trace_jsonl()}
    if normalization is not None:
        result["normalization"] = normalization
    print(json.dumps(result, ensure_ascii=False, indent=2))

def cmd_make_plan(args):
    """程序生成 plan JSON(治本:避免 LLM 手写 JSON 出错——可机械生成的不让 LLM 手写,原则8 延伸)。

    两种模式:
    - 单步:传 --action + 对应参数,输出单步 plan JSON
    - 批量:传 --steps-file(每行一个 action 的 key=value 参数),输出多步 plan JSON
    续检索步加 --gap/--candidate/--candidate-basis/--expected-gain
    """
    steps = []
    if args.steps_file:
        # 批量:每行 "action=read_section|page=x|section=y|reason=z"(续检索加 gap=..|candidate=..)
        for line in open(args.steps_file, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            kv = {}
            for pair in line.split("|"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    kv[k.strip()] = v.strip()
            steps.append(_build_step(kv))
    else:
        kv = {k: v for k, v in vars(args).items()
              if k not in ("cmd", "steps_file") and v is not None and v != ""}
        steps.append(_build_step(kv))
    print(json.dumps({"plan": steps}, ensure_ascii=False))

def _build_step(kv: dict) -> dict:
    """从 key=value 字典构造单步(按 action 组装 input)。

    参数清单从 _ACTION_SIGS 自动派生，新增工具只需在 query_actions 注册函数签名，
    _build_step 自动适配——不再需要在此维护 if/elif 硬编码。
    """
    action = kv.get("action", "read_section")
    inp = {}
    for param, default in _ACTION_SIGS.get(action, []):
        inp[param] = kv.get(param, default)
    step = {"action": action, "input": inp, "reason": kv.get("reason", "")}
    # 续检索字段(有 gap 即视为续检索步)
    for f in ("gap", "candidate", "candidate_basis", "expected_gain"):
        if kv.get(f):
            step[f] = kv[f]
    return step


def intent_to_plan(intent: dict) -> list[dict]:
    """把 LLM 的有限意图转换成合法动作；LLM 不直接编写执行动作。"""
    steps = []
    if intent.get("need_recall", True):
        steps.append(_build_step({"action": "wiki_recall", "query": intent.get("query", ""),
                                  "domain": intent.get("domain", ""), "topk": str(intent.get("topk", 8)),
                                  "reason": "程序根据意图生成初始召回"}))
    if intent.get("need_graph"):
        steps.append(_build_step({"action": "graph_search", "term": intent.get("graph_term", intent.get("query", "")),
                                  "reason": "程序根据意图生成图扩展"}))
    return [s for s in steps if s["input"].get("query", "x") or s["input"].get("term", "x")]

# ============ DSH cockpit: API 查询循环 + citation contract ============

def _valid_search_strategy(strategy) -> bool:
    if not isinstance(strategy, dict):
        return False
    if strategy.get("status") == "skipped":
        return bool(strategy.get("reason"))
    if strategy.get("status") != "drafted":
        return False
    return all(isinstance(strategy.get(key), list) and strategy.get(key)
               for key in ("slots", "clues", "search_order", "stop_conditions"))


def _api_decision_schema(obj):
    """校验 LLM 返回的决策 JSON。"""
    if not isinstance(obj, dict):
        return False
    decision = obj.get("decision")
    if decision not in ("discover", "read", "answer"):
        return False
    if decision == "answer":
        return isinstance(obj.get("answer", ""), str)
    plan = obj.get("plan")
    return isinstance(plan, list) and len(plan) > 0


def _check_citations(citations: list, read_sources: list) -> dict:
    """citation contract: 验证回答引用是否全部存在于 read_sources。

    不阻断回答，只标记核验状态（程序提供事实，不判语义）。
    """
    if not citations:
        return {"ok": False, "status": "no_citations", "message": "回答无引用"}
    unverified = [c for c in citations if c not in read_sources]
    if unverified:
        return {"ok": False, "status": "unverified", "unverified": unverified,
                "message": f"以下引用未经 read_raw 核验: {unverified}"}
    return {"ok": True, "status": "verified", "count": len(citations)}


def _build_api_prompt(session: QuerySession, last_results: list, round_num: int) -> str:
    """为 API 查询循环构建 LLM 提示（含状态上下文 + 上轮结果）。"""
    snap = session.snapshot()
    parts = [
        f"查询: {session.query}",
        f"当前阶段: {session.stage}",
        f"允许动作: {snap['allowed_next_actions']}",
        f"已读来源(raw locator): {session.read_sources}",
        f"已访问页: {snap['visited_pages']}",
        f"剩余预算: {snap['remaining_budget']} tokens",
        f"轮次: {round_num}/{MAX_LOOP + 2}",
    ]
    if snap.get("slot_gaps"):
        parts.append(f"未覆盖槽位: {snap['slot_gaps']}")
    if last_results:
        parts.append("上轮执行结果:")
        for r in last_results:
            preview = r.get("text_preview", "")[:200]
            parts.append(f"  - {r['action']}({r.get('input', {})}): {preview}")
    else:
        parts.append("这是第一轮，尚未执行任何动作。")
    parts.append("")
    parts.append("输出 JSON，格式如下:")
    parts.append('  {"decision": "discover|read|answer",')
    parts.append('   "strategy": {"status": "drafted|skipped", "slots": [...], "clues": [...], "search_order": [...], "stop_conditions": [...], "reason": "..."},')
    parts.append('   "plan": [{"action": "...", "input": {...}, "reason": "..."}],')
    parts.append('   "answer": "回答文本",')
    parts.append('   "citations": ["raw路径或locator", ...]}')
    parts.append("规则:")
    parts.append("- 第一轮 discover/read 必须先提交轻量检索策略 strategy；无固定策略的弱结构题应 drafted，直达题可 skipped 并写明 reason")
    parts.append("- discover: 用 graph_search/wiki_recall 发现候选（graph_search 覆盖缩写/别名/标题三路匹配）")
    parts.append("- read: 用 read_section/read_raw 核验候选（read_raw 的 locator 会记入已读来源）")
    parts.append("- answer: 给出回答，citations 必须全部来自已读来源(raw locator)")
    parts.append("- citations 为空时标记为无引用；引用未在已读来源中时标记为未核验")
    return "\n".join(parts)


def _api_query_loop(session: QuerySession, llm_call_fn, max_rounds: int | None = None) -> dict:
    """API 查询循环核心：LLM 决策 → 程序执行 → citation contract。

    llm_call_fn(prompt) -> dict，需含 'parsed' 和 'status' 键。
    """
    if max_rounds is None:
        max_rounds = MAX_LOOP + 2
    last_results = []
    for round_num in range(1, max_rounds + 1):
        prompt = _build_api_prompt(session, last_results, round_num)
        result = llm_call_fn(prompt)
        if result.get("status") == "agent_required":
            return {"session_id": session.session_id, "handoff": result, "round": round_num}
        parsed = result.get("parsed") or {}
        decision = parsed.get("decision", "answer")
        plan = parsed.get("plan", [])
        answer = parsed.get("answer", "")
        citations = parsed.get("citations", [])
        if decision == "answer":
            session.stage = "answer"
            citation_check = _check_citations(citations, session.read_sources)
            session.finalize_stop_reason("sufficient")
            return {"session_id": session.session_id, "answer": answer,
                    "citations": citations, "citation_check": citation_check,
                    "read_sources": session.read_sources, "round": round_num,
                    "snapshot": session.snapshot()}
        if round_num == 1 and decision in ("discover", "read") and not session.search_strategy:
            strategy = parsed.get("strategy")
            if not _valid_search_strategy(strategy):
                last_results = [{"action": "search_strategy_guard",
                                 "text_preview": "首轮检索被拒：必须提交合法 drafted/skipped 轻量检索策略"}]
                continue
            session.record_strategy(strategy)
        if decision == "read" and session.stage == "start":
            session.stage = "evidence"
        elif decision == "discover" and session.stage == "evidence":
            session.stage = "continue"
        out = execute_plan(session, plan, is_continuation=(round_num > 1))
        last_results = out.get("results", [])
        if session.hard_stopped:
            break
    session.stage = "answer"
    session.finalize_stop_reason("no_actionable_candidate")
    return {"session_id": session.session_id, "status": "loop_exhausted",
            "round": max_rounds, "snapshot": session.snapshot()}


def cmd_run_api(args):
    """API 模式全自动查询循环：LLM 决策 + 程序通过 stage guard 执行 + citation contract。"""
    import llm_structured as llm
    session = QuerySession(query=args.query, query_type=args.query_type or "简单事实",
                           stage="start", mode="api")
    if args.slots and args.slots.strip():
        session.slot_checklist = [x.strip() for x in args.slots.split(",") if x.strip()]
    strategy = json.loads(args.strategy) if args.strategy else {}
    if strategy:
        if not _valid_search_strategy(strategy):
            print(json.dumps({"error": "invalid search strategy"}, ensure_ascii=False)); sys.exit(1)
        session.record_strategy(strategy)

    def llm_call_fn(prompt):
        return llm.call_json(prompt, _api_decision_schema,
                             operation="query_api_loop", max_tokens=2000, reasoning="deep")

    result = _api_query_loop(session, llm_call_fn, args.max_rounds or None)
    _save(session)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init"); p_init.add_argument("--query", required=True)
    p_init.add_argument("--query-type", default=""); p_init.add_argument("--plan", default="")
    p_init.add_argument("--strategy", default="", help="轻量检索策略 JSON")
    p_init.add_argument("--slots", default="", help="预期槽位,逗号分隔(多槽题声明,简单题可省略)")
    p_init.add_argument("--stage", default="start", help="查询阶段: start|evidence|continue|answer")
    p_init.add_argument("--mode", default="agent", help="运行模式: agent(默认,不受阶段约束)|api(DSH cockpit,强制 stage 守卫)")
    p_exec = sub.add_parser("exec"); p_exec.add_argument("--session", required=True); p_exec.add_argument("--plan", required=True)
    p_exec.add_argument("--covered", default="", help="本轮已覆盖槽位,逗号分隔(累积,可选)")
    p_exec.add_argument("--stage", default="", help="推进查询阶段: start|evidence|continue|answer(留空=不变)")
    p_fin = sub.add_parser("finalize"); p_fin.add_argument("--session", required=True); p_fin.add_argument("--decision", default="")
    p_ra = sub.add_parser("run-api"); p_ra.add_argument("--query", required=True)
    p_ra.add_argument("--query-type", default="")
    p_ra.add_argument("--slots", default="", help="预期槽位,逗号分隔")
    p_ra.add_argument("--strategy", default="", help="预置轻量检索策略 JSON;缺省由首轮 LLM 输出")
    p_ra.add_argument("--max-rounds", type=int, default=0, help="最大轮次(0=默认MAX_LOOP+2)")
    p_mk = sub.add_parser("make-plan")
    p_mk.add_argument("--steps-file", default="", help="批量:每行 action=x|page=y|section=z|reason=w (续检索加 gap=..|candidate=..)")
    p_mk.add_argument("--action", default="read_section", choices=[k for k in _ACTION_SIGS.keys() if k != "read_raw"])
    p_mk.add_argument("--page", default=""); p_mk.add_argument("--section", default=""); p_mk.add_argument("--term", default="")
    p_mk.add_argument("--node", default=""); p_mk.add_argument("--depth", default="2"); p_mk.add_argument("--predicate", default=""); p_mk.add_argument("--query", default=""); p_mk.add_argument("--domain", default=""); p_mk.add_argument("--topk", default="8")
    p_mk.add_argument("--reason", default="")
    p_mk.add_argument("--gap", default=""); p_mk.add_argument("--candidate", default="")
    p_mk.add_argument("--candidate-basis", default=""); p_mk.add_argument("--expected-gain", default="")
    args = ap.parse_args()
    {"init": cmd_init, "exec": cmd_exec, "finalize": cmd_finalize, "make-plan": cmd_make_plan, "run-api": cmd_run_api}[args.cmd](args)
