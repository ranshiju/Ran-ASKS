#!/usr/bin/env python3
"""check_trace_rules.py — trace 规则遵循度自动化校验(QUERY.md 规则机械检查)

读 trace.jsonl,机械校验 agent 是否遵循 QUERY.md 执行规则(不判语义)。
与 verify_dims.py 互补:verify_dims 校客观四维 vs 页面字段;本脚本校执行过程合规性。

用法: check_trace_rules.py <trace.jsonl> [page1 page2 ...]
输出: JSON 报告(rules 检查项 + violations)
"""
import json, re, sys, yaml
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SIX = {"C_temporal","C_authority","C_conflict","C_evidence","C_consistency","C_slot"}
MAX_LOOP = 3

def load_trace(p):
    recs = []
    for line in Path(p).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line: continue
        try: recs.append(json.loads(line))
        except: pass
    return recs

def action_key(rec):
    a = rec.get("action","")
    inp = rec.get("input","")
    if isinstance(inp, dict):
        inp = json.dumps(inp, sort_keys=True, ensure_ascii=False)
    return (a, str(inp))

def read_fm(page_path):
    p = REPO / page_path
    if not p.exists(): return {}
    c = p.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---", c, re.S)
    if not m: return {}
    try:
        fm = yaml.safe_load(m.group(1)) or {}
        return fm if isinstance(fm, dict) else {}
    except: return {}

def check(trace_path, pages):
    recs = load_trace(trace_path)
    violations = []
    rules = []
    if not recs:
        return {"error": "trace 为空", "violations": []}

    actions = [r.get("action","") for r in recs]
    keys = [action_key(r) for r in recs]

    # R1 当前生产路径须先路由或从图搜索开始；旧索引动作不再合规
    if actions and actions[0] not in ("route", "profile", "judge_type", "graph_search"):
        violations.append("R1: trace 起点非 route/profile/graph_search(实际 {})".format(actions[0]))
    rules.append("R1 当前路由或图搜索起点")

    # R2 不能 step1 直接 answer(须有检索+门控,除非前置路由命中且无 answer-only)
    answer_idx = next((i for i,a in enumerate(actions) if a=="answer"), None)
    if answer_idx is not None and answer_idx <= 1:
        violations.append("R2: answer 出现过早(step {}),未经检索+门控".format(answer_idx+1))
    rules.append("R2 answer 前有检索+门控")

    # R3 生产检索顺序：graph_search → graph relation/hub → wiki section
    legacy = {"read_keyword_index", "read_triples", "grep_section_names"}
    for i, action in enumerate(actions):
        if action in legacy:
            violations.append(f"R3: 使用已废弃检索动作 {action}(step {i + 1})")
    graph_search_idx = next((i for i,a in enumerate(actions) if a == "graph_search"), None)
    graph_follow_idx = next((i for i,a in enumerate(actions) if a in {"graph_neighbors", "graph_relations", "graph_hub_of"}), None)
    if graph_follow_idx is not None and graph_search_idx is None:
        violations.append("R3: graph relation/hub 查询前缺少 graph_search")
    if graph_follow_idx is not None and graph_search_idx is not None and graph_follow_idx < graph_search_idx:
        violations.append("R3: graph relation/hub 查询早于 graph_search")
    rules.append("R3 图搜索优先于关系导航")

    # R4 检索 action 重复拦截:检索动作(read_section/cat_page/read_triples/rg)同 (action,input) ≥2 次=空转
    # completeness_check 同输入重判是回环正常机制(每轮重判),不报;judge_type 不报
    RETRIEVE = {"read_section", "graph_search", "graph_neighbors", "graph_relations", "graph_hub_of", "cat_page", "rg"}
    seen = {}
    for i,r in enumerate(recs):
        k = keys[i]
        if r.get("action") not in RETRIEVE:
            continue
        if k in seen:
            violations.append("R4: 检索 action 重复(step {} 与 step {} 同为 {});回环空转".format(i+1,seen[k]+1,k))
        else:
            seen[k] = i
    rules.append("R4 检索 action 不重复")

    # R5 回环上限:loop_count = #completeness_check - 1 ≤ 3
    checks = [r for r in recs if r.get("action")=="completeness_check"]
    loop_count = max(0, len(checks)-1)
    if loop_count > MAX_LOOP:
        violations.append("R5: loop_count={} 超上限({});应按 token 硬约束停止".format(loop_count,MAX_LOOP))
    rules.append("R5 回环≤3")

    # R6 门控闭环:answer 前最后一个 completeness_check 全 satisfied,或 decision 标停止/限定
    if answer_idx is not None and checks:
        last_check = checks[-1]
        last_comp = last_check.get("completeness",{})
        unsat = [d for d,v in last_comp.items() if v=="unsatisfied"]
        last_dec = last_check.get("decision","")
        if unsat and "回答" in last_dec and not any(w in last_dec for w in ("停","限","标注","差异","缺口","待确认")):
            violations.append("R6: answer 前门控有未满足维度 {} 但 decision='{}'(未诚实标注缺口)".format(unsat,last_dec))
    rules.append("R6 门控闭环(缺口须显式)")

    # R7 图检索无命中时不能直接回答
    for i,r in enumerate(recs):
        if r.get("action") in {"graph_search", "graph_neighbors", "graph_relations", "graph_hub_of"}:
            summ = str(r.get("output_summary",""))
            no_hit = any(w in summ for w in ("未命中", "0 候选", "无匹配", "未找到", "无结果", '"count": 0', '"hubs": []'))
            if no_hit:
                if answer_idx is not None and answer_idx > i:
                    post = actions[i+1:answer_idx]
                    if not any(a in post for a in ("graph_search", "read_section", "rg", "cat_page")):
                        violations.append(f"R7: 图检索无命中(step {i + 1})后直接 answer,未换路径或诚实停止")
    rules.append("R7 检索失败须显式(不伪造)")

    # R10 图命中后必须下钻 wiki；图只是定位器，不是事实证据
    for i, r in enumerate(recs):
        if r.get("action") not in {"graph_search", "graph_neighbors", "graph_relations", "graph_hub_of"}:
            continue
        summary = str(r.get("output_summary", ""))
        if any(w in summary for w in ('"count": 0', '"hubs": []', "无命中", "无结果")):
            continue
        if answer_idx is not None and answer_idx > i:
            if not any(a == "read_section" for a in actions[i + 1:answer_idx]):
                violations.append(f"R10: 图命中(step {i + 1})后未读取 wiki section 就回答")
    rules.append("R10 图命中后下钻 wiki")

    # R11 关系/列举类查询至少执行一次关系枚举
    query_type = " ".join(str(r.get("query_type", "")) for r in recs[:2])
    if any(w in query_type for w in ("relation", "enumeration", "关系", "列举")):
        if not any(a in {"graph_neighbors", "graph_relations", "graph_hub_of"} for a in actions):
            violations.append("R11: 关系/列举 profile 未执行 graph relation/hub 导航")
    rules.append("R11 关系与列举使用图导航")

    # R8 token 预算:累计 tokens_est.in 合理(< 50000 软上限;超 budget_warned 应有提示)
    total_in = sum(r.get("tokens_est",{}).get("in",0) for r in recs)
    if total_in > 50000:
        violations.append("R8: 累计 input token={} 过高(>50000),应触发预算降级".format(total_in))
    rules.append("R8 token 预算合理")

    # R9 deprecated 过滤:读 deprecated 页时,trace 全局须有标注或跟 superseded_by(任一步标注即合规)
    all_text = " ".join(str(r.get("output_summary",""))+str(r.get("decision","")) for r in recs)
    dep_marked = any(w in all_text for w in ("deprecated","superseded","过时","跳转","已废弃","旧版"))
    for r in recs:
        if r.get("action") in ("read_section","cat_page"):
            inp = r.get("input","")
            pg = inp.get("page","") if isinstance(inp,dict) else str(inp)
            if pg and pg.endswith(".md"):
                fm = read_fm(pg)
                if fm.get("status")=="deprecated" and not dep_marked:
                    violations.append("R9: 读 deprecated 页 {} 但 trace 全局未标注/未跟 superseded_by".format(pg))
                    break
    rules.append("R9 deprecated 过滤标注")

    return {
        "trace": str(trace_path),
        "loop_count": loop_count,
        "total_tokens_in": total_in,
        "step_count": len(recs),
        "rules_checked": rules,
        "violations": violations,
        "violation_count": len(violations),
    }

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: check_trace_rules.py <trace.jsonl> [page1 page2 ...]")
        sys.exit(2)
    r = check(sys.argv[1], sys.argv[2:])
    print(json.dumps(r, ensure_ascii=False, indent=2))
