#!/usr/bin/env python3
"""query_actions.py — A+ 编排层检索动作执行 + token 计量(纯程序,不涉 LLM)

复用:wiki_locator/source_locator 确定性截取 + tiktoken 实计
职责:执行单个检索动作,返回真实文本+token。不碰语义判断,不碰状态。
"""
from __future__ import annotations
import subprocess
import json
import re
import sys
import sqlite3
from pathlib import Path
from typing import Optional

_REPO = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO / ".scripts"
sys.path.insert(0, str(_SCRIPTS))
import source_locator as sl
import wiki_locator as wl
import graph_lib as gl
import node_semantics as ns
import hub_semantics as hs
import query_graph as qg

try:
    import tiktoken
    _ENC = tiktoken.get_encoding("o200k_base")
except ImportError:
    _ENC = None

def _tok(text: str) -> int:
    if not text:
        return 0
    if _ENC is not None:
        return len(_ENC.encode(text))
    return len(text) // 2

def read_section(page: str, section: str = "") -> tuple[str, int]:
    """按 Wiki heading slug 截取一节，并只带回该节使用的 Raw locators。"""
    try:
        result = wl.read_wiki_locator(page, section)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        return f"[ERROR {exc}]", _tok(str(exc))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    return text, _tok(text)

def graph_query(command: str, args: list[str]) -> tuple[str, int]:
    """执行只读图查询；图结果纳入统一编排轨迹和 token 计量。"""
    db_script = _SCRIPTS / "query_graph.py"
    r = subprocess.run(["python3", str(db_script), command, *args, "--json"],
                       capture_output=True, text=True, cwd=_REPO)
    text = r.stdout.strip() or r.stderr.strip()
    return text, _tok(text)


def graph_search(term: str = "") -> tuple[str, int]:
    return graph_query("search", [term]) if term else ("[ERROR 缺 term]", 0)


def graph_neighbors(node: str = "", depth: str = "2", similar_topk: str = "",
                    profile: str = "", families: str = "") -> tuple[str, int]:
    # similar_topk: ""=用CLI默认(5), "0"=排除相似边, "-1"=全部, "N"=动态K上限
    # 编排层按 loop_count 注入(第一轮0=纯知识边,续轮渐进放开)
    args = [node, "--depth", str(depth)]
    if similar_topk:
        args += ["--similar-topk", str(similar_topk)]
    if profile:
        args += ["--profile", profile]
    if families:
        args += ["--families", families]
    return graph_query("neighbors", args) if node else ("[ERROR 缺 node]", 0)


def graph_relations(node: str = "", predicate: str = "", profile: str = "",
                    families: str = "") -> tuple[str, int]:
    args = [node]
    if predicate:
        args += ["--predicate", predicate]
    if profile:
        args += ["--profile", profile]
    if families:
        args += ["--families", families]
    return graph_query("relations", args) if node else ("[ERROR 缺 node]", 0)


def graph_hub_of(page: str = "") -> tuple[str, int]:
    return graph_query("hub_of", [page]) if page else ("[ERROR 缺 page]", 0)


def node_resolve(
    name: str = "", context: str = "", node_types: str = "", topk: str = "5"
) -> tuple[str, int]:
    """名称→canonical node_id；只返回身份计划，不修改图。"""
    if not name.strip():
        return "[ERROR 缺 name]", 0
    types = [item.strip() for item in str(node_types).split(",") if item.strip()]
    conn = gl.connect()
    try:
        result = ns.resolve_node(
            conn, name, context, node_types=types or None,
            top_k=max(1, min(int(topk), ns.MAX_TOP_K)),
        )
    finally:
        conn.close()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    return text, _tok(text)


def semantic_search(query: str = "", scope: str = "node", topk: str = "8") -> tuple[str, int]:
    """按含义召回节点/Hub；相关候选不构成节点同一性。"""
    if not query.strip():
        return "[ERROR 缺 query]", 0
    conn = gl.connect()
    try:
        result = ns.semantic_search(
            conn, query, scope=scope,
            top_k=max(1, min(int(topk), ns.MAX_TOP_K)),
        )
    finally:
        conn.close()
    if result.get("decision") == "invalid":
        return f"[ERROR {result.get('reason', 'invalid semantic search')}]", 0
    text = json.dumps(result, ensure_ascii=False, indent=2)
    return text, _tok(text)


def hub_route(page: str = "", topk: str = "5") -> tuple[str, int]:
    """读取论文的可定位方向句并与 canonical Hub Scope 路由；只读。"""
    if not page.strip():
        return "[ERROR 缺 page]", 0
    conn = gl.connect()
    try:
        result = hs.route_paper(conn, page, top_k=max(1, min(int(topk), 20)))
    finally:
        conn.close()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    return text, _tok(text)


def hub_inspect(hub: str = "") -> tuple[str, int]:
    """返回 Hub Scope、类型化普通成员及动力学候选；只读。"""
    if not hub.strip():
        return "[ERROR 缺 hub]", 0
    conn = gl.connect()
    try:
        result = hs.inspect_hub(conn, hub)
        result["writes"] = False
    finally:
        conn.close()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    return text, _tok(text)



DOMAIN_DIRS = {"academic", "admin", "teaching", "business"}


def wiki_recall(query: str = "", domain: str = "", topk: str = "8") -> tuple[str, int]:
    """跨域直接召回+图回退；只返回候选 Navigation，不读 Content/raw。"""
    if not query.strip():
        return "[ERROR 缺 query]", 0
    domain = domain.strip().lower()
    if domain and domain not in DOMAIN_DIRS:
        return f"[ERROR 不支持 domain: {domain}]", 0
    limit = max(1, min(int(topk), 20))
    conn = sqlite3.connect(str(_REPO / "cross-domain/graph.db"))
    conn.row_factory = sqlite3.Row
    terms = []
    for chunk in re.findall(r"[\u3400-\u9fff]+|[A-Za-z0-9-]{2,}", query.lower()):
        if re.fullmatch(r"[\u3400-\u9fff]+", chunk):
            terms.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
            terms.extend(chunk[i:i + 3] for i in range(len(chunk) - 2))
        else:
            terms.append(chunk)
    terms = list(dict.fromkeys(terms))
    direct = []
    for row in conn.execute(
        "SELECT DISTINCT n.path,n.title,n.type,n.status,a.alias FROM nodes n "
        "LEFT JOIN aliases a ON a.node_path=n.path "
        "WHERE n.path LIKE ? AND (n.title LIKE ? OR a.alias LIKE ?)",
        (f"{domain + '/' if domain else '%'}wiki/%", f"%{query}%", f"%{query}%"),
    ):
        direct.append(dict(row))
    pages = {}
    direction_profiles = {}
    roots = [domain] if domain else sorted(DOMAIN_DIRS)
    for root in roots:
        for path in sorted((_REPO / root / "wiki").rglob("*.md")):
            if path.name in {"index.md", "log.md"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            match = re.search(r"^## Navigation\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
            nav = match.group(1).strip() if match else ""
            title = path.stem
            rel = str(path.relative_to(_REPO)).removesuffix(".md")
            pages[rel] = (title, nav)
            profile = hs.read_paper_profile(rel)
            if profile is not None:
                direction_profiles[rel] = profile.text
    direction_profile_hits = 0
    for path, (title, nav) in pages.items():
        profile_text = direction_profiles.get(path, "")
        haystack = f"{title} {nav} {profile_text}".lower()
        score = sum(haystack.count(term) for term in terms)
        if score:
            direct.append({"path": path, "title": title, "type": "page", "status": "unknown", "score": score})
            if profile_text and any(term in profile_text.lower() for term in terms):
                direction_profile_hits += 1
    unique = {}
    for item in direct:
        unique[item["path"]] = item
    direct = sorted(unique.values(), key=lambda x: x.get("score", 1), reverse=True)
    mode = "direct"
    candidates = direct[:limit]
    if not candidates:
        mode = "graph_fallback"
        graph_paths = set()
        for term in terms:
            for row in conn.execute(
                "SELECT DISTINCT n.path,n.title,n.type,n.status FROM nodes n "
                "LEFT JOIN aliases a ON a.node_path=n.path "
                "WHERE n.title LIKE ? OR a.alias LIKE ? LIMIT 30",
                (f"%{term}%", f"%{term}%"),
            ):
                node = row["path"]
                for edge in conn.execute("SELECT subject,object FROM edges WHERE subject=? OR object=?", (node, node)):
                    other = edge["object"] if edge["subject"] == node else edge["subject"]
                    if other.startswith(f"{domain}/wiki/" if domain else tuple(f"{d}/wiki/" for d in DOMAIN_DIRS)):
                        graph_paths.add(other)
        candidates = [{"path": p, "title": pages.get(p, (p.rsplit("/", 1)[-1], ""))[0], "type": "page", "status": "unknown"}
                     for p in sorted(graph_paths)[:limit]]
    out = []
    for item in candidates:
        title, nav = pages.get(item["path"], (item.get("title", ""), ""))
        out.append({"path": item["path"], "title": title, "status": item.get("status"), "score": item.get("score", 0), "navigation": nav})
    conn.close()
    result = {"query": query, "domain": domain or "all", "mode": mode, "count": len(out), "candidates": out,
              "direction_profile_hits": direction_profile_hits,
              "sensitive_review": domain == "business",
              "next": "read Navigation candidates, then Content/raw" if out else "no_candidate"}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    return text, _tok(text)


def admin_recall(query: str = "", topk: str = "8") -> tuple[str, int]:
    """兼容旧入口；新调用统一使用 wiki_recall(domain=admin)。"""
    return wiki_recall(query, "admin", topk)


_INTENT_WEIGHTS = {
    "fact": (1.0, 0.35),
    "relation": (0.55, 1.0),
    "explanation": (1.0, 0.55),
    "exploration": (0.8, 0.8),
    "lineage": (0.45, 1.0),
}


def _adjacent_wiki_pages(conn, node: str, domain: str = "", limit: int = 30) -> list[str]:
    prefix = f"{domain}/wiki/" if domain else ""
    pages = []
    for row in conn.execute(
        "SELECT subject,object FROM edges WHERE subject=? OR object=? LIMIT ?",
        (node, node, limit * 3),
    ):
        other = row["object"] if row["subject"] == node else row["subject"]
        if "/wiki/" not in other or (prefix and not other.startswith(prefix)):
            continue
        if other not in pages:
            pages.append(other)
        if len(pages) >= limit:
            break
    return pages


def _section_capsule(page: str, *terms: str) -> dict:
    target = _REPO / f"{page.removesuffix('.md')}.md"
    if not target.is_file():
        return {}
    section = wl.best_cited_section(target, *terms)
    if section is None:
        return {}
    body = re.sub(r"\s+", " ", section.text).strip()
    return {
        "semantic_address": f"{page.removesuffix('.md')}#{section.slug}",
        "heading": section.title,
        "capsule": body[:320],
        "raw_citations": list(section.raw_citations),
    }


def _query_entity_terms(query: str) -> list[str]:
    terms = []
    for chunk in re.findall(r"[\u3400-\u9fff]+|[A-Za-z][A-Za-z0-9-]{1,}", query):
        if re.fullmatch(r"[\u3400-\u9fff]+", chunk):
            parts = re.split(r"(?:有什么关系|什么关系|为什么|如何|怎样|与|和|及|的)", chunk)
            terms.extend(part for part in parts if len(part) >= 2)
        else:
            terms.append(chunk)
    return list(dict.fromkeys(terms))[:8]


def hybrid_recall(query: str = "", intent: str = "exploration", domain: str = "",
                  topk: str = "8") -> tuple[str, int]:
    """Fuse Wiki narrative recall and Graph structural recall with weighted RRF."""
    if not query.strip():
        return "[ERROR 缺 query]", 0
    intent = intent.strip().lower() or "exploration"
    if intent not in _INTENT_WEIGHTS:
        return f"[ERROR 不支持 intent: {intent}]", 0
    domain = domain.strip().lower()
    if domain and domain not in DOMAIN_DIRS:
        return f"[ERROR 不支持 domain: {domain}]", 0
    limit = max(1, min(int(topk), 20))
    wiki_weight, graph_weight = _INTENT_WEIGHTS[intent]
    scores: dict[str, float] = {}
    signals: dict[str, list[dict]] = {}
    seen_signals = set()

    def add(path: str, channel: str, rank: int, weight: float, via: str = ""):
        if not path or "/wiki/" not in path:
            return
        if domain and not path.startswith(f"{domain}/wiki/"):
            return
        signal_key = (path, channel, via)
        if signal_key in seen_signals:
            return
        seen_signals.add(signal_key)
        scores[path] = scores.get(path, 0.0) + weight / (60.0 + rank)
        signals.setdefault(path, []).append({"channel": channel, "rank": rank, "via": via})

    wiki_text, _ = wiki_recall(query, domain, str(max(limit * 2, 8)))
    wiki_result = json.loads(wiki_text) if not wiki_text.startswith("[ERROR") else {"candidates": []}
    wiki_items = wiki_result.get("candidates", [])
    for rank, item in enumerate(wiki_items, start=1):
        add(item.get("path", ""), "wiki", rank, wiki_weight)

    conn = gl.connect()
    try:
        exact_rank = 0
        for term in _query_entity_terms(query):
            for item in qg.search_nodes(conn, term).get("nodes", [])[:8]:
                exact_rank += 1
                node = item.get("path", "")
                if "/wiki/" in node:
                    add(node, "graph_exact", exact_rank, graph_weight * 1.2, term)
                for page_path in _adjacent_wiki_pages(conn, node, domain, limit):
                    add(page_path, "graph_exact", exact_rank, graph_weight * 1.2, term)
        semantic = ns.semantic_search(conn, query, scope="node", top_k=max(limit * 2, 8))
        graph_nodes = semantic.get("candidates", [])
        if not graph_nodes:
            graph_nodes = qg.search_nodes(conn, query).get("nodes", [])
        for rank, item in enumerate(graph_nodes, start=1):
            node = item.get("node_id") or item.get("path") or ""
            if "/wiki/" in node:
                add(node, "graph", rank, graph_weight, node)
            for page_path in _adjacent_wiki_pages(conn, node, domain, limit):
                add(page_path, "graph", rank, graph_weight, node)
    finally:
        conn.close()

    wiki_by_path = {item.get("path"): item for item in wiki_items}
    capsules = {
        path: _section_capsule(path, query, *(s.get("via", "") for s in signals[path]))
        for path in scores
    }
    for path, capsule in capsules.items():
        if capsule.get("raw_citations"):
            scores[path] += 0.15 / 60.0
    ranked = sorted(scores, key=lambda path: (-scores[path], path))[:limit]
    candidates = []
    for path in ranked:
        capsule = capsules[path]
        item = wiki_by_path.get(path, {})
        candidates.append({
            "path": path,
            "title": item.get("title") or path.rsplit("/", 1)[-1],
            "score": round(scores[path], 6),
            "signals": signals[path],
            "navigation": item.get("navigation", ""),
            **capsule,
        })
    candidates.sort(key=lambda item: (-item["score"], item["path"]))
    result = {
        "query": query,
        "intent": intent,
        "domain": domain or "all",
        "fusion": "weighted_rrf",
        "weights": {"wiki": wiki_weight, "graph": graph_weight},
        "count": len(candidates),
        "candidates": candidates,
        "identity_claim": False,
        "next": "read semantic_address, then raw_citations",
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    return text, _tok(text)


def wiki_context_data(page: str, section: str, profile: str = "explanation",
                      topk: int = 12) -> dict:
    """Read one semantic address and attach a bounded, derived Graph envelope."""
    located = wl.read_wiki_locator(page, section)
    node = located["page"].removesuffix(".md")
    conn = gl.connect(str(gl.graph_db_for(node)))
    try:
        relation_result = qg.relations(
            conn, node, top_k=max(1, min(int(topk), 30)), profile=profile
        )
        context = []
        for edge in relation_result.get("edges", []):
            other = edge["object"] if edge["subject"] == node else edge["subject"]
            info = qg.node_info(conn, other) or {"path": other, "title": other}
            context.append({
                "relation": edge["predicate"],
                "direction": "out" if edge["subject"] == node else "in",
                "family": edge.get("family", ""),
                "locator": edge.get("locator", ""),
                "node": info,
            })
    finally:
        conn.close()
    return {
        "semantic_address": f"{node}#{located['section']}",
        "wiki": located,
        "graph_context": {"profile": profile, "relations": context},
    }


def wiki_context(page: str = "", section: str = "", profile: str = "explanation",
                 topk: str = "12") -> tuple[str, int]:
    if not page or not section:
        return "[ERROR 缺 page 或 section]", 0
    result = wiki_context_data(page, section, profile, int(topk))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    return text, _tok(text)

RAW_PREVIEW_CHARS = 8000


def read_raw(locator: str = "") -> tuple[str, int]:
    """按精确 locator 读 raw 片段；拒绝裸路径和 #全篇。
    复用 source_locator 解析路径+定位器，返回文本+token。"""
    if not locator.strip():
        return "[ERROR 缺 locator]", 0
    path_part, loc = sl.split_locator(locator)
    if not path_part:
        path_part = locator
    target = sl.resolve_path(path_part)
    if target is None:
        return f"[ERROR raw 路径未解析: {path_part}]", 0
    rel = str(target.resolve().relative_to(_REPO)) if target.is_absolute() else str(target)
    if not loc or loc == "全篇":
        return "[ERROR read_raw 需要精确 locator（标题、Lx-Ly 或 page-x-y）；不向 LLM 返回全文]", 0
    status = sl.locator_status(loc, target) if loc else "present"
    if status == "missing":
        return f"[ERROR locator '{loc}' 在 {rel} 中未找到]", 0
    is_binary = target.suffix.lower() in sl.BINARY_SUFFIXES
    seg = sl.read_locator_text(target, loc)
    if seg is None and is_binary:
        msg = f"[OK locator '{loc}' 已验证存在于 {rel}，但原文件无可返回文本；读取同目录 Markdown companion]"
        return msg, _tok(msg)
    if seg is None:
        return f"[ERROR locator '{loc}' 已验证但无法精确截取；未返回全文]", 0
    if len(seg) > RAW_PREVIEW_CHARS:
        return f"[ERROR locator '{loc}' 命中 {len(seg)} 字符，范围过大；请细化 locator，未返回半截内容]", 0
    header = f"[raw {rel}#{loc or '全篇'}]\n"
    return header + seg, _tok(header + seg)


DISPATCH = {
    "read_section": lambda inp: read_section(inp.get("page", ""), inp.get("section", "")),
    "read_raw": lambda inp: read_raw(inp.get("locator", "")),
    "graph_search": lambda inp: graph_search(inp.get("term", "")),
    "graph_neighbors": lambda inp: graph_neighbors(
        inp.get("node", ""), inp.get("depth", "2"), inp.get("similar_topk", ""),
        inp.get("profile", ""), inp.get("families", "")
    ),
    "graph_relations": lambda inp: graph_relations(
        inp.get("node", ""), inp.get("predicate", ""),
        inp.get("profile", ""), inp.get("families", "")
    ),
    "graph_hub_of": lambda inp: graph_hub_of(inp.get("page", "")),
    "node_resolve": lambda inp: node_resolve(
        inp.get("name", ""), inp.get("context", ""), inp.get("node_types", ""), inp.get("topk", "5")
    ),
    "semantic_search": lambda inp: semantic_search(
        inp.get("query", ""), inp.get("scope", "node"), inp.get("topk", "8")
    ),
    "hub_route": lambda inp: hub_route(inp.get("page", ""), inp.get("topk", "5")),
    "hub_inspect": lambda inp: hub_inspect(inp.get("hub", "")),
    "admin_recall": lambda inp: admin_recall(inp.get("query", ""), inp.get("topk", "8")),
    "wiki_recall": lambda inp: wiki_recall(inp.get("query", ""), inp.get("domain", ""), inp.get("topk", "8")),
    "hybrid_recall": lambda inp: hybrid_recall(
        inp.get("query", ""), inp.get("intent", "exploration"),
        inp.get("domain", ""), inp.get("topk", "8")
    ),
    "wiki_context": lambda inp: wiki_context(
        inp.get("page", ""), inp.get("section", ""),
        inp.get("profile", "explanation"), inp.get("topk", "12")
    ),
}

def execute(action: str, input_: dict) -> dict:
    fn = DISPATCH.get(action)
    if fn is None:
        return {"ok": False, "text": "", "tokens": 0,
                "error": f"未知动作 {action}(允许: {list(DISPATCH)})"}
    try:
        text, tokens = fn(input_ or {})
        ok = not text.startswith("[ERROR")
        return {"ok": ok, "text": text, "tokens": tokens, "error": "" if ok else text[:300]}
    except Exception as e:
        return {"ok": False, "text": "", "tokens": 0, "error": f"{type(e).__name__}: {str(e)[:200]}"}
