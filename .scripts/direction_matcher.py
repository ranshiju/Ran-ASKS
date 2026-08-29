#!/usr/bin/env python3
"""direction_matcher.py — keyword → arXiv 研究方向 embedding 匹配

研究方向判定权在程序侧(非 LLM):
- 一次性 embed 标准方向(名称+desc 关键词扩充语义),存 embeddings.db(type='arxiv-direction')
- 每次 ingest: 每个 keyword 取 embedding → 对方向向量算 cosine → top 匹配
- 阈值 ≥ DEFAULT_THRESHOLD → 入该方向 hub(可多归属);全低 → unmatched(catch-all)

设计: 方向稳定(固定集),向量缓存一次复用;keyword 语义匹配零 LLM token。
被 graph_ingest.py 在 LLM 未填研究方向时调用。
"""
import sys
import time
import sqlite3
import re
from pathlib import Path

import numpy as np
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".scripts"))
EMBED_DB = REPO / "cross-domain" / "embeddings.db"
CONFIG_PATH = REPO / "operations" / "config" / "arxiv-directions.yaml"
DIRECTION_TYPE = "arxiv-direction"
DEFAULT_THRESHOLD = 0.75
SUB_THRESHOLD = 0.65    # 子方向匹配阈值(更宽容:子方向seeds窄,关键词需更低阈值命中)
TOP_K = 3  # 每个 keyword 最多匹配几个方向
KEYWORD_DEDUP_THRESHOLD = 0.92  # keyword 去重: cosine ≥ 此值视为同一 keyword

_DIR_VECS = None  # {name: np.array}
_DIR_PARENTS = None  # {sub_name: parent_name}
_NEG_SEEDS = None  # {direction_name: [negative_seed, ...]}

# 方向定义进程级缓存: 同一摄入内多次调用 _load_direction_defs 只读一次配置 + 只报一次种子冲突。
# 签名基于 CONFIG_PATH 与所有 hub 文件的 mtime,配置变更时自动失效重读。
_CACHED_DEFS = None       # [{name, parent, seeds, ...}]
_CACHED_DEFS_SIG = None   # 签名字符串
_SEED_CONFLICTS = []      # [{seed, direction, conflicting}] 供 lint 读取,替代 stderr 刷屏


def _normalized_keyword_components(keyword):
    """Return normalized Chinese, English, and abbreviation components for a keyword."""
    text = str(keyword or "")
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]+", text))
    english_parts = re.findall(r"[A-Za-z][A-Za-z0-9_-]*(?:\s+[A-Za-z][A-Za-z0-9_-]*)*", text)
    normalized_english = [
        re.sub(r"[^a-z0-9]", "", part.casefold())
        for part in english_parts
    ]
    normalized_english = [part for part in normalized_english if part]
    abbreviations = {
        re.sub(r"[^a-z0-9]", "", item.casefold())
        for item in re.findall(r"[（(]\s*([A-Za-z][A-Za-z0-9_-]*)\s*[)）]", text)
    }
    abbreviations.discard("")
    english = " ".join(normalized_english)
    return {"chinese": chinese, "english": english, "abbreviations": abbreviations}


def _components_are_compatible(left, right, shared_component):
    """Reject a one-sided match when the other named component conflicts.

    An omitted translation is compatible. A shared abbreviation also permits a
    full-name versus abbreviation spelling difference.
    """
    other_component = "english" if shared_component == "chinese" else "chinese"
    left_other = left[other_component]
    right_other = right[other_component]
    if not left_other or not right_other or left_other == right_other:
        return True
    return bool(left["abbreviations"] & right["abbreviations"])


def exact_component_keyword_matches(new_keywords, existing_keywords):
    """Match unambiguous keywords sharing an exact Chinese or English component.

    A matching component alone is not enough when the other component names a
    different concept. Ambiguous matches intentionally fall through to the
    embedding stage rather than being merged automatically.
    """
    matches = {}
    existing_components = [
        (existing_keyword, _normalized_keyword_components(existing_keyword))
        for existing_keyword in existing_keywords
    ]
    for new_keyword in new_keywords:
        new_components = _normalized_keyword_components(new_keyword)
        candidates = []
        for existing_keyword, existing_component in existing_components:
            shared_chinese = (
                bool(new_components["chinese"])
                and new_components["chinese"] == existing_component["chinese"]
            )
            shared_english = (
                bool(new_components["english"])
                and new_components["english"] == existing_component["english"]
            )
            if not shared_chinese and not shared_english:
                continue
            compatible = (
                shared_chinese
                and _components_are_compatible(new_components, existing_component, "chinese")
            ) or (
                shared_english
                and _components_are_compatible(new_components, existing_component, "english")
            )
            if compatible:
                candidates.append(existing_keyword)
        if len(candidates) == 1:
            matches[new_keyword] = candidates[0]
    return matches


def has_conflicting_exact_component(new_keyword, existing_keywords):
    """Whether an exact component match exists but its other component conflicts."""
    new_components = _normalized_keyword_components(new_keyword)
    for existing_keyword in existing_keywords:
        existing_component = _normalized_keyword_components(existing_keyword)
        shared_chinese = (
            bool(new_components["chinese"])
            and new_components["chinese"] == existing_component["chinese"]
        )
        shared_english = (
            bool(new_components["english"])
            and new_components["english"] == existing_component["english"]
        )
        if shared_chinese and not _components_are_compatible(
            new_components, existing_component, "chinese"
        ):
            return True
        if shared_english and not _components_are_compatible(
            new_components, existing_component, "english"
        ):
            return True
    return False


def _compute_defs_signature():
    """计算方向定义签名: CONFIG_PATH mtime + 所有 hub 文件 mtime。"""
    import os
    parts = []
    try:
        parts.append(str(os.path.getmtime(CONFIG_PATH)))
    except OSError:
        parts.append("?")
    hubs_dir = REPO / "academic" / "wiki" / "hubs"
    if hubs_dir.exists():
        for f in sorted(hubs_dir.glob("*.md")):
            try:
                parts.append(f"{f.stem}:{os.path.getmtime(f)}")
            except OSError:
                pass
    return "|".join(parts)


def get_seed_conflicts():
    """返回种子冲突列表(供 ingest_check lint 汇总)。每次 _load_direction_defs 刷新时重置。"""
    return list(_SEED_CONFLICTS)


def _load_direction_defs():
    """加载方向定义: 根方向(arxiv-directions.yaml) + 子方向(hub frontmatter parent+seeds)。
    返回 [{name, parent, seeds}]。子方向的 parent 指向根方向名。
    层次:根方向来自配置表;子方向来自 hub_split 动态生成(frontmatter parent/seeds 字段)。
    缓存: 签名(CONFIG_PATH + hub 文件 mtime)未变时返回进程级缓存,避免重复 IO 与重复种子冲突告警。
    """
    global _CACHED_DEFS, _CACHED_DEFS_SIG
    sig = _compute_defs_signature()
    if _CACHED_DEFS is not None and _CACHED_DEFS_SIG == sig:
        return _CACHED_DEFS
    _SEED_CONFLICTS.clear()
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    defs = []
    for entry in cfg.get("directions", []):
        if isinstance(entry, dict):
            name = entry.get("name", "").strip()
            seeds = entry.get("seeds", []) or []
            if name and name not in seeds:
                seeds = [name] + list(seeds)
            defs.append({"name": name, "parent": None, "seeds": seeds})
        else:
            name = str(entry).strip()
            defs.append({"name": name, "parent": None, "seeds": [name]})
    # 扫子方向 hub(academic/wiki/hubs/*.md, frontmatter 含 parent + seeds)
    # 两遍法: Pass1 收集所有 stored seeds(不 auto-fill)→ Pass2 用完整 _used 集合 auto-fill
    # 防止单遍法因处理顺序导致 auto-fill 引入与后续 hub 的 seed 冲突(死循环)
    import re
    try:
        hubs_dir = REPO / "academic" / "wiki" / "hubs"
        if hubs_dir.exists():
            # Pass 1: 收集所有子方向 stored seeds(不 auto-fill)
            _pending = []  # [(sub_name, parent_name, seeds_raw, hub_rel, f_stem)]
            for f in hubs_dir.glob("*.md"):
                text = f.read_text(encoding="utf-8")
                fm_match = re.search(r"^---\n(.*?)\n---", text, re.S)
                if not fm_match:
                    continue
                try:
                    fm = yaml.safe_load(fm_match.group(1)) or {}
                except yaml.YAMLError:
                    print(f"[direction_matcher] WARN: 子方向 hub frontmatter 非法 YAML: {f.stem}", file=sys.stderr)
                    continue
                if not isinstance(fm, dict):
                    continue
                parent_path = fm.get("parent")
                if not parent_path:
                    continue  # 根方向或非研究方向 hub
                sub_name = fm.get("title")
                if not isinstance(parent_path, str) or not isinstance(sub_name, str) or not sub_name.strip():
                    print(f"[direction_matcher] WARN: 子方向 hub 缺字段(parent/title): {f.stem}", file=sys.stderr)
                    continue
                parent_name = Path(parent_path).stem if "/" in parent_path else parent_path
                seeds = fm.get("seeds", [])
                seeds_raw = [str(seed).strip() for seed in seeds if str(seed).strip()] if isinstance(seeds, list) else []
                neg_seeds = fm.get("negative_seeds", [])
                neg_seeds_raw = [str(ns).strip() for ns in neg_seeds if str(ns).strip()] if isinstance(neg_seeds, list) else []
                _pending.append((sub_name, parent_name, seeds_raw, f"academic/wiki/hubs/{f.stem}", f.stem, neg_seeds_raw))
            # Pass 2: 用完整 _used 集合 auto-fill(含所有 pending stored seeds + defs)
            for sub_name, parent_name, seeds_raw, hub_rel, f_stem, neg_seeds_raw in _pending:
                if len(seeds_raw) < 3:
                    try:
                        from hub_split import ensure_hub_seeds
                        _used = set()
                        for _d in defs:
                            for _s in _d.get("seeds", []):
                                _used.add(str(_s).strip())
                        # 含所有 pending stored seeds(防 auto-fill 选到其他 hub 已存 seed)
                        for _p in _pending:
                            for _s in _p[2]:
                                _used.add(str(_s).strip())
                            for _s in _p[5]:
                                _used.add(str(_s).strip())
                        seeds_raw = ensure_hub_seeds(hub_rel, exclude_seeds=_used)
                    except Exception:
                        pass
                if not seeds_raw:
                    print(f"[direction_matcher] WARN: 子方向 hub seeds 仍为空: {f_stem}", file=sys.stderr)
                if sub_name and sub_name not in [d["name"] for d in defs]:
                    d = {"name": sub_name, "parent": parent_name, "seeds": seeds_raw or [sub_name]}
                    if neg_seeds_raw:
                        d["negative_seeds"] = neg_seeds_raw
                    defs.append(d)
    except Exception:
        pass
    # 种子全局互斥校验: 同一 seed 不应出现在多个方向(否则 keyword 命中多方向)
    # 父子直系豁免: 子方向继承父方向种子属合理语义(parent 链可达),不报冲突
    _parent_map = {d["name"]: d.get("parent") for d in defs if d.get("name")}

    def _is_lineage_related(a, b):
        """a 与 b 是否为直系血缘(parent 链任一方向可达)。"""
        def _ascends_to(x, target):
            cur = x
            seen = set()
            while cur and cur not in seen:
                seen.add(cur)
                cur = _parent_map.get(cur)
                if cur == target:
                    return True
            return False
        return _ascends_to(a, b) or _ascends_to(b, a)

    _seed_owners = {}  # key -> [owner_names]
    for d in defs:
        for s in d.get("seeds", []):
            key = str(s).strip().casefold()
            if not key:
                continue
            existing = _seed_owners.setdefault(key, [])
            conflicting = [o for o in existing if not _is_lineage_related(d["name"], o)]
            if conflicting:
                _SEED_CONFLICTS.append({
                    "seed": s, "direction": d["name"], "conflicting": conflicting,
                })
            existing.append(d["name"])
    _CACHED_DEFS = defs
    _CACHED_DEFS_SIG = sig
    return defs


def direction_has_document_support(direction: str, text: str) -> bool:
    """Require a textual anchor before keyword embeddings may assign a paper direction.

    Keyword vectors are useful candidate recall, but generic methods (for example
    low-rank approximation) must not alone attach a paper to an unrelated field.
    """
    normalized = text.casefold()
    for definition in _load_direction_defs():
        if definition["name"] != direction:
            continue
        anchors = [definition["name"], *definition.get("seeds", [])]
        return any(len(anchor.strip()) >= 4 and anchor.casefold() in normalized for anchor in anchors)
    return False


def ensure_direction_embeddings(force=False):
    """确保标准方向的种子向量已缓存。返回 {name: np.array(shape=[S_i, dim])} (每方向多种子)。
    种子集 embedding:每个方向的 seeds 各取一向量,keyword 对该方向取 max cosine。
    同时构建 _DIR_PARENTS(子方向→父方向名),供 classify_keywords 父子感知。
    v8.2: 统一走 embed_cached_batch(文本→向量去重),不再手写 type 缓存。
    """
    global _DIR_VECS, _DIR_PARENTS, _NEG_SEEDS, _CACHED_DEFS, _CACHED_DEFS_SIG
    if _DIR_VECS is not None and not force:
        return _DIR_VECS
    if force:
        _CACHED_DEFS = None
        _CACHED_DEFS_SIG = None
    defs = _load_direction_defs()
    _DIR_PARENTS = {d["name"]: d.get("parent") for d in defs if d.get("parent")}
    _NEG_SEEDS = {d["name"]: d.get("negative_seeds", []) for d in defs if d.get("negative_seeds")}
    names = [d["name"] for d in defs]
    # 展平所有种子去重 embed
    all_seeds = []
    seed_owner = []  # [(name, seed_text)]
    for d in defs:
        for s in d["seeds"]:
            all_seeds.append(s)
            seed_owner.append((d["name"], s))
    # 去重 embed(同 seed 文本只 embed 一次)
    seen = {}
    unique = []
    for s in all_seeds:
        if s not in seen:
            seen[s] = len(unique)
            unique.append(s)
    # v8.2: force 时先删这些文本缓存,再走 embed_cached_batch(全 miss 重算写回)
    if force:
        db = sqlite3.connect(EMBED_DB)
        ph = ",".join("?" * len(unique))
        db.execute(f"DELETE FROM embeddings WHERE text IN ({ph})", unique)
        db.commit()
        db.close()
    from embed_helper import embed_cached_batch
    vecs = embed_cached_batch(unique, cache_type="arxiv-direction")
    uniq_vecs = {unique[i]: vecs[i] for i in range(len(unique))}
    # 聚合: name → [seed vectors]
    _DIR_VECS = {}
    for nm, st in seed_owner:
        _DIR_VECS.setdefault(nm, []).append(uniq_vecs[st])
    _DIR_VECS = {nm: np.array(vs, dtype=np.float32) for nm, vs in _DIR_VECS.items()}
    return _DIR_VECS


_PAREN_RE = None


def expand_keyword(kw):
    """keyword embedding 展开:
    - 新格式「中文英文(缩写)」: 去括号,用主体"中文英文"做 embedding
    - 旧格式「缩写(英文全文/中文)」: 取括号内释义做 embedding(向后兼容)
    - 裸缩写(无括号)原样返回(交由 ingest_check 报 WARN)
    - 纯中文/纯英文无括号时原样返回
    """
    import re
    global _PAREN_RE
    if _PAREN_RE is None:
        _PAREN_RE = re.compile(r"[（(]([^)）]*)[)）]")
    m = _PAREN_RE.search(kw)
    if not m:
        return kw
    inner = m.group(1)
    main = _PAREN_RE.sub("", kw).strip()
    # 旧格式判定: 括号内含 / 分隔,或主体是裸缩写(短+大写)
    if "/" in inner or "／" in inner:
        parts = re.split(r"[/／]", inner)
        parts = [p.strip() for p in parts if p.strip()]
        if parts:
            return " ".join(parts)
    if main and len(main) <= 10 and re.search(r"[A-Z]{2,}", main):
        # 主体是裸缩写(旧格式如 MNIST（手写数字数据集）),用括号内释义
        return inner if inner else main
    # 新格式: 主体是中文英文,括号内是缩写 → 用主体
    return main if main else inner


def _is_neg_excluded(keyword, neg_seeds):
    """检查 keyword 是否被方向的负向 seed 排除。
    判据：keyword 与某 negative_seed 精确匹配或子串包含 → 排除。
    确定性检查，无 embedding 成本。"""
    if not neg_seeds:
        return False
    kw = keyword.strip()
    for ns in neg_seeds:
        ns = ns.strip()
        if ns and (ns == kw or ns in kw or kw in ns):
            return True
    return False


def classify_keywords(keywords, threshold=DEFAULT_THRESHOLD, top_k=None, sub_threshold=SUB_THRESHOLD, candidate_dirs=None):
    """批量分类 keywords → 研究方向。一个 keyword 可属多个领域(阈值过滤全保留,不截断)。

    返回 (kw_dirs, dir_keywords, unmatched):
    - kw_dirs: {keyword: [(direction, score), ...]} 该keyword命中的所有方向(降序,均≥threshold)
    - dir_keywords: {direction: [keywords]} 反向聚合(方向→命中的keywords)
    - unmatched: [keywords 全低于阈值]
    - top_k: 可选,截断每 keyword 最多保留几个方向(默认 None=不截断,全保留≥阈值的)
    - candidate_dirs: 可选,限制匹配的方向集合(如本论文研究方向及其后代)。None=所有方向。
    """
    dir_vecs = ensure_direction_embeddings()
    if not dir_vecs or not keywords:
        return {}, {}, list(keywords)
    from embed_helper import embed_cached_batch, cosine_sim

    if candidate_dirs is not None:
        names = [n for n in dir_vecs.keys() if n in candidate_dirs]
    else:
        names = list(dir_vecs.keys())
    # keyword embedding: 新格式取主体(中文英文),旧格式取括号内释义; keyword 向量走 db 缓存复用
    expand_texts = [expand_keyword(kw) for kw in keywords]
    kvecs = embed_cached_batch(expand_texts, cache_type="keyword")
    if kvecs.ndim == 1:
        kvecs = kvecs[None, :]

    # 近亲互斥(两层): 直接父子互斥 + 祖孙互斥, 后代优先; 曾孙及以上不互斥(可多归属)
    parents = _DIR_PARENTS or {}
    # 反向: 父→子列表
    children_of = {}
    for sub, par in parents.items():
        children_of.setdefault(par, []).append(sub)
    # 近亲互斥: 祖父→孙列表(两层后代)
    grandchildren_of = {}
    for sub, par in parents.items():
        grandparent = parents.get(par)
        if grandparent:
            grandchildren_of.setdefault(grandparent, []).append(sub)

    kw_dirs = {}
    dir_keywords = {}
    unmatched = []
    # 判别性匹配: 子方向分数 = raw - alpha * 兄弟最高分(抑制同父多命中)
    DISC_ALPHA = 0.3
    for i, kw in enumerate(keywords):
        pairs = []
        for nm in names:
            seed_mat = dir_vecs[nm]  # [S, dim]
            sims = cosine_sim(kvecs[i], seed_mat)  # [S]
            raw = float(sims.max())
            # 判别性分数: 子方向减去同父兄弟最高分(根方向不变)
            if nm in parents:
                par = parents[nm]
                siblings = [s for s in children_of.get(par, []) if s != nm and s in dir_vecs]
                if siblings:
                    sib_mats = [dir_vecs[s] for s in siblings]
                    sib_max = max(float(cosine_sim(kvecs[i], sm).max()) for sm in sib_mats)
                    disc = raw - DISC_ALPHA * sib_max
                else:
                    disc = raw
            else:
                disc = raw
            pairs.append((nm, disc, raw))
        pairs.sort(key=lambda x: -x[1])
        # 高置信直归属: 原始 max cosine > 0.95 → 直接归属(跳过阈值/互斥/截断)
        if pairs and pairs[0][2] > 0.95:
            kw_dirs[kw] = [(pairs[0][0], pairs[0][2])]
            dir_keywords.setdefault(pairs[0][0], []).append(kw)
            continue
        # 阈值过滤: 用判别性分数; 子方向用 sub_threshold,根方向用 threshold
        sub_names = set(parents.keys())
        matched_all = [(n, sc) for n, sc, _ in pairs if sc >= (sub_threshold if n in sub_names else threshold)]
        # 负向 seed 排除：keyword 被某方向的 negative_seeds 精确/子串命中 → 移除该方向
        if matched_all and _NEG_SEEDS:
            matched_all = [(n, sc) for n, sc in matched_all
                           if not _is_neg_excluded(kw, _NEG_SEEDS.get(n, []))]
        if top_k:
            matched_all = matched_all[:top_k]
        if not matched_all:
            unmatched.append(kw)
            continue
        # 近亲互斥(两层): 直接父子互斥 + 祖孙互斥, 后代优先; 曾孙及以上不互斥
        matched_names = {n for n, _ in matched_all}
        after_parent = []
        for n, sc in matched_all:
            subs = children_of.get(n, [])
            if subs and any(s in matched_names for s in subs):
                continue  # 层1: 直接子互斥→跳过父(归子)
            grands = grandchildren_of.get(n, [])
            if grands and any(g in matched_names for g in grands):
                continue  # 层2: 孙互斥→跳过祖父(归孙)
            after_parent.append((n, sc))
        # 互斥2(同父的子-子):按 parent 分组,同组只留最高分。
        # 根方向(parent=None)不互斥(不同根方向可多归属,如 cMPS 同时属数学物理+强关联)。
        groups = {}
        for n, sc in after_parent:
            par = parents.get(n)
            groups.setdefault(par, []).append((n, sc))
        matched = []
        for par, items in groups.items():
            if par is None:
                matched.extend(items)  # 根方向多归属保留
            else:
                matched.append(max(items, key=lambda x: x[1]))  # 同父的子取 max
        if matched:
            kw_dirs[kw] = matched
            for n, _s in matched:
                dir_keywords.setdefault(n, []).append(kw)
        else:
            unmatched.append(kw)
    return kw_dirs, dir_keywords, unmatched


def collect_all_hub_keywords():
    """收集所有 hub 页 ## 关键词 段的全部 keyword(去重)。
    扫描 academic/wiki/hubs/*.md,复用 hub_split.parse_hub_keywords。
    """
    from hub_split import parse_hub_keywords
    hubs_dir = REPO / "academic" / "wiki" / "hubs"
    if not hubs_dir.exists():
        return []
    all_kws = set()
    for md in hubs_dir.glob("*.md"):
        kws = parse_hub_keywords(str(md.relative_to(REPO)))
        all_kws.update(kws)
    return list(all_kws)


def match_keyword_to_hub_keywords(new_keywords, existing_keywords=None,
                                  threshold=KEYWORD_DEDUP_THRESHOLD):
    """分级 keyword 匹配: 精确中英字段优先，embedding 作为后备。

    中文或英文完全一致时，只有另一侧为空、相同或共享缩写才自动合并；
    另一侧冲突或存在多个候选时保留给 embedding/人工消歧。
    返回 {new_kw: matched_existing_kw}（仅含命中的）; 未命中的不在 dict 中。
    先做精确匹配(零 API 成本),再对剩余做批量 embedding。
    """
    if not new_keywords:
        return {}
    if existing_keywords is None:
        existing_keywords = collect_all_hub_keywords()
    if not existing_keywords:
        return {}
    from embed_helper import embed_cached_batch, cosine_sim
    # 完整字符串相等及中英字段精确匹配（零 API 成本）
    existing_set = set(existing_keywords)
    matches = exact_component_keyword_matches(new_keywords, existing_keywords)
    remaining = []
    for kw in new_keywords:
        if kw in existing_set:
            matches[kw] = kw
        elif kw not in matches:
            if not has_conflicting_exact_component(kw, existing_keywords):
                remaining.append(kw)
    if not remaining:
        return matches
    # 批量 embedding: 新 keyword + 已有 keyword
    expand_new = [expand_keyword(kw) for kw in remaining]
    expand_existing = [expand_keyword(kw) for kw in existing_keywords]
    all_texts = expand_new + expand_existing
    vecs = embed_cached_batch(all_texts)
    if vecs.ndim == 1:
        vecs = vecs[None, :]
    new_vecs = vecs[:len(remaining)]
    existing_vecs = vecs[len(remaining):]
    for i, kw in enumerate(remaining):
        sims = cosine_sim(new_vecs[i], existing_vecs)
        best_idx = int(np.argmax(sims))
        if sims[best_idx] >= threshold:
            matches[kw] = existing_keywords[best_idx]
    return matches


if __name__ == "__main__":
    # 自测:配 --test <kw1> <kw2> ...
    import argparse
    ap = argparse.ArgumentParser(description="keyword→研究方向匹配(自测)")
    ap.add_argument("--test", nargs="+", help="待测 keywords")
    ap.add_argument("--force", action="store_true", help="强制重 embed 方向向量")
    args = ap.parse_args()
    ensure_direction_embeddings(force=args.force)
    if args.test:
        kw_dirs, dir_keywords, unmatched = classify_keywords(args.test)
        print("\n=== 逐 keyword 匹配 ===")
        for kw, dirs in kw_dirs.items():
            ds = ", ".join(f"{d}({s:.3f})" for d, s in dirs)
            print(f"  {kw} → {ds}")
        if unmatched:
            print(f"\n=== 未匹配(catch-all)===\n  {unmatched}")
        print("\n=== 方向聚合 ===")
        for d, kws in sorted(dir_keywords.items()):
            print(f"  {d}: {kws}")
