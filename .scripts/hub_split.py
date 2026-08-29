#!/usr/bin/env python3
"""hub_split.py — 研究方向 hub 自动分裂(父子层次)

触发: research-direction hub 的 ## 关键词 数 ≥ SPLIT_THRESHOLD(80)
流程:
  1. cluster_keywords 聚类 → 取最大 2 簇
  2. 每簇 top-N 中心词作 seeds → 建子 hub(frontmatter parent/seeds)
  3. 父 hub 全部 keywords 用新子 seeds 重匹配 → 分配到子 hub(命中的)
  4. 父 hub 清空 keywords(保留 ## 关键词 段作兜底)
  5. 建 父hub→子方向→子hub 图边

设计:
  - 层次真理源: graph.db 的 子方向 边(单一)
  - 父 hub frontmatter 加 parent/seeds 字段: parent 标记父(子hub用), seeds 存匹配锚点
  - 互斥: keyword 命中子就不进父(direction_matcher 在父子感知下保证)
  - 父 hub 保留 ## 关键词 段收"命中父但分不清子"的兜底 keyword
用法:
  python3 .scripts/hub_split.py --check                # 检查所有hub是否达分裂阈值
  python3 .scripts/hub_split.py --split <hub_path>     # 执行分裂
  python3 .scripts/hub_split.py --analyze <hub_path>   # 只分析不执行(看聚类建议)
"""
import sys
import time
import sqlite3
from pathlib import Path
from collections import Counter

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".scripts"))
import graph_lib as gl
from cluster_keywords import llm_name_cluster

SPLIT_THRESHOLD = 80        # research-direction hub keyword 数达此值触发分裂
SUBHUB_SEED_COUNT = 10     # 每个子 hub 取簇内 top-N 中心词作 seeds
MAX_CLUSTERS = 2           # 一次分裂产生最多 2 个子 hub(控制粒度)


def hub_keyword_count(hub_path):
    """读 hub 页 ## 关键词 段的 keyword 数。"""
    if not hub_path.endswith(".md"):
        hub_path = hub_path + ".md"
    p = REPO / hub_path if not hub_path.startswith("/") else Path(hub_path)
    if not p.exists():
        return 0
    lines = p.read_text(encoding="utf-8").splitlines()
    count = 0
    in_section = False
    for ln in lines:
        if ln.strip().startswith("## "):
            in_section = (ln.strip() == "## 关键词")
            continue
        if in_section and ln.strip().startswith("- "):
            count += 1
    return count


def parse_hub_keywords(hub_path):
    """读 hub 页 ## 关键词 段所有 keyword(复用 cluster_keywords 的逻辑)。"""
    if not hub_path.endswith(".md"):
        hub_path = hub_path + ".md"
    p = REPO / hub_path if not hub_path.startswith("/") else Path(hub_path)
    if not p.exists():
        return []  # ghost hub (merged but node not cleaned from graph.db)
    lines = p.read_text(encoding="utf-8").splitlines()
    kws = []
    in_section = False
    for ln in lines:
        if ln.strip().startswith("## "):
            in_section = (ln.strip() == "## 关键词")
            continue
        if in_section:
            s = ln.strip()
            if s.startswith("- "):
                kws.append(s[2:].strip())
    return kws


def check_all_hubs():
    """检查所有域的 research-direction hub 的 keyword 数,返回达分裂阈值的列表。"""
    over = []
    for sub in gl.SUBPROJECTS:
        hubs_dir = REPO / sub / "wiki" / "hubs"
        if not hubs_dir.exists():
            continue
        for f in hubs_dir.glob("*.md"):
            # 读 hub_subtype
            text = f.read_text(encoding="utf-8")
            if "hub_subtype: research-direction" not in text:
                continue
            cnt = hub_keyword_count(str(f.relative_to(REPO)))
            if cnt >= SPLIT_THRESHOLD:
                over.append({"path": f.relative_to(REPO).as_posix(), "count": cnt})
    return over


SUB_THRESHOLD = 0.65  # 子方向重分配阈值(与 direction_matcher SUB_THRESHOLD 一致)


def evaluate_split(keywords, sub_clusters, sub_threshold=SUB_THRESHOLD, kvecs=None, seed_mats=None):
    """评价分裂质量: 用子 seeds 多归属重分配模拟,算覆盖度与重合度。
    覆盖度 = 分到任一子的keyword数 / 原父总数 (越高越好)
    重合度 = 两子交集 / 两子并集 Jaccard (越低越好)
    综合 = 覆盖度 - 重合度 (越大分裂越优)
    性能优化(v8.1): kvecs/seed_mats 由调用方传入复用,避免每个t重复embed。
    """
    from embed_helper import embed_cached_batch, cosine_sim
    import numpy as np
    if len(sub_clusters) < 2:
        return {"coverage": 0, "overlap": 1, "score": -1}
    if kvecs is None:
        kvecs = embed_cached_batch(keywords, cache_type="keyword")
    if seed_mats is None:
        seed_mats = [np.array(embed_cached_batch(c["seeds"], cache_type="keyword")) for c in sub_clusters]
    in_sub = [set() for _ in sub_clusters]
    for idx, kw in enumerate(keywords):
        for si, smat in enumerate(seed_mats):
            sims = cosine_sim(kvecs[idx], smat)
            if float(sims.max()) >= sub_threshold:
                in_sub[si].add(kw)
    union = set().union(*in_sub) if in_sub else set()
    inter = set.intersection(*in_sub) if len(in_sub) >= 2 else set()
    coverage = len(union) / len(keywords) if keywords else 0
    overlap = len(inter) / len(union) if union else 0
    return {"coverage": coverage, "overlap": overlap, "score": coverage - overlap,
            "union": len(union), "inter": len(inter)}


def _select_seeds(top_clusters, embs, keywords, alpha=0.3):
    """判别性评分选 seeds: score = own − alpha*sib(默认 alpha=0.3(经验最优:coverage 不掉 + overlap↓);alpha=1.0 纯判别)。
    own = cos(kw,本簇质心)(概括性,越高越能代表本簇→coverage);
    sib = max cos(kw,兄弟簇质心)(重合风险,越低越不像兄弟→overlap↓)。
    alpha=0 退化为"距质心最近"(纯概括性);alpha=0.3 兼顾(默认);alpha=1 纯判别(overlap↓但 coverage 可能↓)。
    """
    import numpy as np
    cluster_data = []
    for cluster_kws in top_clusters:
        cluster_embs = embs[[keywords.index(k) for k in cluster_kws]]
        centroid = cluster_embs.mean(axis=0)
        cluster_data.append({"keywords": cluster_kws, "embs": cluster_embs, "centroid": centroid})
    sub_clusters = []
    for ci, cd in enumerate(cluster_data):
        own_c = cd["centroid"]
        sib_centroids = [cluster_data[j]["centroid"] for j in range(len(cluster_data)) if j != ci]
        kw_embs = cd["embs"]
        kw_unit = kw_embs / (np.linalg.norm(kw_embs, axis=1, keepdims=True) + 1e-9)
        own_unit = own_c / (np.linalg.norm(own_c) + 1e-9)
        own_sims = kw_unit @ own_unit  # [n]
        if sib_centroids:
            sib_mat = np.array(sib_centroids)
            sib_unit = sib_mat / (np.linalg.norm(sib_mat, axis=1, keepdims=True) + 1e-9)
            sib_max = (kw_unit @ sib_unit.T).max(axis=1)  # [n]
        else:
            sib_max = np.zeros(len(kw_embs))
        scores = own_sims - alpha * sib_max  # alpha 越大越偏判别
        top_idx = np.argsort(scores)[::-1][:SUBHUB_SEED_COUNT]
        seeds = [cd["keywords"][i] for i in top_idx]
        sub_clusters.append({"keywords": cd["keywords"], "centroid": own_c, "seeds": seeds})
    return sub_clusters


def cluster_hub_keywords(keywords, max_clusters=MAX_CLUSTERS):
    """对 keywords 做 embedding + 层次聚类,遍历切分点 t 用覆盖度-重合度综合评价选最优。
    返回 [{keywords, centroid, seeds}] 最多 max_clusters 簇。
    v8(2026-07-28): 切分点选择改为覆盖度↑+重合度↓综合最优(替原"第一个满足硬约束的t")。
    """
    from embed_helper import embed_cached_batch
    import numpy as np
    from scipy.spatial.distance import pdist; from scipy.cluster.hierarchy import linkage, fcluster
    embs = embed_cached_batch(keywords, cache_type="keyword")
    dists = pdist(embs, metric='cosine')
    Z = linkage(dists, method='average')
    # 遍历所有候选 t,对每个 t 聚类→建seeds→模拟重分配→算覆盖度-重合度,选综合最优
    candidates = []
    for t in np.linspace(0.2, 0.9, 29):  # 更密扫描
        labels = fcluster(Z, t=t, criterion='distance')
        clusters = {}
        for kw, label in zip(keywords, labels):
            clusters.setdefault(label, []).append(kw)
        sorted_clusters = sorted(clusters.values(), key=len, reverse=True)
        if len(sorted_clusters) < max_clusters:
            continue
        if not all(len(c) >= 5 for c in sorted_clusters[:max_clusters]):
            continue
        top_clusters = sorted_clusters[:max_clusters]
        # 建每簇 seeds(判别性评分:本簇质心相似度−兄弟簇最大相似度)
        sub_clusters = _select_seeds(top_clusters, embs, keywords)
        # 评价该 t
        ev = evaluate_split(keywords, sub_clusters)
        # 均衡因子: 避免选极不均衡的切分(如 83:6 导致子 hub 仍超阈需递归)
        top_sizes = [len(c["keywords"]) for c in sub_clusters]
        balance = min(top_sizes) / max(top_sizes) if max(top_sizes) > 0 else 0
        candidates.append((t, ev["score"] * balance, ev, sub_clusters))
    if not candidates:
        # 兜底:无候选满足约束,取 t=0.5
        labels = fcluster(Z, t=0.5, criterion='distance')
        clusters = {}
        for kw, label in zip(keywords, labels):
            clusters.setdefault(label, []).append(kw)
        sorted_clusters = sorted(clusters.values(), key=len, reverse=True)
        top_clusters = sorted_clusters[:max_clusters]
        return _select_seeds(top_clusters, embs, keywords)
    # 选综合得分最高的 t
    candidates.sort(key=lambda x: -x[1])
    best_t, best_score, best_ev, best_clusters = candidates[0]
    best_sizes = [len(c["keywords"]) for c in best_clusters]
    best_bal = min(best_sizes) / max(best_sizes) if max(best_sizes) > 0 else 0
    print(f"[hub_split] 选最优 t={best_t:.2f} 综合={best_score:.3f} (覆盖={best_ev["coverage"]:.3f} 重合={best_ev["overlap"]:.3f} 均衡={best_bal:.2f} 簇={best_sizes})", file=sys.stderr)
    return best_clusters


def create_subhub(parent_path, parent_name, sub_name, seeds, display_name=None, subproject="academic"):
    """建子 hub 页(frontmatter parent/seeds + ## 关键词 空段)。返回子hub相对路径。

    sub_name 用作文件名(哈希防冲突)；display_name 用作 title 和正文标题(语义名)。
    """
    sub_path = f"{subproject}/wiki/hubs/{sub_name}"
    parent_node_path = parent_path.replace(".md", "")
    sub_file = REPO / (sub_path + ".md")
    sub_file.parent.mkdir(parents=True, exist_ok=True)
    today = time.strftime("%Y-%m-%d")
    seeds_yaml = ", ".join(seeds)
    title = display_name or sub_name
    content = f"""---
title: "{title}"
type: topic-hub
hub_subtype: research-direction
parent: "{parent_node_path}"
seeds: [{seeds_yaml}]
status: active
created: {today}
updated: {today}
---

# {title}

> {parent_name} 子方向(自动分裂生成,{today})。seeds 作 direction_matcher 匹配锚点。

## 关键词

"""
    sub_file.write_text(content, encoding="utf-8")
    return sub_path


def add_child_edge(conn, parent_path, sub_path):
    """建 父hub→子方向→子hub 图边。hub title 从 frontmatter 读(ensure_node 用 INSERT OR REPLACE,传空串会覆盖已有 title)。"""
    _pt = gl.read_frontmatter(parent_path).get("title") or Path(parent_path).name
    _st = gl.read_frontmatter(sub_path).get("title") or Path(sub_path).name
    gl.ensure_node(conn, parent_path, _pt, "hub", "", "", "current", 0)
    gl.ensure_node(conn, sub_path, _st, "hub", "", "", "current", 0)
    # 去重:已有子方向边则不重建
    existing = conn.execute(
        "SELECT 1 FROM edges WHERE subject=? AND predicate='子方向' AND object=?",
        (parent_path, sub_path)
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO edges(subject, predicate, object, confidence, source, is_sr) VALUES(?,?,?,?,?,?)",
            (parent_path, "子方向", sub_path, "可追溯", "", 0)
        )
        conn.commit()
        return True
    return False


def redistribute_keywords(keywords, sub_clusters):
    """把父 hub 的 keywords 按子 seeds 重匹配,分配到各子 hub(同父互斥)。
    规则:
      最高分子 ≥ 阈值 → 归最高分子
      最高分子 < 阈值 且与父 cosine < 阈值 → 归最高分子(推到子,不污染父)
      最高分子 < 阈值 且与父 cosine ≥ 阈值 → 留父兜底(确实属于父方向)
    与 evaluate_split 的区别:evaluate 用多归属模拟算 overlap(预测天然可分性),
    本函数是实际分配(强制互斥,交集恒为0)。
    """
    from embed_helper import embed_cached_batch, cosine_sim
    import numpy as np
    kvecs = embed_cached_batch(keywords, cache_type="keyword")
    seed_mats = [np.array(embed_cached_batch(c["seeds"], cache_type="keyword")) for c in sub_clusters]
    # 父 hub 质心(全部 keywords 的平均向量)
    parent_centroid = kvecs.mean(axis=0)
    parent_unit = parent_centroid / (np.linalg.norm(parent_centroid) + 1e-9)
    assign = {i: [] for i in range(len(sub_clusters))}
    fallback = []
    for idx, kw in enumerate(keywords):
        # 互斥:同父的子取最高分,只归一个(或都不命中归父)
        best_si, best_sc = None, -1.0
        for si, smat in enumerate(seed_mats):
            sc = float(cosine_sim(kvecs[idx], smat).max())
            if sc > best_sc:
                best_si, best_sc = si, sc
        # 与父 hub 质心的 cosine
        kw_unit = kvecs[idx] / (np.linalg.norm(kvecs[idx]) + 1e-9)
        parent_sc = float(kw_unit @ parent_unit)
        if best_si is not None and best_sc >= SUB_THRESHOLD:
            assign[best_si].append(kw)
        elif best_si is not None and parent_sc < SUB_THRESHOLD:
            # 与子都不匹配且与父也不匹配 → 推到最高分子(不留在父污染)
            assign[best_si].append(kw)
        else:
            fallback.append(kw)
    return assign, fallback



def split_hub(hub_path, dry_run=False):
    """执行分裂。hub_path 相对路径。返回报告 dict。"""
    if not hub_path.endswith(".md"):
        hub_path = f"{hub_path}.md"
    parent_name = Path(hub_path).stem
    keywords = parse_hub_keywords(hub_path)
    report = {"hub": hub_path, "keyword_count": len(keywords), "subhubs": [], "fallback_count": 0}
    if len(keywords) < SPLIT_THRESHOLD:
        report["skipped"] = f"未达阈值 {SPLIT_THRESHOLD}"
        return report
    # 1. 聚类
    sub_clusters = cluster_hub_keywords(keywords)
    if len(sub_clusters) < 2:
        report["skipped"] = "聚类未能分出≥2簇"
        return report
    if dry_run:
        for i, c in enumerate(sub_clusters):
            report["subhubs"].append({"seeds": c["seeds"], "keyword_count": len(c["keywords"])})
        # dry-run 也报重分配预测(互斥)
        assign, fallback = redistribute_keywords(keywords, sub_clusters)
        report["fallback_count"] = len(fallback)
        report["assigned"] = {i: len(assign[i]) for i in range(len(sub_clusters))}
        return report
    # 2. 建子 hub：使用独立短名 + 一次性编号（与父 hub 的包含关系只走子方向边承载，不进文件名）
    db = sqlite3.connect(REPO / "cross-domain" / "graph.db")
    sub_paths = []
    for i, c in enumerate(sub_clusters, 1):
        import uuid
        token = uuid.uuid4().hex[:6]
        sub_name = f"子方向-{token}"
        display_name = llm_name_cluster(c["seeds"])
        if not display_name:
            display_name = sub_name
        _sub = hub_path.split("/")[0] if "/" in hub_path else "academic"
        sp = create_subhub(hub_path, parent_name, sub_name, c["seeds"], display_name=display_name, subproject=_sub)
        sub_paths.append((sp, c))
        add_child_edge(db, hub_path.replace('.md',''), sp)
        report["subhubs"].append({"path": sp, "name": display_name, "seeds": c["seeds"], "seed_count": len(c["seeds"])})
    # 3. 重分配父 keywords 到子(同父互斥)
    assign, fallback = redistribute_keywords(keywords, sub_clusters)
    # 4. 写入子 hub keywords + 清空父 hub keywords(保留段收 fallback)
    from cluster_keywords import add_keywords_to_hub, remove_keywords_from_hub
    for (sp, _c), kws in zip(sub_paths, [assign[i] for i in range(len(sub_clusters))]):
        if kws:
            add_keywords_to_hub(sp + '.md', kws)
    # 父 hub: 移除全部分配走的,保留 fallback
    remove_keywords_from_hub(hub_path, keywords)
    if fallback:
        add_keywords_to_hub(hub_path, fallback)
    report["fallback_count"] = len(fallback)
    report["assigned"] = {sp: len(assign[i]) for (sp, _), i in zip(sub_paths, range(len(sub_clusters)))}
    db.close()
    return report


# ===== 动态分裂+融合机制 =====

MERGE_THRESHOLD = 0.85  # 子hub与已有hub的embedding相似度阈值
MIN_SEEDS = 3            # seeds 低于此数触发自动补充


def get_ancestors(conn, hub, max_depth=3):
    """向上追溯祖先（含姻亲），BFS。返回祖先集合（不含 hub 自身）。"""
    ancestors = set()
    queue = [(hub, 0)]
    while queue:
        h, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        # 沿子方向边反向: object=hub → subject=父
        parents = [r[0] for r in conn.execute(
            "SELECT subject FROM edges WHERE predicate='子方向' AND object=?", (h,)
        ).fetchall()]
        # 沿姻亲边反向: object=hub → subject=姻亲源
        affiliates = [r[0] for r in conn.execute(
            "SELECT subject FROM edges WHERE predicate='姻亲' AND object=?", (h,)
        ).fetchall()]
        for p in parents + affiliates:
            if p not in ancestors:
                ancestors.add(p)
                queue.append((p, depth + 1))
    return ancestors


def has_blood_relation(conn, hub_a, hub_b, max_depth=3):
    """两个 hub 向上追溯三代（含姻亲），有交叉连接则有血缘关系。"""
    anc_a = get_ancestors(conn, hub_a, max_depth)
    anc_b = get_ancestors(conn, hub_b, max_depth)
    return bool(anc_a & anc_b) or hub_a in anc_b or hub_b in anc_a


def find_similar_hub(conn, sub_seeds, exclude_set, threshold=MERGE_THRESHOLD):
    """用 embedding 找与子 hub seeds 最相似的已有 hub。返回 hub path 或 None。"""
    from embed_helper import embed_cached_batch
    import numpy as np
    if not sub_seeds:
        return None
    sub_vecs = embed_cached_batch(sub_seeds, cache_type="keyword")
    sub_centroid = sub_vecs.mean(axis=0)
    sub_unit = sub_centroid / (np.linalg.norm(sub_centroid) + 1e-9)
    best_hub = None
    best_score = threshold
    for (hub_path,) in conn.execute(
        "SELECT path FROM nodes WHERE type='hub' AND path LIKE '%/wiki/hubs/%'"
    ).fetchall():
        if hub_path in exclude_set:
            continue
        kws = parse_hub_keywords(hub_path)
        if len(kws) < 3:
            continue
        hub_vecs = embed_cached_batch(kws, cache_type="keyword")
        hub_centroid = hub_vecs.mean(axis=0)
        hub_unit = hub_centroid / (np.linalg.norm(hub_centroid) + 1e-9)
        score = float(sub_unit @ hub_unit)
        if score > best_score:
            best_score = score
            best_hub = hub_path
    return best_hub


def _read_hub_seeds(hub_path):
    """读 hub frontmatter 的 seeds 字段。"""
    import yaml
    p = REPO / (hub_path + ".md") if not hub_path.endswith(".md") else REPO / hub_path
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8")
    m = __import__("re").match(r"^---\n(.*?)\n---\n", text, __import__("re").S)
    if not m:
        return []
    try:
        fm = yaml.safe_load(m.group(1)) or {}
        seeds = fm.get("seeds", [])
        return seeds if isinstance(seeds, list) else []
    except Exception:
        return []


def _update_hub_seeds(hub_path, seeds):
    """更新 hub frontmatter 的 seeds 字段。"""
    import yaml, re
    p = REPO / (hub_path + ".md") if not hub_path.endswith(".md") else REPO / hub_path
    if not p.exists():
        return
    text = p.read_text(encoding="utf-8")
    m = re.match(r"^(---\n)(.*?)(\n---\n)", text, re.S)
    if not m:
        return
    try:
        fm = yaml.safe_load(m.group(2)) or {}
    except Exception:
        fm = {}
    fm["seeds"] = seeds
    new_fm = yaml.dump(fm, allow_unicode=True, default_flow_style=False, sort_keys=False)
    new_text = m.group(1) + new_fm + m.group(3) + text[m.end():]
    p.write_text(new_text, encoding="utf-8")


def ensure_hub_seeds(hub_path, min_seeds=MIN_SEEDS, target_count=SUBHUB_SEED_COUNT, exclude_seeds=None):
    """确保 hub 有足够 seeds。不足时从 ## 关键词 自动补充（距质心最近的 top-N）。
    条件: hub 有 ≥ min_seeds 个关键词时才补充（否则信息不足，返回原 seeds）。
    exclude_seeds: 全局已用 seed 集合(casefold)，补充时跳过这些 keyword。
    保留已有非冲突 seeds, 仅从 available 中补足; 无可用时不退回已排除项(防冲突循环)。
    返回 seeds 列表（可能仍不足 min_seeds）。
    """
    seeds = _read_hub_seeds(hub_path)
    if len(seeds) >= min_seeds:
        return seeds
    keywords = parse_hub_keywords(hub_path)
    if len(keywords) < min_seeds:
        return seeds  # 关键词也不够，无法补充
    from embed_helper import embed_cached_batch
    import numpy as np
    exclude = {s.casefold() for s in (exclude_seeds or set())}
    # 保留已有非冲突 seeds
    kept = [s for s in seeds if s.casefold() not in exclude]
    # 可用 keyword: 未被排除且非已有 seed
    available = [k for k in keywords if k.casefold() not in exclude and k not in kept]
    if not available:
        # 无可用补充: 保留现有(防冲突循环, hub 可能需合并)
        if kept != seeds:
            _update_hub_seeds(hub_path, kept)
        return kept
    kvecs = embed_cached_batch(available, cache_type="keyword")
    # 域守卫: centroid 锚点用 stored seeds(kept)而非 available candidates,
    # 防止跨域论文误入后从其 keyword 补出跨域 seed(污染自放大循环)。
    if kept:
        anchor_vecs = embed_cached_batch(kept, cache_type="keyword")
        centroid = anchor_vecs.mean(axis=0)
    else:
        centroid = kvecs.mean(axis=0)
    centroid_unit = centroid / (np.linalg.norm(centroid) + 1e-9)
    kw_units = kvecs / (np.linalg.norm(kvecs, axis=1, keepdims=True) + 1e-9)
    sims = kw_units @ centroid_unit
    need = max(target_count - len(kept), min_seeds - len(kept))
    top_idx = np.argsort(sims)[::-1][:need]
    new_seeds = kept + [available[i] for i in top_idx]
    _update_hub_seeds(hub_path, new_seeds)
    print(f"[hub_split] seeds 补充: {hub_path} {len(seeds)}→{len(new_seeds)} (保留{len(kept)}+补{len(new_seeds)-len(kept)}, 排除{len(exclude)}已用)")
    return new_seeds


def merge_hubs(conn, source_hub, target_hub):
    """合并 source hub 到 target hub。
    - seeds: 并集去重
    - keywords: 迁移到 target（去重）
    - 边: 全部迁移，子方向→姻亲
    - 删除 source hub 节点+页面
    """
    from cluster_keywords import add_keywords_to_hub
    # 1. seeds 并集（先确保双方 seeds 充足）
    source_seeds = ensure_hub_seeds(source_hub)
    target_seeds = ensure_hub_seeds(target_hub)
    merged_seeds = list(dict.fromkeys(target_seeds + source_seeds))
    _update_hub_seeds(target_hub, merged_seeds)
    # 2. keywords 迁移
    source_kws = parse_hub_keywords(source_hub)
    if source_kws:
        add_keywords_to_hub(target_hub + ".md", source_kws)
    # 3. 边迁移: source 作为 subject 的边
    for row in conn.execute(
        "SELECT predicate, object FROM edges WHERE subject=?", (source_hub,)
    ).fetchall():
        pred, obj = row[0], row[1]
        if obj == target_hub:
            continue  # 跳过自环(source→target 父子边迁移后变自环)
        new_pred = "姻亲" if pred == "子方向" else pred
        conn.execute(
            "INSERT OR IGNORE INTO edges (subject, predicate, object, confidence, source, is_sr) "
            "VALUES (?,?,?,?,?,?)",
            (target_hub, new_pred, obj, "[可追溯]", "", 0)
        )
    # 3b. source 作为 object 的边
    for row in conn.execute(
        "SELECT subject, predicate FROM edges WHERE object=?", (source_hub,)
    ).fetchall():
        subj, pred = row[0], row[1]
        if subj == target_hub:
            continue  # 跳过自环
        new_pred = "姻亲" if pred == "子方向" else pred
        conn.execute(
            "INSERT OR IGNORE INTO edges (subject, predicate, object, confidence, source, is_sr) "
            "VALUES (?,?,?,?,?,?)",
            (subj, new_pred, target_hub, "[可追溯]", "", 0)
        )
    # 3c. 删除 source 的所有边
    conn.execute("DELETE FROM edges WHERE subject=? OR object=?", (source_hub, source_hub))
    # 4. 删除 source hub 节点
    conn.execute("DELETE FROM nodes WHERE path=?", (source_hub,))
    # 5. 删除 source hub 页面
    source_file = REPO / (source_hub + ".md")
    if source_file.exists():
        source_file.unlink()
    conn.commit()
    return {"merged": source_hub, "into": target_hub, "seeds_count": len(merged_seeds),
            "keywords_moved": len(source_kws)}


MAX_SPLIT_ITERATIONS = 5  # 动态分裂+融合最大迭代次数


def dynamic_split(conn, hub_path, max_iterations=MAX_SPLIT_ITERATIONS):
    """动态分裂+融合：分裂→子hub与已有hub对比→相似且无血缘则融合→融合后超阈值再分裂→循环至收敛。

    next_pending 收集本轮分裂/融合后仍超阈(SPLIT_THRESHOLD)的 hub, 下轮继续处理;
    processed 集合保证每 hub 至多分裂一次, 既防同 hub 反复分裂, 也防 A↔B 乒乓
    (A 分裂→子融合入 B→B 超阈→B 分裂→子融合入 A→A 在 processed 内不再分裂)。
    processed 内 hub 融合后即便超阈也不再本轮分裂, 留待下次 ingest 兜底(全量重扫)。
    """
    if hub_path.endswith(".md"):
        hub_path = hub_path[:-3]  # 归一化为无后缀, 与 nodes.path / create_subhub 返回一致
    results = []
    pending = [hub_path]
    processed = set()      # 已分裂的 hub(每 hub 至多分裂一次)
    for iteration in range(max_iterations):
        if not pending:
            break
        next_pending = []
        changed = False
        for hp in pending:
            if hp in processed:
                continue
            processed.add(hp)
            # 1. 分裂
            split_result = split_hub(hp)
            if "skipped" in split_result:
                continue
            changed = True
            subhubs = split_result.get("subhubs", [])
            # 2. 子 hub 与已有 hub 对比
            for sub in subhubs:
                sub_path = sub.get("path", "")
                sub_seeds = sub.get("seeds", [])
                if not sub_path:
                    continue
                similar = find_similar_hub(conn, sub_seeds, exclude_set={hp, sub_path})
                if similar and not has_blood_relation(conn, sub_path, similar):
                    # 3. 融合（无血缘关系）
                    merge_result = merge_hubs(conn, sub_path, similar)
                    results.append({"iteration": iteration + 1, "merged": sub_path,
                                    "into": similar, "result": merge_result})
                    # 融合后超阈值则纳入下一轮再分裂(processed 内除外, 防乒乓)
                    if hub_keyword_count(similar) >= SPLIT_THRESHOLD and similar not in processed:
                        next_pending.append(similar)
                else:
                    # 有血缘关系或无相似 hub → 保留为新 hub
                    results.append({"iteration": iteration + 1, "created": sub_path})
                    # 新 hub 超阈值则纳入下一轮再分裂
                    if hub_keyword_count(sub_path) >= SPLIT_THRESHOLD and sub_path not in processed:
                        next_pending.append(sub_path)
        pending = next_pending
        if not changed:
            break  # 收敛
    if pending:
        results.append({"unresolved": list(pending),
                         "reason": "达迭代上限或 processed 内 hub 融合后超阈(留待下次 ingest 兜底)"})
    return results


def main():
    import argparse, json
    ap = argparse.ArgumentParser(description="hub 自动分裂+融合+seeds维护")
    ap.add_argument("--check", action="store_true", help="检查所有hub是否达分裂阈值")
    ap.add_argument("--analyze", help="只分析不执行(看聚类建议)")
    ap.add_argument("--split", help="执行分裂")
    ap.add_argument("--dedup-seeds", action="store_true", help="种子全局互斥: 同一 seed 只保留在一个方向")
    ap.add_argument("--name-seed", action="store_true", help="hub名与seed互斥: seed 匹配 hub名则移除,hub名优先")
    ap.add_argument("--consolidation", action="store_true", help="检测子方向 hub 合并/重分配计划(dry-run)")
    ap.add_argument("--run-consolidation", action="store_true", help="执行合并/重分配(检测→执行→重检测循环)")
    ap.add_argument("--rename-placeholders", action="store_true", help="占位 hub(子方向-xxxxx) LLM 语义命名")
    ap.add_argument("--merge-duplicates", action="store_true", help="合并同名 hub(不同文件但 title 相同)")
    args = ap.parse_args()
    if args.check:
        over = check_all_hubs()
        if not over:
            print(f"无 research-direction hub 达分裂阈值({SPLIT_THRESHOLD})")
        for h in over:
            print(f"  {h['path']}: {h['count']} keywords (达阈值)")
    elif args.analyze:
        rep = split_hub(args.analyze, dry_run=True)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    elif args.split:
        rep = split_hub(args.split, dry_run=False)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    elif args.dedup_seeds:
        rep = dedup_seeds()
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    elif args.name_seed:
        rep = enforce_name_seed_exclusivity()
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    elif args.consolidation:
        rep = detect_hub_consolidation()
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    elif args.run_consolidation:
        rep = run_consolidation()
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    elif args.rename_placeholders:
        rep = rename_placeholder_hubs()
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    elif args.merge_duplicates:
        rep = merge_duplicate_hubs()
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))




def sync_hub_seeds_if_drift(hub_path, drift_ratio=0.33):
    """规则: hub keywords 变动(数量或内容)超过 drift_ratio 时重选 seeds。

    判据: 现有 seeds 中有多少已不在当前 ## 关键词 段(失效比例)。
    失效比例 > drift_ratio → 从当前 keywords 质心强制重选(不管 seeds 数量是否够)。
    返回 (updated: bool, old_count, new_count, stale_ratio)。
    """
    old_seeds = _read_hub_seeds(hub_path)
    keywords = parse_hub_keywords(hub_path)
    if not old_seeds or not keywords:
        return False, len(old_seeds), 0, 0.0
    kw_set = {k.strip() for k in keywords}
    stale = [s for s in old_seeds if str(s).strip() not in kw_set]
    stale_ratio = len(stale) / len(old_seeds) if old_seeds else 0.0
    # 数量变动也超 1/3: keywords 数量 vs seeds 来源数量偏差大
    count_drift = abs(len(keywords) - len(old_seeds)) / max(len(old_seeds), 1) > drift_ratio
    if stale_ratio > drift_ratio or count_drift:
        # 强制重选: 从当前 keywords 质心选 top-N(不走 ensure_hub_seeds 的"不足才补"逻辑)
        from embed_helper import embed_cached_batch
        import numpy as np
        kvecs = embed_cached_batch(keywords, cache_type="keyword")
        # 域守卫: centroid 锚点用仍有效的 old_seeds(非 stale)防止跨域漂移重选。
        valid_old = [str(s).strip() for s in old_seeds if str(s).strip() in kw_set]
        if valid_old:
            anchor_vecs = embed_cached_batch(valid_old, cache_type="keyword")
            centroid = anchor_vecs.mean(axis=0)
        else:
            centroid = kvecs.mean(axis=0)
        centroid_unit = centroid / (np.linalg.norm(centroid) + 1e-9)
        kw_units = kvecs / (np.linalg.norm(kvecs, axis=1, keepdims=True) + 1e-9)
        sims = kw_units @ centroid_unit
        top_idx = np.argsort(sims)[::-1][:SUBHUB_SEED_COUNT]
        new_seeds = [keywords[i] for i in top_idx]
        _update_hub_seeds(hub_path, new_seeds)
        return True, len(old_seeds), len(new_seeds), stale_ratio
    return False, len(old_seeds), len(old_seeds), stale_ratio


def sync_all_hub_seeds(drift_ratio=0.33):
    """扫描所有子方向 hub, 对 seeds 漂移超阈值的重选。返回汇总报告。"""
    hubs_dir = REPO / "academic" / "wiki" / "hubs"
    if not hubs_dir.exists():
        return {"checked": 0, "updated": 0, "details": []}
    updated = 0
    details = []
    for md in sorted(hubs_dir.glob("*.md")):
        hub_rel = f"academic/wiki/hubs/{md.stem}"
        # 只处理有 parent 的子方向 hub
        text = md.read_text(encoding="utf-8")
        import re
        fm_match = re.search(r"^---\n(.*?)\n---", text, re.S)
        if not fm_match:
            continue
        try:
            import yaml
            fm = yaml.safe_load(fm_match.group(1)) or {}
        except Exception:
            continue
        if not fm.get("parent"):
            continue  # 根方向, seeds 在 yaml 不在 hub
        did, old_n, new_n, stale = sync_hub_seeds_if_drift(hub_rel, drift_ratio)
        if did:
            updated += 1
            details.append({"hub": md.stem, "old": old_n, "new": new_n, "stale_ratio": round(stale, 2)})
    return {"checked": len(details) + (len(list(hubs_dir.glob('*.md'))) - len(details)),
            "updated": updated, "details": details}


def _is_blood_relation(a, b, parents):
    """Check if a is an ancestor of b or vice versa (via parent chain)."""
    def is_ancestor(anc, desc):
        cur = desc
        seen = set()
        while cur in parents and cur not in seen:
            seen.add(cur)
            cur = parents[cur]
            if cur == anc:
                return True
        return False
    return is_ancestor(a, b) or is_ancestor(b, a)


def detect_hub_consolidation(merge_name=0.70, merge_jaccard=0.15, merge_centroid=0.82,
                              realloc_centroid=0.85, realloc_cross=0.15, realloc_margin=0.03,
                              min_keywords=3, conn=None):
    """检测子方向 hub 合并/重分配计划(两阶段 + 分布统计量)。

    指标:
    - name_sim: hub 名称 embedding cosine(语义名相似度)
    - seed_jaccard: seeds 集合 Jaccard(锚点重叠)
    - centroid_sim: 两 hub 质心 cosine(中心距离)
    - cross_contamination: A 中 keyword 对 B 质心 > 对 A 质心的比例(分布可分性)
    - sep_margin: 可分性边际 = mean(cos_own - cos_other), keyword 对自身 hub 质心
      与对方 hub 质心的 cosine 差值均值。高→keyword 可区分两 hub; 低→不可区分。

    两阶段判定:
    Stage 1 — MERGE (全维度一致):
      name_sim > merge_name AND seed_jaccard > merge_jaccard AND centroid_sim > merge_centroid
      → 三维度(名称/种子/质心)一致, 同概念重复, 直接合并

    Stage 2 — 可分性测试 (质心近 + keyword 混配, 但非全维度一致):
      centroid_sim > realloc_centroid AND cross > realloc_cross AND NOT Stage 1
      → sep_margin 分布统计量:
        sep_margin <= realloc_margin → 不可分离, 同概念 → MERGE
        sep_margin > realloc_margin → 可分离, 不同概念 keyword 混配 → REALLOC

    返回 {"merge_groups": [...], "realloc_pairs": [...], "all_pairs": [...], "stats": {...}}
    """
    import sys
    sys.path.insert(0, str(REPO / ".scripts"))
    import direction_matcher as dm
    from embed_helper import embed_cached_batch
    import numpy as np
    import yaml, re

    dm._DIR_VECS = None
    dm.ensure_direction_embeddings(force=False)
    defs = dm._load_direction_defs()
    parents = dm._DIR_PARENTS or {}

    # 三代血缘+姻亲检查需要 graph.db 连接(姻亲边只在图数据库)
    _own_conn = conn is None
    if _own_conn:
        import sqlite3
        from graph_lib import GRAPH_DB
        conn = sqlite3.connect(str(GRAPH_DB))

    def get_hub_file(name):
        for f in (REPO / "academic" / "wiki" / "hubs").glob("*.md"):
            text = f.read_text(encoding="utf-8")
            m = re.search(r"^---\n(.*?)\n---", text, re.S)
            if m:
                try:
                    fm = yaml.safe_load(m.group(1)) or {}
                    if fm.get("title") == name:
                        return f
                except Exception:
                    pass
        return None

    def unit(v):
        return v / (np.linalg.norm(v, axis=-1, keepdims=True) + 1e-9)

    hubs = {}
    for d in defs:
        name = d["name"]
        if name not in parents:
            continue
        hf = get_hub_file(name)
        if not hf:
            continue
        kws = parse_hub_keywords(f"academic/wiki/hubs/{hf.stem}")
        if len(kws) < min_keywords:
            continue
        expand = [dm.expand_keyword(k) for k in kws]
        kvecs = embed_cached_batch(expand, cache_type="keyword")
        kvecs_u = unit(kvecs)
        centroid = kvecs_u.mean(axis=0)
        hubs[name] = {
            "path": f"academic/wiki/hubs/{hf.stem}",
            "seeds": set(s.casefold() for s in d.get("seeds", [])),
            "keywords": kws,
            "kvecs_u": kvecs_u,
            "centroid_u": unit(centroid),
            "kw_count": len(kws),
        }

    # hub 名称 embedding (一次性批量)
    names_list = list(hubs.keys())
    if not names_list:
        return {"merge_groups": [], "realloc_pairs": [], "all_pairs": [], "stats": {"total_subdirs": 0}}
    name_vecs = embed_cached_batch(names_list, cache_type="hub-name")
    name_vecs_u = unit(name_vecs)

    names = list(hubs.keys())
    merge_pairs = []
    realloc_pairs = []
    all_pairs = []
    for i, a in enumerate(names):
        for j, b in enumerate(names):
            if j <= i:
                continue
            ha, hb = hubs[a], hubs[b]
            c_sim = float(ha["centroid_u"] @ hb["centroid_u"])
            inter = len(ha["seeds"] & hb["seeds"])
            union = len(ha["seeds"] | hb["seeds"])
            jaccard = inter / union if union else 0
            name_sim = float(name_vecs_u[i] @ name_vecs_u[j])
            # 可分性边际: 每个 keyword 对自身 hub 质心 vs 对方 hub 质心的 cosine 差
            a_own = ha["kvecs_u"] @ ha["centroid_u"]
            a_other = ha["kvecs_u"] @ hb["centroid_u"]
            b_own = hb["kvecs_u"] @ hb["centroid_u"]
            b_other = hb["kvecs_u"] @ ha["centroid_u"]
            margins = np.concatenate([a_own - a_other, b_own - b_other])
            sep_margin = float(np.mean(margins))
            cross_a = float(np.mean(a_other > a_own))
            cross_b = float(np.mean(b_other > b_own))
            cross = max(cross_a, cross_b)
            combined_kw = ha["kw_count"] + hb["kw_count"]
            too_large = combined_kw > SPLIT_THRESHOLD
            metrics = {"a": a, "b": b,
                       "path_a": ha["path"], "path_b": hb["path"],
                       "c_sim": round(c_sim, 3), "jaccard": round(jaccard, 3),
                       "name_sim": round(name_sim, 3), "sep_margin": round(sep_margin, 4),
                       "cross": round(cross, 3), "cross_a": round(cross_a, 3), "cross_b": round(cross_b, 3),
                       "combined_kw": combined_kw}
            all_pairs.append(metrics)
            # 三代血缘+姻亲互斥: 有血缘或姻亲关系的 hub 禁止合并(防合并-分裂死循环)
            if has_blood_relation(conn, ha["path"], hb["path"], max_depth=3):
                continue
            # Stage 1: 全维度一致 → MERGE (unless combined > SPLIT_THRESHOLD → 防合并-分裂死循环)
            if name_sim > merge_name and jaccard > merge_jaccard and c_sim > merge_centroid:
                if not too_large:
                    merge_pairs.append((a, b, metrics))
                elif sep_margin > realloc_margin:
                    realloc_pairs.append((a, b, metrics))  # 降级: 太大不能合并, 但可重分配
            # Stage 2: 质心近 + keyword 混配, 但非全维度一致 → 可分性测试
            elif c_sim > realloc_centroid and cross > realloc_cross:
                if sep_margin <= realloc_margin and not too_large:
                    merge_pairs.append((a, b, metrics))
                elif sep_margin > realloc_margin:
                    realloc_pairs.append((a, b, metrics))

    # 合并组: 连通分量(MERGE 对的传递闭包)
    parent_uf = {}
    def find(x):
        while parent_uf.get(x, x) != x:
            parent_uf[x] = parent_uf.get(parent_uf[x], parent_uf[x])
            x = parent_uf[x]
        return x
    def union(x, y):
        parent_uf[find(x)] = find(y)
    for a, b, _ in merge_pairs:
        union(a, b)
    groups = {}
    for n in names:
        r = find(n)
        groups.setdefault(r, []).append(n)
    merge_groups = [g for g in groups.values() if len(g) > 1]

    if _own_conn:
        conn.close()
    return {
        "merge_groups": [{"hubs": g, "paths": [hubs[n]["path"] for n in g],
                          "total_keywords": sum(hubs[n]["kw_count"] for n in g)} for g in merge_groups],
        "realloc_pairs": [{"a": a, "b": b, **m} for a, b, m in realloc_pairs],
        "all_pairs": all_pairs,
        "stats": {"total_subdirs": len(names), "merge_groups": len(merge_groups),
                  "realloc_pairs": len(realloc_pairs)},
    }


def _reparent_children(conn, source_hub, target_hub):
    """把 source 的子节点重新指向 target(保持父子关系, 非姻亲)。
    边方向: parent --子方向--> child (subject=parent, object=child)。
    找 source 的子节点: subject=source 的子方向边, object 即子节点。
    """
    import yaml, re
    for row in conn.execute(
        "SELECT object FROM edges WHERE subject=? AND predicate='子方向'", (source_hub,)
    ).fetchall():
        child = row[0]
        conn.execute("DELETE FROM edges WHERE subject=? AND predicate='子方向' AND object=?", (source_hub, child))
        conn.execute(
            "INSERT OR IGNORE INTO edges (subject, predicate, object, confidence, source, is_sr) "
            "VALUES (?,?,?,?,?,?)", (target_hub, "子方向", child, "[可追溯]", "", 0))
    for child_row in conn.execute(
        "SELECT object FROM edges WHERE subject=? AND predicate='子方向'", (target_hub,)
    ).fetchall():
        child_path = child_row[0]
        child_file = REPO / (child_path + ".md")
        if not child_file.exists():
            continue
        text = child_file.read_text(encoding="utf-8")
        m = re.match(r"^(---\n)(.*?)(\n---\n)", text, re.S)
        if not m:
            continue
        try:
            fm = yaml.safe_load(m.group(2)) or {}
        except Exception:
            continue
        if fm.get("parent") == source_hub:
            fm["parent"] = target_hub
            new_fm = yaml.dump(fm, allow_unicode=True, default_flow_style=False, sort_keys=False)
            child_file.write_text(m.group(1) + new_fm + m.group(3) + text[m.end():], encoding="utf-8")

def execute_merge_group(conn, group_hubs):
    """执行一个合并组: 选 keyword 最多的为 target, 其余合并进去。
    子节点 reparent 到 target, 然后调 merge_hubs。
    返回 [{source, target, keywords_moved}]。
    """
    results = []
    hub_kw_counts = []
    for h in group_hubs:
        kws = parse_hub_keywords(h)
        hub_kw_counts.append((h, len(kws)))
    hub_kw_counts.sort(key=lambda x: -x[1])
    target = hub_kw_counts[0][0]
    for source, _ in hub_kw_counts[1:]:
        _reparent_children(conn, source, target)
        r = merge_hubs(conn, source, target)
        results.append({"source": source, "target": target, **r})
    sync_hub_seeds_if_drift(target, drift_ratio=0.0)
    return results


def execute_realloc(conn, hub_a, hub_b, margin=0.01):
    """重分配: 合并两 hub keywords, 按 centroid 归属重新分配, 重选 seeds。
    不删 hub, 只移动错配 keyword + 重选 seeds。
    如果重分配后某 hub keyword < 3, 则改为合并(分离失败)。
    """
    from embed_helper import embed_cached_batch
    import numpy as np
    import direction_matcher as dm
    from cluster_keywords import add_keywords_to_hub, remove_keyword_from_hub

    dm._DIR_VECS = None
    dm.ensure_direction_embeddings(force=False)

    kws_a = parse_hub_keywords(hub_a)
    kws_b = parse_hub_keywords(hub_b)
    if not kws_a or not kws_b:
        return {"moved_a_to_b": 0, "moved_b_to_a": 0, "fallback_merge": False}
    all_kws = list(dict.fromkeys(kws_a + kws_b))
    expand = [dm.expand_keyword(k) for k in all_kws]
    kvecs = embed_cached_batch(expand, cache_type="keyword")
    kvecs_u = kvecs / (np.linalg.norm(kvecs, axis=1, keepdims=True) + 1e-9)
    a_idx = [i for i, k in enumerate(all_kws) if k in kws_a]
    b_idx = [i for i, k in enumerate(all_kws) if k in kws_b]
    cent_a = kvecs_u[a_idx].mean(axis=0)
    cent_b = kvecs_u[b_idx].mean(axis=0)
    cent_a /= np.linalg.norm(cent_a) + 1e-9
    cent_b /= np.linalg.norm(cent_b) + 1e-9

    moved_a_to_b = 0
    moved_b_to_a = 0
    for i, kw in enumerate(all_kws):
        sim_a = float(kvecs_u[i] @ cent_a)
        sim_b = float(kvecs_u[i] @ cent_b)
        if kw in kws_a and sim_b > sim_a + margin:
            remove_keyword_from_hub(hub_a + ".md", kw)
            add_keywords_to_hub(hub_b + ".md", [kw])
            moved_a_to_b += 1
        elif kw in kws_b and sim_a > sim_b + margin:
            remove_keyword_from_hub(hub_b + ".md", kw)
            add_keywords_to_hub(hub_a + ".md", [kw])
            moved_b_to_a += 1

    # 重分配后检查: 某 hub keyword 太少 → 分离失败, 改合并
    new_a = parse_hub_keywords(hub_a)
    new_b = parse_hub_keywords(hub_b)
    if len(new_a) < 3 or len(new_b) < 3:
        target = hub_a if len(new_a) >= len(new_b) else hub_b
        source = hub_b if target == hub_a else hub_a
        _reparent_children(conn, source, target)
        r = merge_hubs(conn, source, target)
        sync_hub_seeds_if_drift(target, drift_ratio=0.0)
        return {"moved_a_to_b": moved_a_to_b, "moved_b_to_a": moved_b_to_a,
                "fallback_merge": True, "merged": source, "into": target, **r}

    sync_hub_seeds_if_drift(hub_a, drift_ratio=0.0)
    sync_hub_seeds_if_drift(hub_b, drift_ratio=0.0)
    return {"moved_a_to_b": moved_a_to_b, "moved_b_to_a": moved_b_to_a, "fallback_merge": False}


def run_consolidation(max_iterations=15, **detect_kwargs):
    """检测→执行→重检测 循环, 直到无候选或达迭代上限。

    策略:
    1. 每轮先执行所有 MERGE 组(连通分量, 互不冲突)
    2. 再按 cross 降序尝试 REALLOC 对, 跳过 0 移动对(防死循环)
    3. 重检测, 循环
    """
    from graph_lib import GRAPH_DB
    conn = sqlite3.connect(str(GRAPH_DB))
    all_results = []
    skip_pairs = set()  # (a,b) pairs that had 0 moves, skip in future
    for iteration in range(max_iterations):
        plan = detect_hub_consolidation(conn=conn, **detect_kwargs)
        merge_groups = plan.get("merge_groups", [])
        realloc_pairs = plan.get("realloc_pairs", [])
        # 过滤掉已知的 0-移动对
        realloc_pairs = [rp for rp in realloc_pairs
                         if (rp["a"], rp["b"]) not in skip_pairs and (rp["b"], rp["a"]) not in skip_pairs]
        if not merge_groups and not realloc_pairs:
            break
        iter_result = {"iteration": iteration + 1, "merges": [], "reallocs": []}
        # 1. 执行所有 MERGE 组
        for mg in merge_groups:
            r = execute_merge_group(conn, mg["paths"])
            iter_result["merges"].append({"hubs": mg["hubs"], "result": r})
        conn.commit()
        # 2. 按 cross 降序尝试 REALLOC, 跳过 0 移动对
        for rp in sorted(realloc_pairs, key=lambda x: -x.get("cross", 0)):
            r = execute_realloc(conn, rp["path_a"], rp["path_b"])
            moved = r.get("moved_a_to_b", 0) + r.get("moved_b_to_a", 0)
            if moved == 0 and not r.get("fallback_merge"):
                skip_pairs.add((rp["a"], rp["b"]))
                continue  # 尝试下一对
            iter_result["reallocs"].append({"a": rp["a"], "b": rp["b"], "result": r})
            conn.commit()
            break  # 每轮只执行一个有效 REALLOC
        all_results.append(iter_result)
        if not iter_result["merges"] and not iter_result["reallocs"]:
            break
    conn.close()
    return all_results


def dedup_seeds():
    """种子全局互斥: 同一 seed 只保留在一个方向。
    规则:
    - 方向名(被 _load_direction_defs 动态加入): 根方向保留, 子方向移除
    - 深度不同: 深的保留(子方向 > 根方向)
    - 深度相同: seed embedding 对各方质心 cosine 最高的保留
    - 移除后 seeds < MIN_SEEDS 的 hub: 从关键词质心补选(排除已用 seed)
    """
    import sys, yaml, re
    sys.path.insert(0, str(REPO / ".scripts"))
    import direction_matcher as dm
    from embed_helper import embed_cached_batch
    import numpy as np

    dm._DIR_VECS = None
    dm.ensure_direction_embeddings(force=False)
    defs = dm._load_direction_defs()
    parents = dm._DIR_PARENTS or {}

    # All direction names (dynamically added as seeds by _load_direction_defs)
    dir_names = {d["name"].casefold() for d in defs}

    def get_depth(name):
        d = 0
        cur = name
        seen = set()
        while cur in parents and cur not in seen:
            seen.add(cur)
            cur = parents[cur]
            d += 1
        return d

    def get_hub_file(name):
        for f in (REPO / "academic" / "wiki" / "hubs").glob("*.md"):
            text = f.read_text(encoding="utf-8")
            m = re.search(r"^---\n(.*?)\n---", text, re.S)
            if m:
                try:
                    fm = yaml.safe_load(m.group(1)) or {}
                    if fm.get("title") == name:
                        return f
                except Exception:
                    pass
        return None

    # Build seed -> [(direction, depth, orig_text)] mapping
    seed_dirs = {}
    for d in defs:
        depth = get_depth(d["name"])
        for s in d.get("seeds", []):
            key = str(s).strip()
            if not key:
                continue
            seed_dirs.setdefault(key.casefold(), []).append((d["name"], depth, key))

    conflicts = {k: v for k, v in seed_dirs.items() if len(v) > 1}
    if not conflicts:
        return {"conflicts": 0, "resolved": 0}

    # For each conflict, determine owner
    removals = {}  # direction_name -> set of seeds to remove
    for seed_cf, dirs in conflicts.items():
        # Rule 1: if seed is a direction name, root direction keeps it
        if seed_cf in dir_names:
            owner = next((d[0] for d in dirs if d[1] == 0), dirs[0][0])
        else:
            max_depth = max(d[1] for d in dirs)
            deepest = [d for d in dirs if d[1] == max_depth]
            if len(deepest) == 1:
                owner = deepest[0][0]
            else:
                # Same depth: compare seed embedding to each direction's centroid
                seed_text = deepest[0][2]
                seed_vec = embed_cached_batch([seed_text], cache_type="keyword")[0]
                seed_u = seed_vec / (np.linalg.norm(seed_vec) + 1e-9)
                best_sim, owner = -1, deepest[0][0]
                for name, _, _ in deepest:
                    hf = get_hub_file(name)
                    if not hf:
                        continue
                    kws = parse_hub_keywords(f"academic/wiki/hubs/{hf.stem}")
                    if len(kws) < 2:
                        continue
                    kvecs = embed_cached_batch([dm.expand_keyword(k) for k in kws], cache_type="keyword")
                    cent = kvecs.mean(axis=0)
                    cent_u = cent / (np.linalg.norm(cent) + 1e-9)
                    sim = float(seed_u @ cent_u)
                    if sim > best_sim:
                        best_sim, owner = sim, name
        for name, _, orig_seed in dirs:
            if name != owner:
                removals.setdefault(name, set()).add(orig_seed)

    # Apply removals to YAML (root directions)
    yaml_path = REPO / "operations" / "config" / "arxiv-directions.yaml"
    yaml_data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    resolved = 0
    for direction in yaml_data.get("directions", []):
        name = direction.get("name", "")
        if name in removals:
            old_seeds = direction.get("seeds", [])
            new_seeds = [s for s in old_seeds if s not in removals[name]]
            resolved += len(old_seeds) - len(new_seeds)
            direction["seeds"] = new_seeds
    yaml_path.write_text(yaml.dump(yaml_data, allow_unicode=True, default_flow_style=False, sort_keys=False),
                         encoding="utf-8")

    # Apply removals to hub frontmatter (sub-directions)
    for name, seeds_to_remove in removals.items():
        hf = get_hub_file(name)
        if not hf:
            continue
        hub_rel = f"academic/wiki/hubs/{hf.stem}"
        old_seeds = _read_hub_seeds(hub_rel)
        new_seeds = [s for s in old_seeds if str(s).strip() not in seeds_to_remove]
        if len(new_seeds) < len(old_seeds):
            _update_hub_seeds(hub_rel, new_seeds)
            resolved += len(old_seeds) - len(new_seeds)

    # Refill: hubs with < MIN_SEEDS after dedup, excluding used seeds
    # Rebuild used seeds set from current defs (post-removal)
    dm._DIR_VECS = None
    defs2 = dm._load_direction_defs()
    all_used = set()
    for d in defs2:
        for s in d.get("seeds", []):
            all_used.add(str(s).strip().casefold())

    refilled = 0
    for name in removals:
        hf = get_hub_file(name)
        if not hf:
            continue
        hub_rel = f"academic/wiki/hubs/{hf.stem}"
        current_seeds = _read_hub_seeds(hub_rel)
        if len(current_seeds) >= MIN_SEEDS:
            continue
        kws = parse_hub_keywords(hub_rel)
        if len(kws) < 2:
            continue
        kvecs = embed_cached_batch([dm.expand_keyword(k) for k in kws], cache_type="keyword")
        cent = kvecs.mean(axis=0)
        cent_u = cent / (np.linalg.norm(cent) + 1e-9)
        kw_units = kvecs / (np.linalg.norm(kvecs, axis=1, keepdims=True) + 1e-9)
        sims = kw_units @ cent_u
        need = MIN_SEEDS - len(current_seeds)
        added = []
        for idx in np.argsort(sims)[::-1]:
            kw = kws[idx]
            if kw.casefold() in all_used:
                continue
            if kw in current_seeds:
                continue
            added.append(kw)
            all_used.add(kw.casefold())
            if len(added) >= need:
                break
        if added:
            _update_hub_seeds(hub_rel, current_seeds + added)
            refilled += len(added)

    return {"conflicts": len(conflicts), "resolved": resolved, "refilled": refilled}


def rename_hub(conn, old_path, new_name):
    """Rename a hub: file rename + graph.db path/edge update + child parent update.
    old_path: 'academic/wiki/hubs/子方向-xxxxx' (no .md)
    new_name: semantic name string
    Returns {old, new, children_updated, edges_updated}
    """
    import re, yaml
    old_file = REPO / (old_path + ".md")
    if not old_file.exists():
        return {"error": "old file not found"}
    new_path = f"academic/wiki/hubs/{new_name}"
    new_file = REPO / (new_path + ".md")

    # 1. Read & update file content (title, heading)
    text = old_file.read_text(encoding="utf-8")
    # Update frontmatter title (handle both quoted and unquoted)
    if re.search(r'^title:\s*".*"', text, re.M):
        text = re.sub(r'^(title:\s*").*(")', f'\\g<1>{new_name}\\g<2>', text, count=1, flags=re.M)
    else:
        text = re.sub(r'^(title:\s*).*', f'\\g<1>{new_name}', text, count=1, flags=re.M)
    # Update heading
    text = re.sub(r'^# .+$', f'# {new_name}', text, count=1, flags=re.M)

    # Handle name collision: if new file already exists, merge instead
    if new_file.exists() and new_file != old_file:
        # Merge old into existing new
        from cluster_keywords import add_keywords_to_hub
        old_kws = parse_hub_keywords(old_path)
        if old_kws:
            add_keywords_to_hub(new_path + ".md", old_kws)
        # Merge graph edges
        for row in conn.execute("SELECT predicate, object FROM edges WHERE subject=?", (old_path,)).fetchall():
            conn.execute("INSERT OR IGNORE INTO edges (subject, predicate, object, confidence, source, is_sr) VALUES (?,?,?,?,?,?)",
                         (new_path, row[0], row[1], "[可追溯]", "", 0))
        for row in conn.execute("SELECT subject, predicate FROM edges WHERE object=?", (old_path,)).fetchall():
            conn.execute("INSERT OR IGNORE INTO edges (subject, predicate, object, confidence, source, is_sr) VALUES (?,?,?,?,?,?)",
                         (row[0], row[1], new_path, "[可追溯]", "", 0))
        conn.execute("DELETE FROM edges WHERE subject=? OR object=?", (old_path, old_path))
        conn.execute("DELETE FROM nodes WHERE path=?", (old_path,))
        old_file.unlink()
        conn.commit()
        return {"old": old_path, "new": new_path, "merged": True}

    # 2. Write to new file & delete old
    new_file.write_text(text, encoding="utf-8")
    old_file.unlink()

    # 3. Update graph.db: nodes path
    conn.execute("UPDATE nodes SET path=? WHERE path=?", (new_path, old_path))
    # Update edges (subject and object)
    conn.execute("UPDATE edges SET subject=? WHERE subject=?", (new_path, old_path))
    conn.execute("UPDATE edges SET object=? WHERE object=?", (new_path, old_path))
    # Update aliases
    conn.execute("UPDATE aliases SET node_path=? WHERE node_path=?", (new_path, old_path))

    # 4. Update child hubs' parent frontmatter
    children_updated = 0
    for row in conn.execute(
        "SELECT object FROM edges WHERE subject=? AND predicate='子方向'", (new_path,)
    ).fetchall():
        child_path = row[0]
        child_file = REPO / (child_path + ".md")
        if not child_file.exists():
            continue
        ct = child_file.read_text(encoding="utf-8")
        m = re.match(r"^(---\n)(.*?)(\n---\n)", ct, re.S)
        if not m:
            continue
        try:
            fm = yaml.safe_load(m.group(2)) or {}
        except Exception:
            continue
        if fm.get("parent") and old_path in str(fm["parent"]):
            fm["parent"] = new_path
            new_fm = yaml.dump(fm, allow_unicode=True, default_flow_style=False, sort_keys=False)
            child_file.write_text(m.group(1) + new_fm + m.group(3) + ct[m.end():], encoding="utf-8")
            children_updated += 1

    edges_updated = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE subject=? OR object=?", (new_path, new_path)
    ).fetchone()[0]
    conn.commit()
    return {"old": old_path, "new": new_path, "children_updated": children_updated,
            "edges_updated": edges_updated}


def rename_placeholder_hubs():
    """Find all placeholder hubs (子方向-xxxxx), generate semantic names via LLM, and rename.
    Returns [{old, new, result}].
    """
    import sqlite3
    from graph_lib import GRAPH_DB
    from cluster_keywords import llm_name_cluster
    import re

    conn = sqlite3.connect(str(GRAPH_DB))
    results = []

    # Find all placeholder hubs
    placeholders = []
    for f in sorted((REPO / "academic" / "wiki" / "hubs").glob("子方向-*.md")):
        stem = f.stem
        if re.match(r"^子方向-[0-9a-f]{6}$", stem):
            kws = parse_hub_keywords(f"academic/wiki/hubs/{stem}")
            placeholders.append({"path": f"academic/wiki/hubs/{stem}", "stem": stem, "kws": kws})

    # Sort by keyword count (most keywords first — more signal for LLM)
    placeholders.sort(key=lambda x: -len(x["kws"]))

    existing_names = set()
    for f in (REPO / "academic" / "wiki" / "hubs").glob("*.md"):
        if not re.match(r"^子方向-", f.stem):
            existing_names.add(f.stem)

    for ph in placeholders:
        kws = ph["kws"]
        if not kws:
            # Empty hub — skip (will be cleaned up separately)
            results.append({"old": ph["path"], "new": None, "error": "no keywords"})
            continue
        # Use top 20 keywords for naming
        sample = kws[:20]
        name = llm_name_cluster(sample)
        if not name or name == "未命名主题":
            results.append({"old": ph["path"], "new": None, "error": "LLM naming failed"})
            continue
        # Sanitize: remove quotes, brackets, and filename-unsafe chars
        name = re.sub(r'["""《》\n/\\:*?<>|]', '', name).strip()
        if not name or len(name) < 2:
            results.append({"old": ph["path"], "new": None, "error": "name too short after sanitize"})
            continue
        # Avoid collision with existing names
        if name in existing_names:
            # Merge into existing
            pass  # rename_hub will handle collision merge
        existing_names.add(name)

        r = rename_hub(conn, ph["path"], name)
        results.append({"old": ph["path"], "new": name, "result": r})

    conn.close()
    return results


def enforce_name_seed_exclusivity():
    """hub名与seed互斥: seed 匹配任意 hub 名(含自身)则移除,hub名优先保留。
    规则: hub名是身份标识,seed 是匹配锚点;二者重复时保留 hub名、移除 seed。
    移除后 seeds < MIN_SEEDS 的 hub 从关键词质心补选(排除已用 seed + 所有 hub名)。

    Returns {names_checked, seeds_removed, refilled, details}
    """
    import sys, yaml, re
    sys.path.insert(0, str(REPO / ".scripts"))
    import direction_matcher as dm
    from embed_helper import embed_cached_batch
    import numpy as np

    # Collect all hub titles (root from YAML + sub from hub frontmatter)
    yaml_path = REPO / "operations" / "config" / "arxiv-directions.yaml"
    yaml_data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))

    all_hub_names = {}  # name_casefold -> (name, source, is_root)
    for direction in yaml_data.get("directions", []):
        name = direction.get("name", "").strip()
        if name:
            all_hub_names[name.casefold()] = (name, "yaml", True)

    for f in (REPO / "academic" / "wiki" / "hubs").glob("*.md"):
        text = f.read_text(encoding="utf-8")
        m = re.search(r"^---\n(.*?)\n---", text, re.S)
        if m:
            try:
                fm = yaml.safe_load(m.group(1)) or {}
                name = fm.get("title", "").strip()
                if name:
                    all_hub_names[name.casefold()] = (name, f.stem, False)
            except Exception:
                pass

    if not all_hub_names:
        return {"names_checked": 0, "seeds_removed": 0}

    # Check & remove from YAML (root directions)
    seeds_removed = 0
    for direction in yaml_data.get("directions", []):
        name = direction.get("name", "")
        old_seeds = direction.get("seeds", [])
        new_seeds = []
        for s in old_seeds:
            s_cf = str(s).strip().casefold()
            # Rule: hub名与seed互斥——seed 匹配任意 hub 名(含自身)则移除,hub名优先保留
            if s_cf in all_hub_names:
                seeds_removed += 1
                continue
            new_seeds.append(s)
        if len(new_seeds) < len(old_seeds):
            direction["seeds"] = new_seeds

    yaml_path.write_text(yaml.dump(yaml_data, allow_unicode=True, default_flow_style=False, sort_keys=False),
                         encoding="utf-8")

    # Check & remove from hub frontmatter (sub-directions)
    refilled = 0
    details = []
    for f in (REPO / "academic" / "wiki" / "hubs").glob("*.md"):
        text = f.read_text(encoding="utf-8")
        m = re.search(r"^---\n(.*?)\n---", text, re.S)
        if not m:
            continue
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except Exception:
            continue
        hub_name = fm.get("title", "").strip()
        if not hub_name:
            continue
        hub_rel = f"academic/wiki/hubs/{f.stem}"
        old_seeds = _read_hub_seeds(hub_rel)
        if not old_seeds:
            continue
        new_seeds = []
        for s in old_seeds:
            s_cf = str(s).strip().casefold()
            # Rule: hub名与seed互斥——seed 匹配任意 hub 名(含自身)则移除,hub名优先保留
            if s_cf in all_hub_names:
                seeds_removed += 1
                continue
            new_seeds.append(s)
        if len(new_seeds) < len(old_seeds):
            _update_hub_seeds(hub_rel, new_seeds)
            details.append({"hub": hub_name, "removed": len(old_seeds) - len(new_seeds)})

            # Refill if below MIN_SEEDS
            if len(new_seeds) < MIN_SEEDS:
                kws = parse_hub_keywords(hub_rel)
                if len(kws) >= 2:
                    # Rebuild used seeds set
                    dm._DIR_VECS = None
                    dm.ensure_direction_embeddings(force=False)
                    defs = dm._load_direction_defs()
                    all_used = set()
                    for d in defs:
                        for ds in d.get("seeds", []):
                            all_used.add(str(ds).strip().casefold())
                    # Also add all hub names
                    for n_cf in all_hub_names:
                        all_used.add(n_cf)

                    kvecs = embed_cached_batch([dm.expand_keyword(k) for k in kws], cache_type="keyword")
                    cent = kvecs.mean(axis=0)
                    cent_u = cent / (np.linalg.norm(cent) + 1e-9)
                    kw_units = kvecs / (np.linalg.norm(kvecs, axis=1, keepdims=True) + 1e-9)
                    sims = kw_units @ cent_u
                    need = MIN_SEEDS - len(new_seeds)
                    added = []
                    for idx in np.argsort(sims)[::-1]:
                        kw = kws[idx]
                        if kw.casefold() in all_used or kw in new_seeds:
                            continue
                        added.append(kw)
                        all_used.add(kw.casefold())
                        if len(added) >= need:
                            break
                    if added:
                        _update_hub_seeds(hub_rel, new_seeds + added)
                        refilled += len(added)

    return {"names_checked": len(all_hub_names), "seeds_removed": seeds_removed,
            "refilled": refilled, "details": details}

def merge_duplicate_hubs():
    """合并同名 hub(不同文件但 title 相同 = 同概念重复)。
    规则: 同 title → 合并 keyword 少的进 keyword 多的; 血亲(父子)不合并。
    合并前 reparent 子节点, 防止 stale parent 引用。
    """
    import sqlite3, yaml, re
    from graph_lib import GRAPH_DB

    conn = sqlite3.connect(str(GRAPH_DB))

    title_files = {}
    for f in sorted((REPO / "academic" / "wiki" / "hubs").glob("*.md")):
        ftext = f.read_text(encoding="utf-8")
        m = re.search(r"^---\n(.*?)\n---", ftext, re.S)
        if not m:
            continue
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except Exception:
            continue
        title = fm.get("title", "").strip()
        if not title:
            continue
        parent = fm.get("parent")
        kws = parse_hub_keywords(f"academic/wiki/hubs/{f.stem}")
        title_files.setdefault(title.casefold(), []).append({
            "title": title, "stem": f.stem,
            "path": f"academic/wiki/hubs/{f.stem}",
            "kw_count": len(kws), "parent": parent,
        })

    results = []
    for title_cf, hubs in title_files.items():
        if len(hubs) < 2:
            continue
        hubs.sort(key=lambda x: -x["kw_count"])
        target = hubs[0]
        for source in hubs[1:]:
            def parent_chain(start_path):
                chain = set()
                cur = start_path
                seen = set()
                for _ in range(10):
                    if cur in seen:
                        break
                    seen.add(cur)
                    cf = REPO / (cur + ".md")
                    if not cf.exists():
                        break
                    ct = cf.read_text(encoding="utf-8")
                    cm = re.search(r"^---\n(.*?)\n---", ct, re.S)
                    if not cm:
                        break
                    try:
                        cfm = yaml.safe_load(cm.group(1)) or {}
                    except Exception:
                        break
                    p_val = cfm.get("parent")
                    if not p_val or not isinstance(p_val, str):
                        break
                    chain.add(p_val)
                    cur = p_val
                return chain

            t_chain = parent_chain(target["path"])
            s_chain = parent_chain(source["path"])
            if source["path"] in t_chain or target["path"] in s_chain:
                results.append({"skipped": "blood_relation",
                                "source": source["path"], "target": target["path"]})
                continue
            _reparent_children(conn, source["path"], target["path"])
            r = merge_hubs(conn, source["path"], target["path"])
            conn.commit()
            results.append({"source": source["path"], "target": target["path"],
                            "source_kws": source["kw_count"], "target_kws": target["kw_count"],
                            "result": r})

    conn.close()
    return results

if __name__ == "__main__":
    main()
