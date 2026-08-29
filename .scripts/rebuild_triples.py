#!/usr/bin/env python3
"""rebuild_triples.py — 从各页 Core Triples 段派生重建全局 triples

> **v3(2026-07-25)**:图重建已收口到 `graph_build.py`(nodes+edges 入 SQLite 图)。
> 本脚本仍生成 markdown 派生物(triples*.md),保留向后兼容;图稳定后 markdown 派生物删除,
> 本脚本降级为图重建的薄封装或弃用。详见 `projects/code-review-graph-study/idea-deltas.md`。
> 提取逻辑(extract_core_triples)被 graph_build.py 复用。

原:从各页 Core Triples 段派生重建全局 triples

原理:页面 Core Triples 段是关系主维护位置(单一事实源),全局 triples 是派生索引。
本脚本扫描所有 wiki 内容页的 ## Core Triples 段,聚合到 cross-domain/triples*.md。

用法:
  rebuild_triples.py --scan           # 扫描所有页,输出统计(不写文件)
  rebuild_triples.py --build          # 重建全局 triples 文件(写到 .rebuilt 后缀,不覆盖原文件)
  rebuild_triples.py --diff           # diff 派生版 vs 现有手动版(发现漂移)
  rebuild_triples.py --build --apply  # 覆盖现有文件(危险,需确认)

元数据格式(行内花括号):
  主体 → 谓词 → 客体 {authority: X; temporal: Y; confidence: Z}（来源:...）
默认继承:关系不标元数据时,authority=页面 source_type, temporal=页面 status, confidence=页面 confidence
"""
import argparse
import re
import sys
from pathlib import Path

# 谓词 → 主题分桶(决定写到哪个 triples 文件)
# memory: pink/du/wu/fountas/zhang agent-memory 主题
# rag: sarthi/edge/jeong/li-graphreader/yao/jin-longrefiner/li-deepgraphrag/sun-hmem/cao/hu-zoomrag/polonuer-ark/sun-autosearch/wu-gam/karpathy
# people: 师生/合作/对标(人物关系)
# main: 其余通用学术关系
MEMORY_KEYWORDS = {"pink","du-","wu-","fountas","zhang-","em-llm","episodic","memgate","agent-memory","memory-survey"}
RAG_KEYWORDS = {"sarthi","edge","jeong","li-2024","li-2026","yao","jin-2025","sun-2026","cao","hu-2026","polonuer","wu-2026","karpathy","raptor","graphrag","adaptive-rag","graphreader","seakr","longrefiner","deep-graphrag","h-mem","higmem","zoomrag","ark","autosearch","gam"}
PEOPLE_PREDICATES = {"指导","合作者","对标","师生","合作","指导老师"}

def read_frontmatter(page_path):
    p = Path(page_path)
    if not p.exists():
        return {}
    c = p.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---", c, re.S)
    if not m:
        return {}
    try:
        import yaml
        fm = yaml.safe_load(m.group(1)) or {}
        return fm if isinstance(fm, dict) else {}
    except:
        return {}

def extract_core_triples(page_path):
    """提取页面的 ## Core Triples 段,返回 [(raw_line, subject, predicate, object, metadata, source_note)]"""
    p = Path(page_path)
    if not p.exists():
        return []
    c = p.read_text(encoding="utf-8")
    # 提取 ## Core Triples 到下一个 ## 之间
    m = re.search(r"^## Core Triples\s*\n(.*?)(?=^## |\Z)", c, re.M | re.S)
    if not m:
        return []
    block = m.group(1)
    results = []
    for line in block.split("\n"):
        line = line.strip()
        if not line.startswith("- "):
            continue
        body = line[2:]
        # 解析:主体 → 谓词 → 客体 {metadata}（来源:...）
        # 元数据块
        meta = {}
        meta_match = re.search(r"\{(.+?)\}", body)
        if meta_match:
            meta_str = meta_match.group(1)
            for kv in meta_str.split(";"):
                kv = kv.strip()
                if ":" in kv:
                    k, _, v = kv.partition(":")
                    meta[k.strip()] = v.strip()
            body = body[:meta_match.start()] + body[meta_match.end():]
        # 来源注
        source_note = ""
        src_match = re.search(r"[（(]来源[:：](.+?)[）)]", body)
        if src_match:
            source_note = src_match.group(1)
            body = body[:src_match.start()] + body[src_match.end():]
        body = body.strip()
        # 主谓客(按 → 分隔)
        parts = [x.strip() for x in body.split("→")]
        if len(parts) >= 3:
            subject = parts[0]
            predicate = parts[1]
            obj = " → ".join(parts[2:])  # 客体可能含 →
            results.append({
                "raw": line,
                "subject": subject,
                "predicate": predicate,
                "object": obj,
                "metadata": meta,
                "source_note": source_note,
                "page": str(page_path),
            })
    return results

def inherit_metadata(triple, fm):
    """默认继承:关系不标元数据时从页面 frontmatter 继承"""
    meta = dict(triple["metadata"])
    if "authority" not in meta:
        meta["authority"] = fm.get("source_type", "unknown")
    if "temporal" not in meta:
        st = fm.get("status", "unknown")
        meta["temporal"] = st
    if "confidence" not in meta:
        meta["confidence"] = fm.get("confidence", "unknown")
    return meta

def bucket_for(triple, fm):
    """决定写到哪个主题文件"""
    subj = triple["subject"].lower()
    pred = triple["predicate"]
    # people: 师生/合作/对标
    if pred in PEOPLE_PREDICATES:
        return "triples-people"
    # memory: agent-memory 主题
    if any(k in subj for k in MEMORY_KEYWORDS):
        return "triples-memory"
    # rag: RAG/检索主题
    if any(k in subj for k in RAG_KEYWORDS):
        return "triples-rag"
    return "triples"  # 主文件

def scan_all(wiki_root=".", verbose=True):
    """扫描所有 wiki 内容页"""
    all_triples = []
    pages_scanned = 0
    pages_with_triples = 0
    for p in Path(wiki_root).rglob("*.md"):
        if "/.git/" in str(p) or "/raw/" in str(p) or "/testbed/" in str(p):
            continue
        if "/wiki/" not in str(p):
            continue
        pages_scanned += 1
        fm = read_frontmatter(p)
        triples = extract_core_triples(p)
        if triples:
            pages_with_triples += 1
        for t in triples:
            t["metadata_inherited"] = inherit_metadata(t, fm)
            t["bucket"] = bucket_for(t, fm)
            all_triples.append(t)
    if verbose:
        print(f"扫描 {pages_scanned} 页,含 Core Triples 段 {pages_with_triples} 页,共 {len(all_triples)} 条关系")
        from collections import Counter
        buckets = Counter(t["bucket"] for t in all_triples)
        for b, n in sorted(buckets.items()):
            print(f"  {b}: {n} 条")
    return all_triples

def build_global_triples(all_triples):
    """按谓词分组,生成全局 triples 内容(返回 {filename: content})
    
    头部保留策略:保留现有文件第一个 ## 之前的头部(用途/维护规则/对标说明等手动维护内容),
    只重建 ## 关系段。新建文件(无现有版)用最小默认头。"""
    from collections import defaultdict
    by_bucket_pred = defaultdict(lambda: defaultdict(list))
    for t in all_triples:
        by_bucket_pred[t["bucket"]][t["predicate"]].append(t)
    
    files = {}
    for bucket, preds in by_bucket_pred.items():
        manual_path = Path("cross-domain") / f"{bucket}.md"
        if manual_path.exists():
            existing = manual_path.read_text(encoding="utf-8")
            m = re.search(r"^## ", existing, re.M)
            header = existing[:m.start()] if m else existing.rstrip() + "\n\n"
        else:
            tail = bucket.split("-")[-1] if "-" in bucket else ""
            label = {'people': '人物', 'rag': 'RAG', 'memory': 'agent记忆'}.get(tail, '主')
            header = f"# 知识三元组索引 — {label}\n\n> 派生重建自各页 Core Triples 段(rebuild_triples.py 生成)\n\n"
        lines = [header.rstrip() + "\n"]
        for pred in sorted(preds.keys()):
            lines.append(f"\n## {pred}\n")
            for t in sorted(preds[pred], key=lambda x: x["subject"]):
                # 来源规范化:本文/本文 frontmatter/空 → 派生当前页 wikilink
                src_note = t["source_note"].strip()
                if not src_note or src_note.startswith("本文"):
                    m_page = re.search(r"/wiki/(.+?)\.md$", str(t["page"]))
                    src = f"[[{m_page.group(1)}]]" if m_page else str(t["page"])
                else:
                    src = src_note
                # [SR] 标记从 authority 派生
                sr = " [SR]" if t["metadata_inherited"].get("authority") == "speech-recognition" else ""
                lines.append(f"- {t['subject']} → {pred} → {t['object']}（来源：{src}）{sr}")
        files[f"{bucket}.md"] = "\n".join(lines) + "\n"
    return files

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--diff", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    
    if not (args.scan or args.build or args.diff):
        ap.print_help()
        return
    
    all_triples = scan_all(verbose=True)
    
    if args.scan:
        return
    
    files = build_global_triples(all_triples)
    
    for fname, content in files.items():
        out_path = Path("cross-domain") / fname.replace(".md", ".rebuilt.md")
        out_path.write_text(content, encoding="utf-8")
        print(f"派生版写入: {out_path}")
    
    if args.diff:
        print("\n=== diff 派生版 vs 现有手动版 ===")
        for fname in files:
            manual = Path("cross-domain") / fname
            rebuilt = Path("cross-domain") / fname.replace(".md", ".rebuilt.md")
            if not manual.exists():
                print(f"{fname}: 手动版不存在(新文件)")
                continue
            import subprocess
            r = subprocess.run(["diff", str(manual), str(rebuilt)], capture_output=True, text=True)
            if r.returncode == 0:
                print(f"{fname}: ✅ 一致")
            else:
                # 统计差异行数
                diff_lines = [l for l in r.stdout.split("\n") if l.startswith(">") or l.startswith("<")]
                print(f"{fname}: ⚠ {len(diff_lines)} 行差异(头部保留,仅关系段差异)")
                print(r.stdout[:500])

    if args.build and args.apply:
        print("\n=== --apply 覆盖现有文件 ===")
        for fname, content in files.items():
            target = Path("cross-domain") / fname
            target.write_text(content, encoding="utf-8")
            print(f"  ✅ {target} 已覆盖(头部保留+关系段重建)")
        for f in Path("cross-domain").glob("*.rebuilt.md"):
            f.unlink()
            print(f"  🧹 清理临时文件 {f}")

if __name__ == "__main__":
    main()
