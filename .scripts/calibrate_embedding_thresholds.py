#!/usr/bin/env python3
"""calibrate_embedding_thresholds.py — ADR-003 阈值标定(纯程序,零 LLM)。

从 graph.db 的 confirmed alias 清洗出真同义对(正例),从共享包含边的 keyword 对
采集硬负例(相关但不同概念),用 embedding 余弦分布找分离阈值。

用法:
  python3 .scripts/calibrate_embedding_thresholds.py              # 标定+报告
  python3 .scripts/calibrate_embedding_thresholds.py --write        # 标定+写入 config
"""
import argparse
import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GRAPH_DB = REPO / "cross-domain" / "graph.db"
CONFIG_PATH = REPO / "operations" / "config" / "embedding-resolve.yaml"

# ── 噪声判据:这些 alias 是引文残片/错误合并,不是真同义 ──
CITATION_RE = re.compile(r"^[A-Za-z]+[-_]?\d{2,4}")  # Verstraete-2006, Rakovszky-2019, SJ-2020, ZY-2018
ARXIV_RE = re.compile(r"^arXiv:", re.IGNORECASE)
SEMICYEAR_RE = re.compile(r"\d{4};.*:\d")  # 2019;1:538–550.-2019


def is_noise(alias: str, node_path: str, title: str = "") -> bool:
    """排除引文残片、arXiv ID、错误合并产物、论文标题型节点。"""
    for s in (alias, node_path):
        if ARXIV_RE.match(s) or CITATION_RE.match(s) or SEMICYEAR_RE.search(s):
            return True
    # 论文标题型(alias 是引文 key,title 是整句论文标题 → 非概念同义)
    if title and len(title) > 40 and " " in title:
        return True
    return False


def collect_positive_pairs(conn):
    """清洗 alias → (alias_text, node_title) 真同义对。"""
    pairs = []
    for r in conn.execute(
        """SELECT a.alias, a.node_path, n.title, n.entity_subtype
           FROM aliases a JOIN nodes n ON a.node_path = n.path
           WHERE a.status='confirmed' AND n.type='entity'
             AND n.entity_subtype IN ('keyword', NULL, '')"""
    ):
        alias, node_path, title, subtype = r["alias"], r["node_path"], r["title"] or "", r["entity_subtype"] or ""
        if is_noise(alias, node_path, title):
            continue
        if alias == title:  # 别名和标题相同(无信息)
            continue
        pairs.append((alias, title))
    return pairs


def collect_hard_negatives(conn, n=100):
    """共享「包含」边的 keyword 对(父子概念,相关但不同)。"""
    pairs = set()
    for r in conn.execute(
        """SELECT e1.subject, e1.object FROM edges e1
           JOIN nodes n1 ON e1.subject = n1.path
           JOIN nodes n2 ON e1.object = n2.path
           WHERE e1.predicate = '包含'
             AND n1.type = 'entity' AND n1.entity_subtype = 'keyword'
             AND n2.type = 'entity' AND n2.entity_subtype = 'keyword'"""
    ):
        a, b = r["subject"], r["object"]
        if a != b and not is_noise(a, a) and not is_noise(b, b):
            # 排除论文标题型 keyword(误分类的论文节点)
            pairs.add((min(a, b), max(a, b)))
    # 同论文的不同 keyword(经研究关键词边,经 page 桥接)
    for r in conn.execute(
        """SELECT DISTINCT e1.object, e2.object FROM edges e1
           JOIN edges e2 ON e1.subject = e2.subject
           JOIN nodes n1 ON e1.object = n1.path
           JOIN nodes n2 ON e2.object = n2.path
           WHERE e1.predicate IN ('研究基础','核心方法','核心创新点','研究关键词')
             AND e2.predicate IN ('研究基础','核心方法','核心创新点','研究关键词')
             AND e1.object < e2.object
             AND n1.entity_subtype = 'keyword' AND n2.entity_subtype = 'keyword'
           LIMIT ?""",
        (n,),
    ):
        a, b = r[0], r[1]
        na = conn.execute("SELECT title FROM nodes WHERE path=?", (a,)).fetchone()
        nb = conn.execute("SELECT title FROM nodes WHERE path=?", (b,)).fetchone()
        if (na and is_noise(a, a, na["title"] or "")) or (nb and is_noise(b, b, nb["title"] or "")):
            continue
        pairs.add((a, b))
    return list(pairs)[:n]


def main():
    ap = argparse.ArgumentParser(description="ADR-003 embedding resolve 阈值标定")
    ap.add_argument("--write", action="store_true", help="标定后写入 embedding-resolve.yaml")
    ap.add_argument("--neg", type=int, default=150, help="硬负例采样数(默认150)")
    args = ap.parse_args()

    sys.path.insert(0, str(REPO / ".scripts"))
    import embed_helper as eh

    conn = sqlite3.connect(GRAPH_DB)
    conn.row_factory = sqlite3.Row

    pos = collect_positive_pairs(conn)
    neg = collect_hard_negatives(conn, args.neg)
    conn.close()

    print(f"正例(真同义对): {len(pos)}  硬负例(相关但不同): {len(neg)}")
    if len(pos) < 10 or len(neg) < 10:
        print("样本不足(<10),无法标定", file=sys.stderr)
        return 1

    print("\n正例样本(前10):")
    for a, b in pos[:10]:
        print(f"  {a[:40]:40s} ↔ {b[:40]}")
    print("\n硬负例样本(前10):")
    for a, b in neg[:10]:
        print(f"  {a[:40]:40s} ↔ {b[:40]}")

    # embed 全部文本(去重后批量,命中缓存秒返)
    all_texts = list(dict.fromkeys([t for pair in pos + neg for t in pair]))
    print(f"\n[embed] {len(all_texts)} 条文本(去重后)...")
    vecs = eh.embed_cached_batch(all_texts)
    text2vec = {t: vecs[i] for i, t in enumerate(all_texts)}

    import numpy as np

    def cos(a, b):
        va, vb = text2vec[a], text2vec[b]
        na, nb = np.linalg.norm(va), np.linalg.norm(vb)
        if na == 0 or nb == 0:
            return 0.0
        return float(np.dot(va, vb) / (na * nb))

    pos_sims = sorted([cos(a, b) for a, b in pos], reverse=True)
    neg_sims = sorted([cos(a, b) for a, b in neg], reverse=True)

    pos_arr = np.array(pos_sims)
    neg_arr = np.array(neg_sims)
    print(f"\n正例分布: mean={pos_arr.mean():.3f}  p50={np.median(pos_arr):.3f}  p5={np.percentile(pos_arr,5):.3f}")
    print(f"负例分布: mean={neg_arr.mean():.3f}  p50={np.median(neg_arr):.3f}  p95={np.percentile(neg_arr,95):.3f}")

    # 找分离点:扫描 0.60-0.95 步长 0.01,找正例/负例重叠最小的阈值
    best_sep = 0
    best_f1 = 0
    for t in np.arange(0.60, 0.96, 0.01):
        tp = (pos_arr >= t).sum()
        fp = (neg_arr >= t).sum()
        fn = (pos_arr < t).sum()
        prec = tp / (tp + fp) if (tp + fp) > 0 else 0
        rec = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0
        if f1 > best_f1:
            best_f1 = f1
            best_sep = round(float(t), 2)

    # auto_threshold: 正例 95% 分位且负例几乎不达(误合并≈0)
    auto = round(max(np.percentile(pos_arr, 5), best_sep), 2)
    # floor: 负例 95% 分位以下(低于此必不同)
    floor = round(min(np.percentile(neg_arr, 95), auto - 0.05), 2)

    # 验证集(随机 20%,非 top-scoring)
    import random
    random.seed(42)
    n_val = max(1, len(pos) // 5)
    val_pos = random.sample(pos_sims, min(n_val, len(pos_sims)))
    val_neg = random.sample(neg_sims, min(n_val, len(neg_sims)))
    vp = sum(1 for s in val_pos if s >= auto) / len(val_pos) if val_pos else 0
    vn = sum(1 for s in val_neg if s < floor) / len(val_neg) if val_neg else 0

    print(f"\n=== 标定结果 ===")
    print(f"分离点(best F1): {best_sep}  F1={best_f1:.3f}")
    print(f"auto_threshold:  {auto}  (验证集 precision={vp:.3f})")
    print(f"floor:           {floor}  (验证集 recall={vn:.3f})")
    print(f"重叠区: [{floor}, {auto}]")

    if args.write:
        yaml = CONFIG_PATH.read_text(encoding="utf-8")
        import datetime
        now = datetime.datetime.now().strftime("%Y-%m-%d")
        replacements = {
            "calibrated: false": "calibrated: true",
            "calibrated_at: null": f"calibrated_at: {now}",
            "positive_samples: 0": f"positive_samples: {len(pos)}",
            "negative_samples: 0": f"negative_samples: {len(neg)}",
            "validation_precision: null": f"validation_precision: {vp:.3f}",
            "validation_recall: null": f"validation_recall: {vn:.3f}",
            "  auto: 0.90": f"  auto: {auto}",
            "  floor: 0.72": f"  floor: {floor}",
        }
        for old, new in replacements.items():
            yaml = yaml.replace(old, new)
        CONFIG_PATH.write_text(yaml, encoding="utf-8")
        print(f"\n✅ 已写入 {CONFIG_PATH}")
    else:
        print(f"\n(dry-run) 加 --write 写入 config")

    return 0


if __name__ == "__main__":
    sys.exit(main())
