#!/usr/bin/env python3
"""recompute_tokens.py — 用 tiktoken(cl100k_base)对 trace 读取类步骤重新实计 token

读取类(read_section/read_keyword_index/read_triples/grep_section_names):
  重新执行实际读取,编码真实输出文本,得 tokens_actual
推理类(judge_type/completeness_check/answer):
  无明确文本(内部推理),保留 tokens_est,标 token_source="est(推理类)"

用法: recompute_tokens.py <trace.jsonl> [--write] [--encoding o200k_base|cl100k_base]\n  默认 o200k_base(GPT-4o/o-series 当前主流),cl100k_base 作对照
"""
import json, sys, subprocess, re, os
import tiktoken
encoding_name = "o200k_base"
for i,a in enumerate(sys.argv):
    if a == "--encoding" and i+1 < len(sys.argv):
        encoding_name = sys.argv[i+1]
enc = tiktoken.get_encoding(encoding_name)
SCRIPTS = os.path.join(os.path.dirname(__file__))

def read_section(file, section):
    r = subprocess.run(["bash", os.path.join(SCRIPTS,"read_section.sh"), file, section],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""

def get_text(step, trace_query):
    """返回该步骤实际读入上下文的文本(供编码),或 None(推理类)"""
    a = step["action"]
    inp = step.get("input", "")
    if a == "judge_type":
        # agent 输入是用户查询(trace 顶层 query 字段),非"问题类型判定"占位
        return trace_query or None
    if a == "read_section":
        if isinstance(inp, dict):
            f = inp.get("file") or inp.get("page")
            sec = inp.get("section")
            if f and sec:
                return read_section(f, sec)
        return None
    if a == "read_keyword_index":
        # output_summary 含命中的 keyword-index-*.md 文件名,整读该文件
        m = re.search(r"(keyword-index[-\w]*\.md)", step.get("output_summary",""))
        if m:
            path = os.path.join("cross-domain", m.group(1))
            if os.path.exists(path):
                return open(path, encoding="utf-8").read()
        # 兜底:读总入口
        path = "cross-domain/keyword-index.md"
        return open(path, encoding="utf-8").read() if os.path.exists(path) else None
    if a == "read_triples":
        # 旧式整读(已被 read_section/grep 替代,兼容)
        m = re.search(r"(triples[-\w]*\.md)", step.get("output_summary",""))
        f = os.path.join("cross-domain", m.group(1)) if m else "cross-domain/triples.md"
        return open(f, encoding="utf-8").read() if os.path.exists(f) else None
    if a == "grep_section_names":
        # grep '^## ' 各 triples 文件的段名列表
        files = ["triples.md","triples-rag.md","triples-memory.md","triples-people.md"]
        out = []
        for fn in files:
            p = os.path.join("cross-domain", fn)
            if os.path.exists(p):
                r = subprocess.run(["grep","^## ", p], capture_output=True, text=True)
                if r.stdout: out.append(f"# {fn}\n{r.stdout}")
        return "\n".join(out) if out else None
    return None  # completeness_check / answer 推理类

def main():
    path = sys.argv[1]
    write = "--write" in sys.argv
    lines = [json.loads(l) for l in open(path, encoding="utf-8")]
    trace_query = lines[0].get("query", "") if lines else ""
    total_est = total_actual = 0
    read_steps = reason_steps = 0
    for s in lines:
        est = s.get("tokens_est", {}).get("in", 0)
        text = get_text(s, trace_query)
        if text is not None:
            actual = len(enc.encode(text))
            s["tokens_actual"] = actual
            s["token_source"] = f"tiktoken({encoding_name})"
            total_actual += actual
            read_steps += 1
        else:
            s["tokens_actual"] = est  # 推理类沿用估算
            s["token_source"] = "est(推理类无文本)"
            total_actual += est
            reason_steps += 1
        total_est += est
    if write:
        with open(path, "w", encoding="utf-8") as f:
            for s in lines:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"{os.path.basename(path)}: 读取类{read_steps}步+推理类{reason_steps}步")
    print(f"  旧 tokens_est 合计: {total_est}")
    print(f"  新 tokens_actual 合计: {total_actual}")
    print(f"  误差: {(total_est-total_actual)/total_actual*100:+.1f}% (负=旧值低估)")

if __name__ == "__main__":
    main()
