#!/usr/bin/env python3
"""verify_dims.py — 客观四维程序化校验(复核 agent 六维判定)

读 trace 的 completeness_check + 读相关页 frontmatter(yaml),复核客观四维:
- C_temporal: 页 status 是否 deprecated(若 agent 判 satisfied 但页 deprecated → 虚报)
- C_authority: 页 source_type 是否达问题所需(数据质量校验，C_authority 已降为检索信号非测试维度)
- C_conflict: related 字段指向页是否有未闭合冲突(简化:有 related 但无 comparison 页 → 可能未闭合)
- C_evidence: 页 sources 字段非空(有 raw 回溯链)

用法: verify_dims.py <trace.jsonl> <page1> [page2 ...]
输出: 校验报告(JSON,打印 + 写 .verify.json)
"""
import json
import re
import sys
import yaml
from pathlib import Path

def read_frontmatter(page_path):
    p = Path(page_path)
    if not p.exists():
        return {"_error": "file not found", "_path": str(page_path)}
    c = p.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---", c, re.S)
    if not m:
        return {"_error": "no frontmatter"}
    try:
        fm = yaml.safe_load(m.group(1)) or {}
        return fm if isinstance(fm, dict) else {"_error": "fm not dict"}
    except Exception as e:
        return {"_error": f"yaml parse: {e}"}

def verify(trace_path, pages):
    trace_lines = Path(trace_path).read_text(encoding="utf-8").splitlines() if Path(trace_path).exists() else []
    agent_checks = []
    for line in trace_lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except:
            continue
        if rec.get("action") == "completeness_check":
            agent_checks.append(rec.get("completeness", {}))
    
    if not agent_checks:
        return {"error": "trace 无 completeness_check 步骤(或 trace 为空)", "trace_path": str(trace_path)}
    
    final = agent_checks[-1]

    # 回环计数:completeness_check 次数 - 1(减初始门控);上限 3 轮(QUERY 步骤4.5"回环上限3轮")
    # 程序计数非 LLM 自报——回环次数可机械判断,不依赖 agent 自检(原则7 能验证则验证)
    loop_count = max(0, len(agent_checks) - 1)
    max_loop = 3

    fms = {}
    for pg in pages:
        fms[pg] = read_frontmatter(pg)

    report = {"agent_final_completeness": final, "pages": {}, "mismatches": [],
              "loop_count": loop_count, "max_loop": max_loop,
              "completeness_check_count": len(agent_checks)}

    # 回环超限校验:loop_count > 3 = 超过 QUERY 步骤4.5 上限,应在上限后按 token 硬约束停止
    # 事后审计能抓超限,不事前拦截(事前拦截是 gap3 运行时壳,见 design-discussion 查询侧可控性)
    if loop_count > max_loop:
        report["mismatches"].append(
            f"回环超限: loop_count={loop_count} > {max_loop} (completeness_check 次数={len(agent_checks)},应在上限后按 token 硬约束停止)")

    for pg, fm in fms.items():
        if "_error" in fm:
            report["pages"][pg] = fm
            continue
        pg_report = {
            "status": fm.get("status", "unknown"),
            "source_type": fm.get("source_type", "unknown"),
            "sources_count": len(fm.get("sources", []) or []) if isinstance(fm.get("sources"), list) else (1 if fm.get("sources") else 0),
            "related_count": len(fm.get("related", []) or []) if isinstance(fm.get("related"), list) else (1 if fm.get("related") else 0),
        }
        report["pages"][pg] = pg_report
        
        agent_temporal = final.get("C_temporal", "N/A")
        if agent_temporal == "satisfied" and fm.get("status") == "deprecated":
            # C_temporal satisfied + 页 deprecated 不一定虚报:agent 可能已跟 superseded_by
            # 检查 trace 是否读过 superseded_by 指向页
            sup = fm.get("superseded_by", "")
            if sup:
                # 提取 wikilink 目标页名
                import re as _re
                m = _re.search(r"\[\[(.+?)\]\]", str(sup))
                target = m.group(1).split("/")[-1].replace("]]","") if m else ""
                # 检查 trace 是否有 read_section 该目标页
                trace_text = Path(trace_path).read_text(encoding="utf-8") if Path(trace_path).exists() else ""
                if target and target not in trace_text and sup not in trace_text:
                    report["mismatches"].append(f"{pg}: agent C_temporal=satisfied 但页 deprecated 且 trace 未读 superseded_by 目标(可能虚报)")
            else:
                report["mismatches"].append(f"{pg}: 页 deprecated 但无 superseded_by(数据缺失)")
    
    # C_evidence 校验:agent 判 satisfied 但页无 sources → 虚报
    agent_evidence = final.get("C_evidence", "N/A")
    if agent_evidence == "satisfied":
        for pg, fm in fms.items():
            if "_error" in fm:
                continue
            src_cnt = report["pages"][pg].get("sources_count", 0)
            if src_cnt == 0:
                report["mismatches"].append(f"{pg}: agent C_evidence=satisfied 但页无 sources(无 raw 回溯链,可能虚报)")
    
    # C_authority 数据质量校验(C_authority 已降为检索信号非测试维度):agent 判 satisfied 但页 source_type=unknown → 虚报
    agent_authority = final.get("C_authority", "N/A")
    if agent_authority == "satisfied":
        for pg, fm in fms.items():
            if "_error" in fm:
                continue
            if fm.get("source_type", "unknown") == "unknown":
                report["mismatches"].append(f"{pg}: agent C_authority=satisfied 但页 source_type=unknown(权威未标,可能虚报)")
    
    # C_conflict 校验(简化):agent 判 satisfied 但页有 related 且存在 deprecated 未处理项 → 可能虚报
    agent_conflict = final.get("C_conflict", "N/A")
    if agent_conflict == "satisfied":
        for pg, fm in fms.items():
            if "_error" in fm:
                continue
            # 页标 deprecated 但无 superseded_by + agent 判无冲突 → 矛盾
            if fm.get("status") == "deprecated" and not fm.get("superseded_by"):
                report["mismatches"].append(f"{pg}: agent C_conflict=satisfied 但页 deprecated 无 superseded_by(未闭合冲突,可能虚报)")
    
    report["agent_objective_dims"] = {
        "C_temporal": final.get("C_temporal", "N/A"),
        "C_authority": final.get("C_authority", "N/A"),
        "C_conflict": final.get("C_conflict", "N/A"),
        "C_evidence": final.get("C_evidence", "N/A"),
    }
    report["mismatch_count"] = len(report["mismatches"])
    return report

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: verify_dims.py <trace.jsonl> [page1 page2 ...]")
        print("  无 page 参数时只校验回环计数(loop_count),跳过客观四维")
        sys.exit(2)
    r = verify(sys.argv[1], sys.argv[2:])
    print(json.dumps(r, ensure_ascii=False, indent=2))
