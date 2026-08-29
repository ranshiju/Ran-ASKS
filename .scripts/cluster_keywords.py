#!/usr/bin/env python3
"""cluster_keywords.py — catch-all 队列 keyword 聚类拆分

动态 hub 生长机制：catch-all 队列的 ## 关键词 段达阈值时，
embedding + 层次聚类 → 最大簇拆出建新 hub → LLM 语义命名 → 查重合并。

用法:
  python3 .scripts/cluster_keywords.py --analyze           # 分析+输出建议(不执行)
  python3 .scripts/cluster_keywords.py --apply             # 执行拆分(建hub+迁移keyword)
  python3 .scripts/cluster_keywords.py --analyze --hub academic/wiki/hubs/未归类关键词.md
  python3 .scripts/cluster_keywords.py --status            # 查所有hub keyword数
"""
import sys, os, json, urllib.request, re, time
from pathlib import Path
import numpy as np
from scipy.cluster.hierarchy import linkage, fcluster
from scipy.spatial.distance import pdist

sys.path.insert(0, str(Path(__file__).parent))
from embed_helper import embed_batch, cosine_sim

BASE = Path(__file__).resolve().parent.parent
GRAPH_DB = BASE / "cross-domain" / "graph.db"
CATCH_ALL_QUEUE = BASE / "academic/wiki/hubs/未归类关键词.md"

# ── 配置 ──
TRIGGER_THRESHOLD = 100      # catch-all 队列 keyword 数达此值触发聚类
SPLIT_SIZE = 30               # 拆出最大簇的 size 上限
MERGE_THRESHOLD = 0.85        # 质心 cosine_sim > 此值 → 提议合并
MERGE_SIZE_LIMIT = 100        # 合并后 hub keyword 数上限

def _load_env():
    env = {}
    p = BASE / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

_ENV = _load_env()
_API_BASE = _ENV.get("LLM_API_BASE", "")
_API_KEY = _ENV.get("LLM_API_KEY", "")
_MODEL = _ENV.get("LLM_MODEL", "")

def llm_name_cluster(keywords):
    """让 LLM 根据簇内 keywords 给一个简短中文主题名"""
    kw_str = "\n".join(f"- {k}" for k in keywords)
    prompt = f"""以下是聚类得到的一组关键词，请根据它们的语义给出一个简短的中文主题名称（4-10字，不带书名号或引号）。

关键词：
{kw_str}

只输出主题名称，不要其他内容。"""
    payload_data = {
        "model": _MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 800, "temperature": 0.1,
    }
    # DeepSeek-V4-Flash 推理模型需限制推理预算
    reasoning_field = _ENV.get("LLM_REASONING_FIELD", "")
    if reasoning_field:
        payload_data[reasoning_field] = _ENV.get("LLM_REASONING_EFFORT_LOW", "low")
    payload = json.dumps(payload_data).encode()
    # 重试 2 次（与 embed_batch 一致），限流/瞬时网络故障可恢复
    last_err = None
    for attempt in range(2):
        req = urllib.request.Request(
            f"{_API_BASE}/v1/chat/completions", data=payload,
            headers={"Authorization": f"Bearer {_API_KEY}", "Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                name = json.loads(resp.read())["choices"][0]["message"]["content"].strip()
                name = re.sub(r'["""《》\n]', '', name).strip()
                return name if name else "未命名主题"
        except Exception as e:
            last_err = e
            if attempt == 0:
                print(f"[llm_name] 第1次失败: {e}, 重试...", file=__import__('sys').stderr)
                time.sleep(1)
    print(f"[llm_name] 2次均失败: {last_err}")
    return None

def parse_hub_keywords(hub_path):
    """解析 hub 页正文 ## 关键词 段的 keyword 列表"""
    if not os.path.exists(hub_path):
        return []
    with open(hub_path) as f:
        content = f.read()
    m = re.search(r'## 关键词\s*\n(.*?)(?=\n## |\Z)', content, re.DOTALL)
    if not m:
        return []
    kws = []
    for line in m.group(1).strip().splitlines():
        line = line.strip()
        if line and not line.startswith('<!--') and not line.startswith('#'):
            # 支持 "- keyword" 或 "keyword" 或 "| keyword |"
            cleaned = re.sub(r'^[-*]\s*', '', line).strip()
            cleaned = cleaned.split('|')[0].strip() if '|' in cleaned else cleaned
            if cleaned:
                kws.append(cleaned)
    return kws

def remove_keywords_from_hub(hub_path, keywords_to_remove):
    """从 hub 正文 ## 关键词 段移除指定 keyword"""
    with open(hub_path) as f:
        content = f.read()
    m = re.search(r'(## 关键词\s*\n)(.*?)(?=\n## |\Z)', content, re.DOTALL)
    if not m:
        return
    section_body = m.group(2)
    for kw in keywords_to_remove:
        # 移除含该 kw 的行
        section_body = '\n'.join(
            line for line in section_body.splitlines()
            if kw not in line
        )
    content = content[:m.start(2)] + section_body + content[m.end(2):]
    with open(hub_path, 'w') as f:
        f.write(content)

def remove_keyword_from_hub(hub_path, keyword):
    """从 hub 正文 ## 关键词 段精确移除单个 keyword(行清理后完全匹配)。"""
    with open(hub_path) as f:
        content = f.read()
    m = re.search(r'(## 关键词\s*\n)(.*?)(?=\n## |\Z)', content, re.DOTALL)
    if not m:
        return
    section_body = m.group(2)
    lines = section_body.splitlines()
    new_lines = []
    for line in lines:
        cleaned = re.sub(r'^[-*]\s*', '', line).strip()
        if cleaned == keyword:
            continue
        new_lines.append(line)
    new_section = '\n'.join(new_lines)
    content = content[:m.start(2)] + new_section + content[m.end(2):]
    with open(hub_path, 'w') as f:
        f.write(content)

def add_keywords_to_hub(hub_path, keywords):
    """向 hub 正文 ## 关键词 段追加 keyword（去重）"""
    with open(hub_path) as f:
        content = f.read()
    existing = set(parse_hub_keywords(hub_path))
    new_kws = [k for k in keywords if k not in existing]
    if not new_kws:
        return
    m = re.search(r'(## 关键词\s*\n)', content)
    if m:
        insert_pos = m.end()
        addition = ''.join(f'- {k}\n' for k in new_kws)
        content = content[:insert_pos] + addition + content[insert_pos:]
    else:
        # 无 ## 关键词 段，追加
        addition = '\n## 关键词\n\n' + ''.join(f'- {k}\n' for k in new_kws)
        content = content.rstrip() + '\n' + addition
    with open(hub_path, 'w') as f:
        f.write(content)

def create_hub_page(hub_name, keywords, subproject="academic"):
    """创建新 hub 页"""
    hub_path = BASE / subproject / "wiki" / "hubs" / f"{hub_name}.md"
    hub_path.parent.mkdir(parents=True, exist_ok=True)
    kw_list = '\n'.join(f'- {k}' for k in keywords)
    content = f"""---
title: "{hub_name}"
type: topic-hub
status: active
created: {time.strftime("%Y-%m-%d")}
updated: {time.strftime("%Y-%m-%d")}
---

# {hub_name}

> 动态聚类生成 hub（{time.strftime("%Y-%m-%d")}）

## 关键词

{kw_list}
"""
    hub_path.write_text(content)
    return str(hub_path.relative_to(BASE))

def get_all_keyword_hubs():
    """获取所有含 ## 关键词 段的 hub 页（含 catch-all）"""
    hubs = {}
    for sub in ["academic", "admin", "business", "teaching"]:
        hubs_dir = BASE / sub / "wiki" / "hubs"
        if not hubs_dir.exists():
            continue
        for f in hubs_dir.glob("*.md"):
            kws = parse_hub_keywords(str(f))
            if kws:
                rel = str(f.relative_to(BASE))
                hubs[rel] = kws
    return hubs

def cluster_analysis(catch_all_path):
    """聚类分析：返回 {cluster_id: {keywords, name, centroid}}"""
    keywords = parse_hub_keywords(catch_all_path)
    if len(keywords) < TRIGGER_THRESHOLD:
        print(f"keyword 数 {len(keywords)} < {TRIGGER_THRESHOLD}，未达聚类阈值")
        return None

    print(f"catch-all 队列 keyword 数: {len(keywords)}，开始聚类...")
    # 1. 批量 embedding
    embs = embed_batch(keywords)
    # 2. 层次聚类
    dists = pdist(embs, metric='cosine')
    Z = linkage(dists, method='average')
    # 切分：尝试不同 t 值，找最大簇 ≤ SPLIT_SIZE 的切分
    best_clusters = None
    for t in np.linspace(0.3, 0.8, 11):
        labels = fcluster(Z, t=t, criterion='distance')
        clusters = {}
        for kw, label in zip(keywords, labels):
            clusters.setdefault(label, []).append(kw)
        # 最大簇
        sizes = sorted(len(v) for v in clusters.values())
        if sizes and sizes[-1] <= SPLIT_SIZE and len(clusters) > 1:
            best_clusters = clusters
            break
    if not best_clusters:
        # 兜底：强制切分到每簇 ≤ SPLIT_SIZE
        labels = fcluster(Z, t=0.5, criterion='distance')
        best_clusters = {}
        for kw, label in zip(keywords, labels):
            best_clusters.setdefault(label, []).append(kw)

    # 3. 取最大簇 + LLM 命名
    sorted_clusters = sorted(best_clusters.values(), key=len, reverse=True)
    largest = sorted_clusters[0]
    if len(largest) > SPLIT_SIZE:
        largest = largest[:SPLIT_SIZE]
    # 检查是否还有值得拆出的簇（≥5 个 keyword）
    splittable = [c for c in sorted_clusters if len(c) >= 5]

    result = []
    for cluster_kws in splittable:
        if len(cluster_kws) > SPLIT_SIZE:
            cluster_kws = cluster_kws[:SPLIT_SIZE]
        # embedding 质心
        cluster_embs = embs[[keywords.index(k) for k in cluster_kws]]
        centroid = cluster_embs.mean(axis=0)
        # LLM 命名
        name = llm_name_cluster(cluster_kws)
        result.append({
            'keywords': cluster_kws,
            'name': name,
            'centroid': centroid,
            'size': len(cluster_kws)
        })
    return result

def check_merge(new_cluster, all_hubs):
    """查重合并：新簇质心 vs 已有 hub keyword 集合质心"""
    new_name = new_cluster['name']
    new_centroid = new_cluster['centroid']
    new_kws = set(new_cluster['keywords'])

    for hub_path, hub_kws in all_hubs.items():
        if Path(hub_path).name == CATCH_ALL_QUEUE.name:
            continue  # 不和 catch-all 合并
        # 已有 hub 质心
        hub_embs = embed_batch(hub_kws)
        hub_centroid = hub_embs.mean(axis=0)
        sim = cosine_sim(new_centroid, hub_centroid)[0]
        combined_size = len(new_kws | set(hub_kws))
        if sim > MERGE_THRESHOLD:
            return {
                'merge_with': hub_path,
                'similarity': float(sim),
                'combined_size': combined_size,
                'can_merge': combined_size <= MERGE_SIZE_LIMIT
            }
    return None

def cmd_analyze(catch_all_path=None):
    if not catch_all_path:
        catch_all_path = str(CATCH_ALL_QUEUE)
    print(f"=== 聚类分析: {catch_all_path} ===")
    clusters = cluster_analysis(catch_all_path)
    if not clusters:
        return
    all_hubs = get_all_keyword_hubs()
    print(f"\n聚类得到 {len(clusters)} 个可拆分簇:")
    for i, c in enumerate(clusters):
        print(f"\n--- 簇 {i+1} ({c['size']} keywords) → 建议命名: {c['name']} ---")
        for kw in c['keywords']:
            print(f"  {kw}")
        merge = check_merge(c, all_hubs)
        if merge:
            action = "合并" if merge['can_merge'] else "超限不合并(区别命名)"
            print(f"  ⚠ 查重: 与 {merge['merge_with']} 相似度 {merge['similarity']:.3f} → {action}")
        else:
            print(f"  ✓ 无重合 hub，新建")

def cmd_apply(catch_all_path=None):
    if not catch_all_path:
        catch_all_path = str(CATCH_ALL_QUEUE)
    print(f"=== 执行聚类拆分: {catch_all_path} ===")
    clusters = cluster_analysis(catch_all_path)
    if not clusters:
        return
    all_hubs = get_all_keyword_hubs()
    created = []
    merged = []
    for c in clusters:
        merge = check_merge(c, all_hubs)
        if merge and merge['can_merge']:
            # 合并到已有 hub
            add_keywords_to_hub(str(BASE / merge['merge_with']), c['keywords'])
            remove_keywords_from_hub(catch_all_path, c['keywords'])
            merged.append((c['name'], merge['merge_with'], c['keywords']))
            all_hubs[merge['merge_with']] = list(set(all_hubs.get(merge['merge_with'], [])) | set(c['keywords']))
        else:
            # 新建 hub
            hub_name = c['name']
            rel_path = create_hub_page(hub_name, c['keywords'])
            # graph.db 加 hub 节点
            import sqlite3
            conn = sqlite3.connect(str(GRAPH_DB))
            conn.execute(
                "INSERT OR IGNORE INTO nodes (path,title,type,source_type,date,status,has_raw_source) VALUES (?,?,?,?,?,?,?)",
                (rel_path, hub_name, 'hub', '', time.strftime("%Y-%m-%d"), 'active', 0)
            )
            conn.commit()
            conn.close()
            remove_keywords_from_hub(catch_all_path, c['keywords'])
            created.append((hub_name, rel_path, c['keywords']))
            all_hubs[rel_path] = c['keywords']
    print(f"\n=== 结果 ===")
    print(f"新建 hub: {len(created)}")
    for name, path, kws in created:
        print(f"  {name} ({path}) ← {len(kws)} keywords")
    print(f"合并到已有 hub: {len(merged)}")
    for name, target, kws in merged:
        print(f"  {name} → {target} (+{len(kws)} keywords)")

def cmd_status():
    """查所有 hub keyword 数"""
    all_hubs = get_all_keyword_hubs()
    print("=== Hub keyword 数量 ===")
    for path, kws in sorted(all_hubs.items(), key=lambda x: len(x[1]), reverse=True):
        count = len(kws)
        flag = ""
        if Path(path).name == CATCH_ALL_QUEUE.name:
            if count >= TRIGGER_THRESHOLD:
                flag = " ⚠ 达阈值,触发聚类"
            elif count >= 80:
                flag = " ⚠ 接近阈值"
        print(f"  {count:3d}  {path}{flag}")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "--status"
    if cmd == "--analyze":
        hub = sys.argv[sys.argv.index("--hub")+1] if "--hub" in sys.argv else None
        cmd_analyze(hub)
    elif cmd == "--apply":
        hub = sys.argv[sys.argv.index("--hub")+1] if "--hub" in sys.argv else None
        cmd_apply(hub)
    elif cmd == "--status":
        cmd_status()
    else:
        print(__doc__)
