#!/usr/bin/env python3
"""metrics.py — 第一阶段实验评估脚本（机械计算，所有基线共用）

v2(2026-07-23): 加跨方法交叉表（by_dim × by_method），输出正确率/姿态/可追溯/token 四张表。
  这是"双面成本叙事"的数据出口——分维度看各方法差距，而非一个总分糊在一起。

两种模式:
  旧(单方法检索指标): python3 metrics.py <results.json> <queries.json>
  新(交叉表):         python3 metrics.py --cross <pilot.json> <B1-judged.json> [<B2-judged.json> ...]
                     也可用 --dirs <目录> 自动找所有 *-judged*.json

四层评价（交叉表模式）:
  答案层: 正确率、姿态正确率
  证据层: 可追溯率
  导航层: stop_reason 分布(sufficient_complete/sufficient_partial/no_actionable_candidate)
  成本层: 平均 token、平均轮次
"""
import json, sys, argparse, glob
from pathlib import Path
from collections import defaultdict

def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))

def extract_gold_pages(gold_evs):
    return set(ev.split(" ## ")[0].split(" (")[0].strip() for ev in gold_evs)

def count_hits(gold_pages, retrieved_pages):
    return sum(1 for gp in gold_pages if any(gp in rp or rp in gp for rp in retrieved_pages))

def hit_level(hits, gold_count):
    if hits == gold_count: return "hit"
    elif hits > 0: return "partial"
    else: return "miss"

# ============ 旧模式:单方法检索指标(向后兼容) ============
def evaluate(results, queries):
    q_map = {q["id"]: q for q in queries}
    n = len(results)
    cit_p, cit_r = [], []
    tokens, loops = [], []
    no_answer_correct, no_answer_total = 0, 0
    by_dim = defaultdict(lambda: {"hit":0,"partial":0,"miss":0,"total":0,"tokens":[]})
    hit_count, partial_count, miss_count = 0, 0, 0
    for r in results:
        q = q_map.get(r["id"])
        if not q: continue
        gold = q["gold"]
        dim = q["primary_dim"].split("+")[0]
        retrieved_pages = [c["page"] for c in r.get("retrieved", [])]
        retrieved_set = set(retrieved_pages)
        tok = r.get("retrieval_tokens", r.get("tokens", 0))
        gold_pages = extract_gold_pages(gold["evidence_set"])
        hits = count_hits(gold_pages, retrieved_pages)
        level = hit_level(hits, len(gold_pages))
        if level == "hit": hit_count += 1
        elif level == "partial": partial_count += 1
        else: miss_count += 1
        by_dim[dim]["total"] += 1
        by_dim[dim][level] += 1
        by_dim[dim]["tokens"].append(tok)
        if retrieved_set:
            cit_p.append(hits / len(retrieved_set))
        if gold_pages:
            cit_r.append(hits / len(gold_pages))
        if "证据不足" in gold.get("answer", "") or gold.get("posture","").startswith("限定"):
            no_answer_total += 1
            sys_ans = r.get("answer", "")
            sys_pos = r.get("posture", "")
            if sys_pos.startswith("限定") or "证据不足" in sys_ans or "未找到" in sys_ans:
                no_answer_correct += 1
        tokens.append(tok)
        if "loops" in r: loops.append(r["loops"])
    summary = {
        "n_questions": n,
        "citation_precision": round(sum(cit_p)/max(len(cit_p),1), 4),
        "citation_recall": round(sum(cit_r)/max(len(cit_r),1), 4),
        "page_hit_rate": round(hit_count/n, 4) if n else 0,
        "page_partial_rate": round(partial_count/n, 4) if n else 0,
        "page_miss_rate": round(miss_count/n, 4) if n else 0,
        "avg_tokens": round(sum(tokens)/max(len(tokens),1)),
        "no_answer_correct": f"{no_answer_correct}/{no_answer_total}",
        "by_dimension": {d: {"hit":s["hit"],"partial":s["partial"],"miss":s["miss"],"total":s["total"],
                             "avg_tokens": round(sum(s["tokens"])/max(len(s["tokens"]),1))}
                         for d,s in sorted(by_dim.items())},
    }
    if loops:
        summary["avg_loops"] = round(sum(loops)/len(loops), 2)
    return summary

# ============ 新模式:跨方法交叉表 ============
def load_judged(path):
    """加载 judged JSON,返回 {id: row}。"""
    data = load(path)
    if isinstance(data, dict) and "results" in data:
        data = data["results"]
    return {r["id"]: r for r in data}

def method_label(row, fallback):
    """从 judged 行提取方法标签;无则用文件名。"""
    m = row.get("method", "") if row else ""
    m_lower = m.lower()
    if m_lower.startswith("b1") or "bm25" in m_lower: return "B1"
    if m_lower.startswith("b2") or "vector" in m_lower or "embed" in m_lower: return "B2"
    if m_lower.startswith("b3") or "hierarch" in m_lower or "navigation" in m_lower and "real" not in m_lower: return "B3"
    if m_lower.startswith("b4") or "fixed" in m_lower: return "B4"
    if m_lower.startswith("b5") or "real" in m_lower or "nav" in m_lower: return "B5"
    return m or fallback

def cross_evaluate(pilot_path, judged_paths):
    """交叉表: primary_dim × method → 正确率/姿态/可追溯/token/轮次/stop_reason。"""
    pilot = load(pilot_path)
    meta = {q["id"]: q for q in pilot}
    dims = sorted(set(q["primary_dim"].split("+")[0] for q in pilot))

    # 加载各方法
    methods = {}
    used_labels = set()
    for jp in judged_paths:
        rows = load_judged(jp)
        if not rows: continue
        any_row = next(iter(rows.values()))
        base = method_label(any_row, Path(jp).stem)
        # 同标签冲突时用文件名(如 B5-v9 / B5-v10)
        label = base
        if label in used_labels:
            label = Path(jp).stem.replace("-judged", "").replace("-nav", "")
        used_labels.add(label)
        methods[label] = rows

    method_names = sorted(methods.keys())
    if not method_names:
        return {"error": "no judged data loaded"}

    # 聚合: (dim, method) → 指标
    agg = defaultdict(lambda: {"correct":0,"posture":0,"traceable":0,"tokens":[],"loops":[],"n":0,
                                "stop": defaultdict(int)})
    method_totals = {m: {"correct":0,"posture":0,"traceable":0,"tokens":[],"loops":[],"n":0,
                          "stop": defaultdict(int)} for m in method_names}

    for mid, rows in methods.items():
        for qid, r in rows.items():
            q = meta.get(qid, {})
            dim = q.get("primary_dim", r.get("primary_dim","?")).split("+")[0]
            s = r.get("scores", {})
            correct = s.get("correctness", 0)
            posture = s.get("posture_correct", 0)
            trace = s.get("evidence_traceable", 0)
            tok = r.get("retrieval_tokens", r.get("tokens", 0))
            loops = r.get("loops", 0)
            stop = r.get("stop_reason", "unknown")

            cell = agg[(dim, mid)]
            cell["correct"] += correct; cell["posture"] += posture; cell["traceable"] += trace
            cell["tokens"].append(tok); cell["loops"].append(loops); cell["n"] += 1
            cell["stop"][stop] += 1

            mt = method_totals[mid]
            mt["correct"] += correct; mt["posture"] += posture; mt["traceable"] += trace
            mt["tokens"].append(tok); mt["loops"].append(loops); mt["n"] += 1
            mt["stop"][stop] += 1

    def pct(val, n):
        return round(100*val/n, 0) if n else 0
    def avg(lst):
        return round(sum(lst)/len(lst)) if lst else 0

    # 构建 4 张表 + stop_reason 分布
    tables = {}
    metric_defs = [
        ("correctness", "正确率(%)", lambda c: pct(c["correct"], c["n"])),
        ("posture", "姿态正确率(%)", lambda c: pct(c["posture"], c["n"])),
        ("traceable", "可追溯率(%)", lambda c: pct(c["traceable"], c["n"])),
        ("tokens", "平均 token", lambda c: avg(c["tokens"])),
        ("loops", "平均轮次", lambda c: round(sum(c["loops"])/len(c["loops"]),2) if c["loops"] else 0),
    ]
    for key, title, fn in metric_defs:
        rows_out = []
        for dim in dims:
            row = {"dim": dim}
            for m in method_names:
                row[m] = fn(agg[(dim, m)]) if agg[(dim,m)]["n"] else "-"
            rows_out.append(row)
        # overall 行
        overall = {"dim": "ALL"}
        for m in method_names:
            overall[m] = fn(method_totals[m]) if method_totals[m]["n"] else "-"
        rows_out.append(overall)
        tables[key] = {"title": title, "rows": rows_out}

    # stop_reason 分布(每方法)
    stop_dist = {}
    for m in method_names:
        stops = method_totals[m]["stop"]
        n = method_totals[m]["n"] or 1
        stop_dist[m] = {k: f"{v}({round(100*v/n)}%)" for k,v in sorted(stops.items())}

    return {"methods": method_names, "dims": dims, "tables": tables,
            "stop_reason_dist": stop_dist, "n_questions": len(pilot)}

def render_cross(result):
    """渲染为 markdown 表格。"""
    lines = []
    methods = result["methods"]
    lines.append(f"## 交叉表（{result['n_questions']} 题 × {len(methods)} 方法）\n")
    for key in ["correctness","posture","traceable","tokens","loops"]:
        t = result["tables"][key]
        lines.append(f"### {t['title']}\n")
        header = "| 维度 | " + " | ".join(methods) + " |"
        sep = "|---|" + "|".join(["---"]*len(methods)) + "|"
        lines.append(header); lines.append(sep)
        for row in t["rows"]:
            cells = [str(row[m]) for m in methods]
            lines.append(f"| {row['dim']} | " + " | ".join(cells) + " |")
        lines.append("")
    lines.append("### stop_reason 分布\n")
    lines.append("| 方法 | " + " | ".join(methods) + " |")
    lines.append("|---|" + "|".join(["---"]*len(methods)) + "|")
    all_stops = sorted(set(s for m in methods for s in result["stop_reason_dist"][m]))
    for stop in all_stops:
        cells = [result["stop_reason_dist"][m].get(stop, "0(0%)") for m in methods]
        lines.append(f"| {stop} | " + " | ".join(cells) + " |")
    return "\n".join(lines)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cross", action="store_true", help="交叉表模式: 多方法 by_dim×by_method")
    ap.add_argument("--dirs", default="", help="目录,自动找 *-judged*.json")
    ap.add_argument("args", nargs="*", help="旧模式: <results.json> <queries.json> | 交叉模式: <pilot.json> <judged...>")
    args = ap.parse_args()

    if args.cross or args.dirs:
        paths = list(args.args)
        if args.dirs:
            judged = sorted(glob.glob(str(Path(args.dirs) / "*-judged*.json")))
            pilot = paths[0] if paths else None
        else:
            if len(paths) < 2:
                print("用法: metrics.py --cross <pilot.json> <B1-judged.json> [<B2-judged.json> ...]", file=sys.stderr)
                sys.exit(1)
            pilot = paths[0]
            judged = paths[1:]
        if not pilot or not judged:
            print("需要 pilot + 至少一个 judged 文件", file=sys.stderr); sys.exit(1)
        result = cross_evaluate(pilot, judged)
        print(render_cross(result))
    else:
        if len(args.args) < 2:
            print("用法: metrics.py <results.json> <queries.json>", file=sys.stderr); sys.exit(1)
        print(json.dumps(evaluate(load(args.args[0]), load(args.args[1])), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
