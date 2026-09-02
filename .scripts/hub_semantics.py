#!/usr/bin/env python3
"""Hub 成员动力学、Scope 路由与 Agent 受控生命周期。

Hub 是 keyword、proposition、People page 等普通节点的可重叠动态群落。
代码从类型化 profile、Hub Scope、同类成员原型和图邻接产生可重建
``聚类于`` 边及 create/split/merge 候选；Agent 只确认 title/Scope/parent
和生命周期语义。论文方向句→Scope 保留为独立的兼容路由。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import json
import re
from pathlib import Path
from typing import Iterable

import numpy as np
import yaml

import graph_lib as gl
import wiki_locator as wl


REPO = gl.REPO
PAGE_ROOT: Path | None = None
ROUTE_FLOOR = 0.5
ROUTE_MARGIN = 0.04
SPLIT_MIN_MEMBERS = 6
SPLIT_ROUTE_SUCCESS = 0.80
SPLIT_ROUTE_MARGIN = 0.03
MEMBERSHIP_PREDICATE = "聚类于"
MEMBERSHIP_ENTER = 0.66
MEMBERSHIP_RETAIN = 0.58
MEMBERSHIP_MAX_HUBS = 3
NEW_HUB_MIN_MEMBERS = 4
NEW_HUB_SIMILARITY = 0.82
AUTO_CREATE_COHESION = 0.60
AUTO_CREATE_MIN_MEMBERS = 4
MERGE_CANDIDATE_SIMILARITY = 0.88
SPLIT_DISTINCTION_THRESHOLD = 0.85
HUB_MEMBER_LIMIT = 20
MEMBERSHIP_CHILD_BONUS = 0.05
PROFILE_MEMBER_LIMIT = 24
DIRECTION_PREDICATES = {
    "主要研究", "涉及", "应用于", "基于", "贡献于", "延伸至", "探索", "属于",
}


@dataclass(frozen=True)
class DirectionProfile:
    page: str
    locator: str
    text: str
    raw_citations: tuple[str, ...]


@dataclass(frozen=True)
class NodeProfile:
    node_id: str
    kind: str
    text: str
    locator: str = ""


@dataclass(frozen=True)
class HubDefinition:
    path: str
    title: str
    scope: str
    parent: str
    status: str
    canonical: bool
    scope_mode: str


@dataclass(frozen=True)
class MembershipScoringContext:
    canonical: tuple[HubDefinition, ...]
    members_by_hub: dict[str, tuple[NodeProfile, ...]]
    has_prototype: tuple[bool, ...]
    texts: tuple[str, ...]


def configure_page_root(root: str | Path | None) -> None:
    """Route Hub/page file reads and lifecycle writes to an explicit mirror root."""
    global PAGE_ROOT
    PAGE_ROOT = Path(root).resolve() if root is not None else None


def _page_root() -> Path:
    return PAGE_ROOT or REPO


def _page_file(path: str | Path) -> Path:
    target = Path(path)
    if not target.is_absolute():
        target = _page_root() / target
    if not target.suffix:
        target = target.with_suffix(".md")
    return target


def _section_body(path: str | Path, heading: str) -> tuple[str, object | None]:
    target = _page_file(path)
    if not target.is_file():
        return "", None
    section = wl.get_wiki_section(target, heading)
    if section is None:
        return "", None
    lines = []
    for line in section.text.splitlines()[1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("[^"):
            continue
        if stripped.startswith("## "):
            break
        lines.append(stripped)
    text = " ".join(lines)
    text = re.sub(r"\[\^[A-Za-z0-9_-]+\]", "", text).strip()
    return text, section


def read_paper_profile(path: str | Path) -> DirectionProfile | None:
    text, section = _section_body(path, "研究方向定位")
    if not text or section is None:
        return None
    target = _page_file(path)
    rel = str(target.resolve().relative_to(_page_root().resolve())).removesuffix(".md")
    return DirectionProfile(
        page=rel,
        locator=f"{rel}#{section.slug}",
        text=text,
        raw_citations=tuple(section.raw_citations),
    )


def read_hub_scope(path: str | Path) -> str:
    return _section_body(path, "Scope")[0]


def validate_scope(scope: str) -> list[str]:
    text = str(scope or "").strip()
    errors = []
    if not text:
        errors.append("Scope 为空")
    elif len(text) < 20:
        errors.append("Scope 过短，须说明研究对象与核心问题")
    elif len(text) > 300:
        errors.append("Scope 过长，须保持一句导航定义")
    if "\n" in text or text.startswith(("#", "-", "*")):
        errors.append("Scope 必须是单段描述句")
    if re.search(r"<--|待定|TODO|子方向-[0-9a-f]+", text, re.I):
        errors.append("Scope 含占位内容")
    return errors


def _frontmatter(path: str | Path) -> dict:
    target = _page_file(path)
    if not target.is_file():
        return {}
    return gl.read_frontmatter(target)


def list_hubs(conn, include_legacy: bool = True, subtype: str = "") -> list[HubDefinition]:
    definitions = []
    for row in conn.execute(
        "SELECT path,title,status,description FROM nodes WHERE type='hub' ORDER BY path"
    ):
        path = row["path"]
        fm = _frontmatter(path)
        if subtype and str(fm.get("hub_subtype") or "") != subtype:
            continue
        scope = read_hub_scope(path)
        mode = "scope"
        canonical = bool(scope and not validate_scope(scope))
        if not scope and include_legacy:
            # 只读兼容：旧 Hub 可用已有 description/title 参与候选召回，但不能因此
            # 自动取得 canonical 身份，也不产生 ERROR/WARN。
            scope = str(row["description"] or row["title"] or Path(path).name).strip()
            mode = "legacy_description" if row["description"] else "legacy_title"
        if not scope:
            continue
        definitions.append(HubDefinition(
            path=path,
            title=str(row["title"] or fm.get("title") or Path(path).name),
            scope=scope,
            parent=str(fm.get("parent") or ""),
            status=str(row["status"] or fm.get("status") or "active"),
            canonical=canonical,
            scope_mode=mode,
        ))
    return [item for item in definitions if item.status not in {"retired", "archived"}]


def _embed(texts: list[str]):
    try:
        from embed_helper import embed_cached_batch
        values = embed_cached_batch(texts, cache_type="hub-scope")
        array = np.asarray(values, dtype=np.float32)
        if array.ndim == 1:
            array = array[None, :]
        return array
    except Exception:
        return None


def _unit(vectors):
    return vectors / (np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-9)


def route_profile(
    profile: str,
    definitions: Iterable[HubDefinition],
    *,
    top_k: int = 5,
    floor: float = ROUTE_FLOOR,
    margin: float = ROUTE_MARGIN,
) -> dict:
    """论文/节点描述→Hub Scope；只返回计划，不修改图。"""
    text = str(profile or "").strip()
    hubs = list(definitions)
    if not text:
        return {"decision": "invalid", "reason": "empty_profile", "candidates": []}
    if not hubs:
        return {"decision": "no_candidate", "reason": "empty_hub_index", "candidates": []}
    vectors = _embed([text, *[hub.scope for hub in hubs]])
    if vectors is None or len(vectors) != len(hubs) + 1:
        terms = set(re.findall(r"[A-Za-z0-9]{3,}|[\u3400-\u9fff]{2,}", text.casefold()))
        ranked = []
        for hub in hubs:
            haystack = f"{hub.title} {hub.scope}".casefold()
            overlap = sum(term in haystack for term in terms)
            if overlap:
                ranked.append({**asdict(hub), "lexical_score": overlap})
        ranked.sort(key=lambda item: item["lexical_score"], reverse=True)
        return {
            "decision": "candidates" if ranked else "no_candidate",
            "reason": "embedding_unavailable",
            "mode": "lexical_degraded",
            "candidates": ranked[:top_k],
        }
    units = _unit(vectors)
    scores = units[1:] @ units[0]
    ranked = []
    for hub, score in sorted(zip(hubs, scores), key=lambda pair: float(pair[1]), reverse=True):
        ranked.append({**asdict(hub), "score": round(float(score), 4)})
    canonical_ranked = [item for item in ranked if item["canonical"]]
    decision_ranked = canonical_ranked or ranked
    first = decision_ranked[0]
    second_score = decision_ranked[1]["score"] if len(decision_ranked) > 1 else 0.0
    observed_margin = round(first["score"] - second_score, 4)
    resolved = bool(
        canonical_ranked
        and first["score"] >= floor
        and observed_margin >= margin
    )
    if resolved:
        reason = "scope_threshold_and_margin"
    elif not canonical_ranked:
        reason = "no_canonical_scope"
    elif first["score"] < floor:
        reason = "scope_below_floor"
    else:
        reason = "scope_margin_too_small"
    return {
        "decision": "resolved" if resolved else "candidates",
        "node_id": first["path"] if resolved else None,
        "reason": reason,
        "mode": "scope_embedding",
        "top_score": first["score"],
        "margin": observed_margin,
        "candidates": ranked[:top_k],
    }


def route_paper(conn, page: str, **kwargs) -> dict:
    profile = read_paper_profile(page)
    if profile is None:
        return {"decision": "invalid", "reason": "missing_direction_profile", "candidates": []}
    result = route_profile(
        profile.text, list_hubs(conn, subtype="research-direction"), **kwargs,
    )
    result["profile"] = asdict(profile)
    return result


def read_people_profile(path: str | Path) -> NodeProfile | None:
    """Read the one locatable portrait sentence of a canonical People page.

    The sentence may describe research, administrative responsibilities, study
    stage, or another role.  Its heading is the contract; job type is not.
    """
    text, section = _section_body(path, "人物画像")
    if not text or section is None:
        return None
    target = _page_file(path)
    try:
        rel = str(target.resolve().relative_to(REPO.resolve())).removesuffix(".md")
    except ValueError:
        return None
    return NodeProfile(rel, "people", text, f"{rel}#{section.slug}")


def _profile_kind(row) -> str:
    node_type = str(row["type"] or "")
    subtype = str(row["entity_subtype"] or "")
    if node_type == "people":
        return "people"
    if node_type != "entity":
        return ""
    if subtype == "proposition":
        return "proposition"
    if subtype in {"", "keyword", "concept"}:
        return "keyword"
    return ""


def node_profile(conn, node_id: str) -> NodeProfile | None:
    row = conn.execute(
        "SELECT path,title,type,entity_subtype,description FROM nodes WHERE path=?",
        (node_id,),
    ).fetchone()
    if row is None:
        return None
    kind = _profile_kind(row)
    if kind == "people":
        # A name alone must never stand in for a person's semantics.  Lightweight
        # person entities are excluded by _profile_kind before reaching here.
        return read_people_profile(str(row["path"]))
    if not kind:
        return None
    title = str(row["title"] or row["path"] or "").strip()
    description = str(row["description"] or "").strip()
    if not title:
        return None
    text = title if not description or description == title else f"{title}。{description}"
    return NodeProfile(str(row["path"]), kind, text)


def ordinary_profiles(conn, node_ids: Iterable[str] | None = None) -> list[NodeProfile]:
    if node_ids is None:
        rows = conn.execute(
            "SELECT path FROM nodes WHERE type='people' OR "
            "(type='entity' AND COALESCE(entity_subtype,'') IN ('','keyword','concept','proposition')) "
            "ORDER BY path"
        )
    else:
        ids = sorted({str(item) for item in node_ids if str(item)})
        if not ids:
            return []
        rows = conn.execute(
            f"SELECT path FROM nodes WHERE path IN ({','.join('?' for _ in ids)}) ORDER BY path",
            ids,
        )
    return [profile for row in rows if (profile := node_profile(conn, row[0])) is not None]


def _existing_memberships(conn, node_id: str) -> set[str]:
    return {str(row[0]) for row in conn.execute(
        "SELECT object FROM edges WHERE subject=? AND predicate=?",
        (node_id, MEMBERSHIP_PREDICATE),
    )}


def member_node_profiles(conn, hub_path: str, kind: str = "") -> list[NodeProfile]:
    members = [str(row[0]) for row in conn.execute(
        "SELECT DISTINCT subject FROM edges WHERE object=? AND predicate=? ORDER BY subject",
        (hub_path, MEMBERSHIP_PREDICATE),
    )]
    profiles = ordinary_profiles(conn, members)
    return [item for item in profiles if not kind or item.kind == kind]


def _neighbors(conn, node_id: str, cache: dict[str, set[str]] | None = None) -> set[str]:
    if cache is not None and node_id in cache:
        return cache[node_id]
    values = set()
    for row in conn.execute(
        "SELECT subject,object FROM edges WHERE (subject=? OR object=?) AND predicate!=?",
        (node_id, node_id, MEMBERSHIP_PREDICATE),
    ):
        other = row[1] if row[0] == node_id else row[0]
        if other != node_id:
            values.add(str(other))
    if cache is not None:
        cache[node_id] = values
    return values


def _structural_affinity(conn, node_id: str, members: list[NodeProfile],
                         neighbor_cache: dict[str, set[str]] | None = None) -> float | None:
    node_neighbors = _neighbors(conn, node_id, neighbor_cache)
    if not node_neighbors or not members:
        return None
    scores = []
    for member in members[:PROFILE_MEMBER_LIMIT]:
        if member.node_id == node_id:
            continue
        member_neighbors = _neighbors(conn, member.node_id, neighbor_cache)
        union = node_neighbors | member_neighbors
        if union:
            scores.append(len(node_neighbors & member_neighbors) / len(union))
        if member.node_id in node_neighbors:
            scores.append(1.0)
    return max(scores) if scores else None


def _prepare_membership_context(
    conn,
    profile: NodeProfile,
    hubs: list[HubDefinition],
    member_cache: dict[tuple[str, str], list[NodeProfile]] | None = None,
) -> MembershipScoringContext | None:
    canonical = [hub for hub in hubs if hub.canonical]
    if not canonical:
        return None
    member_cache = member_cache if member_cache is not None else {}
    members_by_hub = {}
    for hub in canonical:
        key = (hub.path, profile.kind)
        if key not in member_cache:
            member_cache[key] = member_node_profiles(conn, hub.path, profile.kind)
        members_by_hub[hub.path] = tuple(member_cache[key])
    prototype_texts = []
    has_prototype = []
    for hub in canonical:
        members = [item for item in members_by_hub[hub.path] if item.node_id != profile.node_id]
        has_prototype.append(bool(members))
        prototype_texts.append("\n".join(item.text for item in members[:PROFILE_MEMBER_LIMIT]) or hub.scope)
    texts = [profile.text]
    for hub, prototype in zip(canonical, prototype_texts):
        texts.extend([hub.scope, prototype])
    return MembershipScoringContext(
        canonical=tuple(canonical),
        members_by_hub=members_by_hub,
        has_prototype=tuple(has_prototype),
        texts=tuple(texts),
    )


def _score_membership_candidates(
    conn,
    profile: NodeProfile,
    context: MembershipScoringContext,
    vectors,
    neighbor_cache: dict[str, set[str]] | None = None,
) -> tuple[list[dict], str]:
    if vectors is None or len(vectors) != len(context.texts):
        return [], "embedding_unavailable"
    units = _unit(vectors)
    node_vector = units[0]
    existing = _existing_memberships(conn, profile.node_id)
    ranked = []
    for index, hub in enumerate(context.canonical):
        scope_score = float(node_vector @ units[1 + index * 2])
        prototype_score = float(node_vector @ units[2 + index * 2])
        semantic = (
            scope_score if not context.has_prototype[index]
            else 0.6 * scope_score + 0.4 * prototype_score
        )
        structural = _structural_affinity(
            conn, profile.node_id, context.members_by_hub[hub.path], neighbor_cache,
        )
        if structural is None:
            score = semantic
            mode = "semantic"
        else:
            semantic_weight = 0.45 if profile.kind == "people" else 0.80
            score = semantic_weight * semantic + (1.0 - semantic_weight) * structural
            mode = "semantic+structural"
        threshold = MEMBERSHIP_RETAIN if hub.path in existing else MEMBERSHIP_ENTER
        ranked.append({
            "hub": hub.path,
            "score": round(score, 4),
            "scope_score": round(scope_score, 4),
            "prototype_score": (
                round(prototype_score, 4) if context.has_prototype[index] else None
            ),
            "structural_score": round(structural, 4) if structural is not None else None,
            "threshold": threshold,
            "existing": hub.path in existing,
            "mode": mode,
        })
    # 血亲偏好: 父Hub/子Hub同时命中时, 子Hub加分
    hub_paths = {item["hub"] for item in ranked}
    child_set: set[str] = set()
    for item in ranked:
        hp = item["hub"]
        if hp not in child_set:
            for other in hub_paths:
                if other != hp and other in get_ancestors(conn, hp, max_depth=1):
                    child_set.add(hp)
                    break
    for item in ranked:
        if item["hub"] in child_set:
            item["score"] = round(item["score"] + MEMBERSHIP_CHILD_BONUS, 4)
    ranked.sort(key=lambda item: (-item["score"], item["hub"]))
    return ranked, "scored"


def plan_memberships(conn, node_ids: Iterable[str] | None = None) -> dict:
    """Plan overlapping, rebuildable Hub memberships without modifying the graph."""
    profiles = ordinary_profiles(conn, node_ids)
    hubs = list_hubs(conn)
    nodes = []
    degraded = False
    member_cache: dict[tuple[str, str], list[NodeProfile]] = {}
    neighbor_cache: dict[str, set[str]] = {}
    contexts = [
        _prepare_membership_context(conn, profile, hubs, member_cache)
        for profile in profiles
    ]
    unique_texts = list(dict.fromkeys(
        text
        for context in contexts if context is not None
        for text in context.texts
    ))
    embedded = _embed(unique_texts) if unique_texts else None
    vectors_by_text = None
    if embedded is not None and len(embedded) == len(unique_texts):
        vectors_by_text = dict(zip(unique_texts, embedded))

    for profile, context in zip(profiles, contexts):
        if context is None:
            ranked, reason = [], "empty_hub_index"
        else:
            vectors = None
            if vectors_by_text is not None:
                vectors = np.asarray([vectors_by_text[text] for text in context.texts])
            ranked, reason = _score_membership_candidates(
                conn, profile, context, vectors, neighbor_cache,
            )
        if reason != "scored":
            if reason == "empty_hub_index":
                nodes.append({
                    **asdict(profile), "decision": "unassigned", "reason": reason,
                    "memberships": [], "candidates": [],
                })
                continue
            degraded = degraded or reason == "embedding_unavailable"
            nodes.append({
                **asdict(profile), "decision": "preserve_existing", "reason": reason,
                "memberships": sorted(_existing_memberships(conn, profile.node_id)),
                "candidates": [],
            })
            continue
        selected = [item for item in ranked if item["score"] >= item["threshold"]]
        selected = selected[:MEMBERSHIP_MAX_HUBS]
        nodes.append({
            **asdict(profile),
            "decision": "planned" if selected else "unassigned",
            "reason": reason if selected else "below_membership_threshold",
            "memberships": [item["hub"] for item in selected],
            "candidates": ranked[:MEMBERSHIP_MAX_HUBS + 2],
        })
    return {
        "decision": "degraded" if degraded else "planned",
        "write_safe": not degraded,
        "node_count": len(nodes),
        "nodes": nodes,
    }


def apply_membership_plan(conn, plan: dict) -> dict:
    """Rewrite only targeted derived membership edges from a write-safe plan."""
    if not plan.get("write_safe"):
        return {"applied": False, "reason": "plan_not_write_safe", "nodes": 0, "edges": 0}
    rows = [item for item in plan.get("nodes", []) if item.get("decision") in {"planned", "unassigned"}]
    edge_count = 0
    for item in rows:
        node_id = str(item.get("node_id") or "")
        if not node_id:
            continue
        conn.execute(
            "DELETE FROM edges WHERE subject=? AND predicate=?",
            (node_id, MEMBERSHIP_PREDICATE),
        )
        score_map = {candidate["hub"]: candidate.get("score") for candidate in item.get("candidates", [])}
        for hub in item.get("memberships", []):
            conn.execute(
                "INSERT INTO edges(subject,predicate,object,confidence,source,is_sr,score) "
                "VALUES(?,?,?,?,?,?,?)",
                (node_id, MEMBERSHIP_PREDICATE, hub, "推断", "", 0, score_map.get(hub)),
            )
            edge_count += 1
    return {"applied": True, "nodes": len(rows), "edges": edge_count}


def _replace_frontmatter_and_scope(path: str, fm_updates: dict, scope: str) -> None:
    target = _page_file(path)
    text = target.read_text(encoding="utf-8") if target.is_file() else ""
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    fm = yaml.safe_load(match.group(1)) or {} if match else {}
    fm.update(fm_updates)
    body = text[match.end():] if match else text
    if not body.strip() and fm_updates.get("title"):
        body = f"# {fm_updates['title']}\n\n"
    scope_block = f"## Scope\n\n{scope.strip()}\n"
    section = re.search(r"(?ms)^## Scope\s*\n.*?(?=^## |\Z)", body)
    if section:
        body = body[:section.start()] + scope_block + "\n" + body[section.end():].lstrip("\n")
    else:
        insert = re.search(r"(?m)^## ", body)
        position = insert.start() if insert else len(body)
        prefix = body[:position].rstrip()
        suffix = body[position:].lstrip("\n")
        body = f"{prefix}\n\n{scope_block}\n{suffix}".rstrip() + "\n"
    rendered = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"---\n{rendered}\n---\n\n{body.lstrip()}", encoding="utf-8")


def create_hub(
    conn,
    *,
    path: str,
    title: str,
    scope: str,
    parent: str = "",
    agent_confirmed: bool = False,
) -> dict:
    if not agent_confirmed:
        raise PermissionError("canonical Hub 创建必须由 Agent 确认")
    errors = validate_scope(scope)
    if errors:
        raise ValueError("; ".join(errors))
    if gl.node_exists(conn, path) or _page_file(path).exists():
        raise ValueError(f"Hub path 已存在: {path}")
    if conn.execute("SELECT 1 FROM nodes WHERE type='hub' AND title=?", (title,)).fetchone():
        raise ValueError(f"Hub title 已存在: {title}")
    if parent and not gl.node_exists(conn, parent):
        raise ValueError(f"父 Hub 不存在: {parent}")
    today = __import__("datetime").date.today().isoformat()
    _replace_frontmatter_and_scope(path, {
        "title": title,
        "type": "topic-hub",
        "hub_subtype": "research-direction",
        "parent": parent or None,
        "status": "active",
        "created": today,
        "updated": today,
    }, scope)
    gl.ensure_node(conn, path, title, "hub", status="active", description=scope)
    if parent:
        conn.execute(
            "INSERT OR IGNORE INTO edges(subject,predicate,object,confidence,source,is_sr) "
            "VALUES(?, '子方向', ?, '推断', '', 0)",
            (parent, path),
        )
    return {"created": path, "title": title, "scope": scope, "parent": parent}


def sync_hub_scope(conn, path: str) -> dict:
    scope = read_hub_scope(path)
    errors = validate_scope(scope)
    if errors:
        return {"updated": False, "errors": errors}
    fm = _frontmatter(path)
    gl.ensure_node(
        conn, path, str(fm.get("title") or Path(path).name), "hub",
        status=str(fm.get("status") or "active"), description=scope,
    )
    return {"updated": True, "path": path, "scope": scope}


def migrate_root_scopes(conn, *, apply: bool = False) -> dict:
    """把已由 Agent 审核写入配置的根 Scope 补到 Hub 页；旧子 Hub 不猜测。"""
    config_path = REPO / "operations/config/arxiv-directions.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    scopes = {
        str(item.get("name") or "").strip(): str(item.get("scope") or "").strip()
        for item in config.get("directions", []) if isinstance(item, dict)
    }
    planned = []
    for title, scope in scopes.items():
        if not title or validate_scope(scope):
            continue
        path = f"academic/wiki/hubs/{title}"
        existing = conn.execute(
            "SELECT path FROM nodes WHERE type='hub' AND path=?", (path,)
        ).fetchone()
        if existing:
            if read_hub_scope(path):
                continue
            action = "add_scope"
        else:
            title_conflicts = conn.execute(
                "SELECT path FROM nodes WHERE type='hub' AND title=?", (title,)
            ).fetchall()
            if title_conflicts:
                continue
            action = "create_root"
        planned.append({"action": action, "path": path, "title": title, "scope": scope})
        if apply and action == "add_scope":
            _replace_frontmatter_and_scope(path, {
                "updated": __import__("datetime").date.today().isoformat(),
            }, scope)
            sync_hub_scope(conn, path)
        elif apply:
            create_hub(
                conn, path=path, title=title, scope=scope, agent_confirmed=True,
            )
    return {"apply": apply, "count": len(planned), "hubs": planned}


def member_profiles(conn, hub_path: str) -> list[NodeProfile | DirectionProfile]:
    """Return dynamic ordinary members, with old paper direction edges as fallback."""
    dynamic = member_node_profiles(conn, hub_path)
    if dynamic:
        return dynamic
    pages = [row[0] for row in conn.execute(
        "SELECT DISTINCT subject FROM edges WHERE object=? AND predicate IN "
        f"({','.join('?' for _ in DIRECTION_PREDICATES)})",
        (hub_path, *sorted(DIRECTION_PREDICATES)),
    )]
    return [profile for page in pages if (profile := read_paper_profile(page)) is not None]


def _profile_id(profile: NodeProfile | DirectionProfile) -> str:
    return profile.node_id if isinstance(profile, NodeProfile) else profile.page


def analyze_split(conn, hub_path: str) -> dict:
    """Generate a typed-member split candidate; never write lifecycle state."""
    profiles = member_profiles(conn, hub_path)
    if len(profiles) < SPLIT_MIN_MEMBERS:
        return {"decision": "no_split", "reason": "insufficient_profiles", "count": len(profiles)}
    vectors = _embed([item.text for item in profiles])
    if vectors is None:
        return {"decision": "no_split", "reason": "embedding_unavailable", "count": len(profiles)}
    units = _unit(vectors)
    similarities = units @ units.T
    first = 0
    second = int(np.argmin(similarities[first]))
    centroids = np.vstack([units[first], units[second]])
    labels = np.zeros(len(units), dtype=int)
    for _ in range(20):
        new_labels = np.argmax(units @ centroids.T, axis=1)
        if np.array_equal(new_labels, labels) and _:
            break
        labels = new_labels
        for index in (0, 1):
            members = units[labels == index]
            if len(members):
                centroid = members.mean(axis=0)
                centroids[index] = centroid / (np.linalg.norm(centroid) + 1e-9)
    clusters = []
    for index in (0, 1):
        ids = np.where(labels == index)[0]
        if len(ids) < 3:
            return {"decision": "no_split", "reason": "unstable_small_cluster", "count": len(profiles)}
        ranked = sorted(ids, key=lambda i: float(units[i] @ centroids[index]), reverse=True)
        clusters.append({
            "members": [_profile_id(profiles[i]) for i in ids],
            "representatives": [asdict(profiles[i]) for i in ranked[:5]],
        })
    # 成员级区分度: 两簇 centroid cosine < 阈值才有效
    centroid_similarity = float(centroids[0] @ centroids[1])
    if centroid_similarity >= SPLIT_DISTINCTION_THRESHOLD:
        return {
            "decision": "no_split",
            "reason": "clusters_not_distinct",
            "centroid_similarity": round(centroid_similarity, 4),
            "threshold": SPLIT_DISTINCTION_THRESHOLD,
            "count": len(profiles),
        }
    parent_scope = read_hub_scope(hub_path) or ""
    return {
        "decision": "agent_definition_required",
        "hub": hub_path,
        "count": len(profiles),
        "clusters": clusters,
        "centroid_similarity": round(centroid_similarity, 4),
        "parent_scope": parent_scope,
        "agent_task": "为每簇确认 title、Scope（父Hub Scope 的特化）；两子 Scope 须可区分；代码随后运行 route probes",
    }


def analyze_new_hubs(conn, node_ids: Iterable[str] | None = None) -> dict:
    """Find coherent unassigned ordinary-node components; candidates only."""
    profiles = [item for item in ordinary_profiles(conn, node_ids)
                if not _existing_memberships(conn, item.node_id)]
    if len(profiles) < NEW_HUB_MIN_MEMBERS:
        return {"decision": "no_candidate", "reason": "insufficient_unassigned", "count": len(profiles)}
    vectors = _embed([item.text for item in profiles])
    if vectors is None or len(vectors) != len(profiles):
        return {"decision": "no_candidate", "reason": "embedding_unavailable", "count": len(profiles)}
    units = _unit(vectors)
    parent = list(range(len(profiles)))

    def root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        left, right = root(left), root(right)
        if left != right:
            parent[right] = left

    similarities = units @ units.T
    for left in range(len(profiles)):
        for right in range(left + 1, len(profiles)):
            if float(similarities[left, right]) >= NEW_HUB_SIMILARITY:
                union(left, right)
    components: dict[int, list[int]] = {}
    for index in range(len(profiles)):
        components.setdefault(root(index), []).append(index)
    candidates = []
    for ids in components.values():
        if len(ids) < NEW_HUB_MIN_MEMBERS:
            continue
        centroid = units[ids].mean(axis=0)
        centroid /= np.linalg.norm(centroid) + 1e-9
        ranked = sorted(ids, key=lambda i: float(units[i] @ centroid), reverse=True)
        candidates.append({
            "members": [_profile_id(profiles[i]) for i in ids],
            "kinds": sorted({profiles[i].kind for i in ids}),
            "representatives": [asdict(profiles[i]) for i in ranked[:5]],
            "cohesion": round(float(np.mean(similarities[np.ix_(ids, ids)])), 4),
        })
    candidates.sort(key=lambda item: (-len(item["members"]), -item["cohesion"]))
    return {
        "decision": "agent_definition_required" if candidates else "no_candidate",
        "count": len(profiles),
        "candidates": candidates,
        "agent_task": "判断候选是否值得成为 Hub，并确认 title、Scope 与 parent",
    }


def analyze_merge_candidates(conn) -> dict:
    """Compare canonical Hub definitions/prototypes; a score can only nominate."""
    hubs = [hub for hub in list_hubs(conn) if hub.canonical]
    if len(hubs) < 2:
        return {"decision": "no_candidate", "candidates": []}
    texts = []
    for hub in hubs:
        members = member_node_profiles(conn, hub.path)
        prototype = "\n".join(item.text for item in members[:PROFILE_MEMBER_LIMIT])
        texts.append(f"{hub.scope}\n{prototype}" if prototype else hub.scope)
    vectors = _embed(texts)
    if vectors is None or len(vectors) != len(hubs):
        return {"decision": "no_candidate", "reason": "embedding_unavailable", "candidates": []}
    units = _unit(vectors)
    candidates = []
    for left in range(len(hubs)):
        for right in range(left + 1, len(hubs)):
            score = float(units[left] @ units[right])
            if score < MERGE_CANDIDATE_SIMILARITY:
                continue
            if has_blood_relation(conn, hubs[left].path, hubs[right].path):
                continue  # 三代血亲禁止合并
            candidates.append({
                "left": hubs[left].path, "right": hubs[right].path,
                "score": round(score, 4), "decision": "agent_review_required",
            })
    candidates.sort(key=lambda item: (-item["score"], item["left"], item["right"]))
    return {
        "decision": "agent_review_required" if candidates else "no_candidate",
        "candidates": candidates,
    }


def _hub_member_count(conn, hub_path: str) -> int:
    """Count ordinary members of a Hub via 聚类于 edges."""
    return conn.execute(
        "SELECT COUNT(*) FROM edges WHERE predicate=? AND object=?",
        (MEMBERSHIP_PREDICATE, hub_path),
    ).fetchone()[0]


def _check_hub_overload(conn) -> list[dict]:
    """Check all canonical Hubs for member count exceeding HUB_MEMBER_LIMIT.

    Overloaded Hubs with existing child Hubs trigger redistribution;
    overloaded Hubs without children trigger split candidates.
    """
    overloaded = []
    for hub in list_hubs(conn):
        if not hub.canonical:
            continue
        count = _hub_member_count(conn, hub.path)
        if count <= HUB_MEMBER_LIMIT:
            continue
        children = get_child_hubs(conn, hub.path)
        overloaded.append({
            "hub": hub.path,
            "member_count": count,
            "limit": HUB_MEMBER_LIMIT,
            "children": children,
            "action": "redistribute" if children else "split_candidate",
        })
    return overloaded


def dynamics_plan(conn, node_ids: Iterable[str] | None = None,
                  *, apply_membership: bool = False) -> dict:
    membership = plan_memberships(conn, node_ids)
    apply_report = apply_membership_plan(conn, membership) if apply_membership else {"applied": False}
    hubs = sorted({hub for item in membership.get("nodes", [])
                   for hub in item.get("memberships", [])})
    split_candidates = []
    for hub in hubs:
        result = analyze_split(conn, hub)
        if result.get("decision") == "agent_definition_required":
            split_candidates.append(result)
    unassigned = [item["node_id"] for item in membership.get("nodes", [])
                  if item.get("decision") == "unassigned"]
    return {
        "membership": membership,
        "membership_apply": apply_report,
        "new_hubs": analyze_new_hubs(conn, unassigned),
        "splits": split_candidates,
        "merges": analyze_merge_candidates(conn),
        "overloaded_hubs": _check_hub_overload(conn),
        "lifecycle_auto_applied": False,
    }


def refresh_after_ingest(conn, page: str, *, max_nodes: int = 48) -> dict:
    """Locally refresh derived membership around one ingested page.

    This hook is deliberately bounded and soft-failing at its caller.  It never
    creates/splits/merges a Hub and never scans every ordinary node.
    """
    affected = {page}
    for row in conn.execute(
        "SELECT subject,object FROM edges WHERE subject=? OR object=? LIMIT ?",
        (page, page, max_nodes),
    ):
        affected.update((str(row[0]), str(row[1])))
    eligible = [item.node_id for item in ordinary_profiles(conn, affected)][:max_nodes]
    membership = plan_memberships(conn, eligible)
    applied = apply_membership_plan(conn, membership)
    hubs = sorted({hub for item in membership.get("nodes", []) for hub in item.get("memberships", [])})
    splits = []
    for hub in hubs:
        candidate = analyze_split(conn, hub)
        if candidate.get("decision") == "agent_definition_required":
            splits.append(candidate)
    unassigned = [item["node_id"] for item in membership.get("nodes", [])
                  if item.get("decision") == "unassigned"]
    return {
        "affected_nodes": eligible,
        "membership": membership,
        "membership_apply": applied,
        "new_hubs": analyze_new_hubs(conn, unassigned),
        "splits": splits,
        "overloaded_hubs": _check_hub_overload(conn),
        "lifecycle_auto_applied": False,
    }


def inspect_hub(conn, hub: str) -> dict:
    definition = next((item for item in list_hubs(conn) if item.path == hub), None)
    members = member_node_profiles(conn, hub)
    return {
        "hub": asdict(definition) if definition else {"path": hub, "scope": read_hub_scope(hub)},
        "members": [asdict(item) for item in members],
        "member_kinds": {kind: sum(item.kind == kind for item in members)
                         for kind in ("keyword", "proposition", "people")},
        "split": analyze_split(conn, hub),
    }


def apply_split(conn, parent: str, children: list[dict], *, agent_confirmed: bool = False) -> dict:
    if not agent_confirmed:
        raise PermissionError("Hub 分裂必须由 Agent 确认子 Scope")
    definitions = []
    all_profiles: list[tuple[str, NodeProfile | DirectionProfile]] = []
    for child in children:
        errors = validate_scope(child.get("scope", ""))
        if errors:
            raise ValueError(f"{child.get('title', child.get('path'))}: {'; '.join(errors)}")
        definitions.append(HubDefinition(
            child["path"], child["title"], child["scope"], parent, "active", True, "scope"
        ))
    # Scope 级区分度: 子Hub Scope 间 cosine < 阈值
    if len(definitions) >= 2:
        scope_vecs = _embed([d.scope for d in definitions])
        if scope_vecs is not None and len(scope_vecs) == len(definitions):
            scope_units = _unit(scope_vecs)
            for i in range(len(definitions)):
                for j in range(i + 1, len(definitions)):
                    sim = float(scope_units[i] @ scope_units[j])
                    if sim >= SPLIT_DISTINCTION_THRESHOLD:
                        raise ValueError(
                            f"子Hub Scope 区分度不足: {definitions[i].path} vs {definitions[j].path} "
                            f"cosine={sim:.4f} >= {SPLIT_DISTINCTION_THRESHOLD}; "
                            f"请基于代表成员重写更区分的 Scope"
                        )
    all_profiles: list[tuple[str, NodeProfile | DirectionProfile]] = []
    for child in children:
        for member in child.get("members", []):
            profile = node_profile(conn, member) or read_paper_profile(member)
            if profile:
                all_profiles.append((child["path"], profile))
    routed = [
        (expected, route_profile(profile.text, definitions, floor=0.0, margin=0.0))
        for expected, profile in all_profiles
    ]
    successes = sum(result.get("candidates", [{}])[0].get("path") == expected for expected, result in routed)
    margins = [float(result.get("margin", 0.0)) for _expected, result in routed]
    success_rate = successes / len(routed) if routed else 0.0
    average_margin = sum(margins) / len(margins) if margins else 0.0
    if success_rate < SPLIT_ROUTE_SUCCESS or average_margin < SPLIT_ROUTE_MARGIN:
        raise ValueError(
            f"子 Scope 路由探针未通过: success={success_rate:.3f}, margin={average_margin:.3f}"
        )
    created = []
    for child in children:
        created.append(create_hub(
            conn, path=child["path"], title=child["title"], scope=child["scope"],
            parent=parent, agent_confirmed=True,
        ))
        for page in child.get("members", []):
            conn.execute(
                "DELETE FROM edges WHERE subject=? AND predicate=? AND object=?",
                (page, MEMBERSHIP_PREDICATE, child["path"]),
            )
            conn.execute(
                "INSERT INTO edges(subject,predicate,object,confidence,source,is_sr,score) "
                "VALUES(?, ?, ?, '推断', '', 0, NULL)",
                (page, MEMBERSHIP_PREDICATE, child["path"]),
            )
    return {
        "created": created,
        "route_success": round(success_rate, 3),
        "average_margin": round(average_margin, 3),
    }


def merge_hubs(
    conn,
    *,
    survivor: str,
    retired: str,
    scope: str,
    title: str = "",
    agent_confirmed: bool = False,
) -> dict:
    """非破坏式合并：保留 retired 文件/节点并建 ``合并至`` redirect。"""
    if not agent_confirmed:
        raise PermissionError("Hub 合并与统一 Scope 必须由 Agent 确认")
    errors = validate_scope(scope)
    if errors:
        raise ValueError("; ".join(errors))
    if survivor == retired or not gl.node_exists(conn, survivor) or not gl.node_exists(conn, retired):
        raise ValueError("survivor/retired Hub 无效")
    survivor_fm = _frontmatter(survivor)
    survivor_title = title or str(survivor_fm.get("title") or Path(survivor).name)
    _replace_frontmatter_and_scope(survivor, {
        "title": survivor_title,
        "status": "active",
        "updated": __import__("datetime").date.today().isoformat(),
    }, scope)
    sync_hub_scope(conn, survivor)
    retired_fm = _frontmatter(retired)
    _replace_frontmatter_and_scope(retired, {
        "status": "retired",
        "merged_into": survivor,
        "updated": __import__("datetime").date.today().isoformat(),
    }, read_hub_scope(retired) or str(retired_fm.get("title") or Path(retired).name))
    conn.execute("UPDATE nodes SET status='retired' WHERE path=?", (retired,))
    conn.execute(
        "INSERT OR IGNORE INTO edges(subject,predicate,object,confidence,source,is_sr) "
        "VALUES(?, '合并至', ?, '推断', '', 0)",
        (retired, survivor),
    )
    return {"survivor": survivor, "retired": retired, "scope": scope, "redirect": True}


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def get_ancestors(conn, hub_path: str, max_depth: int = 3) -> set[str]:
    """沿子方向边向上追溯祖先（不含姻亲）。"""
    ancestors: set[str] = set()
    queue = [(hub_path, 0)]
    while queue:
        h, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        parents = [r[0] for r in conn.execute(
            "SELECT subject FROM edges WHERE predicate='子方向' AND object=?", (h,)
        ).fetchall()]
        for p in parents:
            if p not in ancestors:
                ancestors.add(p)
                queue.append((p, depth + 1))
    return ancestors


def has_blood_relation(conn, hub_a: str, hub_b: str, max_depth: int = 3) -> bool:
    """两个Hub沿子方向向上追溯三代，祖先交叉则有血亲关系。"""
    anc_a = get_ancestors(conn, hub_a, max_depth)
    anc_b = get_ancestors(conn, hub_b, max_depth)
    return bool(anc_a & anc_b) or hub_a in anc_b or hub_b in anc_a


def get_child_hubs(conn, hub_path: str) -> list[str]:
    """获取直接子 Hub 列表。"""
    return [r[0] for r in conn.execute(
        "SELECT object FROM edges WHERE predicate='子方向' AND subject=?", (hub_path,)
    ).fetchall()]


def _eligible_auto_splits(conn, overloaded_hubs: list[dict]) -> tuple[list[dict], int]:
    """Promote stable overload splits to the inbox-tail Agent handoff."""
    eligible = []
    backlog_count = 0
    for overload in overloaded_hubs:
        if overload.get("action") != "split_candidate":
            continue
        candidate = analyze_split(conn, str(overload.get("hub", "")))
        if candidate.get("decision") != "agent_definition_required":
            backlog_count += 1
            continue
        eligible.append({
            **candidate,
            "trigger": "member_limit",
            "member_count": overload.get("member_count", candidate.get("count", 0)),
            "limit": overload.get("limit", HUB_MEMBER_LIMIT),
        })
    return eligible, backlog_count


def _redistribution_handoffs(
    overloaded_hubs: list[dict], definitions: Iterable[HubDefinition]
) -> list[dict]:
    """Keep existing-child overloads visible without mutating membership in --check."""
    by_path = {hub.path: hub for hub in definitions}
    handoffs = []
    for overload in overloaded_hubs:
        if overload.get("action") != "redistribute":
            continue
        child_scopes = []
        blockers = []
        for child in overload.get("children", []):
            definition = by_path.get(child)
            ready = bool(definition and definition.canonical)
            child_scopes.append({
                "path": child,
                "title": definition.title if definition else "",
                "scope": definition.scope if definition else "",
                "canonical": ready,
                "ready": ready,
            })
            if not ready:
                blockers.append({
                    "path": child,
                    "reason": (
                        "missing_hub_definition" if definition is None
                        else "missing_canonical_scope"
                    ),
                })
        handoffs.append({
            **overload,
            "decision": (
                "redistribution_required" if not blockers
                else "canonical_scope_required"
            ),
            "trigger": "member_limit",
            "ready_for_redistribution": not blockers,
            "child_scopes": child_scopes,
            "blockers": blockers,
            "agent_task": (
                "run an explicit membership redistribution"
                if not blockers
                else "define and validate missing child Hub scopes before redistribution"
            ),
        })
    return handoffs


def auto_create_check(conn, node_ids: Iterable[str] | None = None) -> dict:
    """摄入末期 Hub 检查：筛选新 Hub 与既存 Hub 分裂候选。

    返回 eligible 候选（cohesion≥AUTO_CREATE_COHESION 且 members≥AUTO_CREATE_MIN_MEMBERS），
    并把超限且通过稳定二分闸的既存 Hub 交给主 Agent 定义子 Scope。
    不达标候选只计 backlog，不向用户报告。
    """
    membership = plan_memberships(conn, node_ids)
    unassigned = [
        item["node_id"] for item in membership.get("nodes", [])
        if item.get("decision") == "unassigned"
    ]
    new_hubs = analyze_new_hubs(conn, unassigned)
    candidates = new_hubs.get("candidates", [])
    eligible = []
    backlog_count = 0
    for candidate in candidates:
        members = candidate.get("members", [])
        cohesion = candidate.get("cohesion", 0)
        if cohesion >= AUTO_CREATE_COHESION and len(members) >= AUTO_CREATE_MIN_MEMBERS:
            suggested = _suggest_parent(conn, candidate)
            entry = {**candidate, "suggested_parent": suggested}
            eligible.append(entry)
        else:
            backlog_count += 1
    overloaded_hubs = _check_hub_overload(conn)
    split_candidates, split_backlog_count = _eligible_auto_splits(conn, overloaded_hubs)
    redistribution_candidates = _redistribution_handoffs(overloaded_hubs, list_hubs(conn))
    return {
        "status": (
            "agent_required"
            if eligible or split_candidates or redistribution_candidates
            else "no_action"
        ),
        "scope": "incremental" if node_ids is not None else "full",
        "affected_node_count": membership.get("node_count", 0),
        "eligible": eligible,
        "split_candidates": split_candidates,
        "redistribution_candidates": redistribution_candidates,
        "backlog_count": backlog_count,
        "split_backlog_count": split_backlog_count,
        "membership_apply": {"applied": False, "reason": "check_read_only"},
    }


def _suggest_parent(conn, candidate: dict) -> dict:
    """为候选簇找最近的 canonical Hub 作为建议 parent。"""
    hubs = [hub for hub in list_hubs(conn) if hub.canonical]
    if not hubs:
        return {"path": "", "title": "", "score": 0.0}
    hub_vectors = _embed([hub.scope for hub in hubs])
    if hub_vectors is None:
        return {"path": "", "title": "", "score": 0.0}
    hub_units = _unit(hub_vectors)
    reps = candidate.get("representatives", [])
    texts = [rep.get("text", "") for rep in reps if rep.get("text")]
    if not texts:
        return {"path": "", "title": "", "score": 0.0}
    rep_vectors = _embed(texts)
    if rep_vectors is None:
        return {"path": "", "title": "", "score": 0.0}
    centroid = rep_vectors.mean(axis=0)
    centroid = centroid / (np.linalg.norm(centroid) + 1e-9)
    scores = hub_units @ centroid
    best = int(np.argmax(scores))
    best_score = float(scores[best])
    if best_score < 0.5:
        return {"path": "", "title": "", "score": round(best_score, 4)}
    return {
        "path": hubs[best].path,
        "title": hubs[best].title,
        "score": round(best_score, 4),
    }


def create_hubs_from_definitions(conn, definitions: list[dict]) -> dict:
    """Agent 生成定义后，validate + create_hub + apply membership。

    definitions: [{title, scope, parent, members}, ...]
    """
    created = []
    errors = []
    all_members = []
    # Scope 级区分度: 新Hub Scope 间 + 与已有 canonical Hub 的 cosine < 阈值
    new_scopes = [str(d.get("scope", "")).strip() for d in definitions if d.get("scope")]
    existing_hubs = [hub for hub in list_hubs(conn) if hub.canonical]
    check_texts = new_scopes + [hub.scope for hub in existing_hubs]
    check_vecs = _embed(check_texts) if check_texts else None
    if check_vecs is not None:
        check_units = _unit(check_vecs)
        n_new = len(new_scopes)
        for i in range(n_new):
            for j in range(i + 1, n_new):
                sim = float(check_units[i] @ check_units[j])
                if sim >= SPLIT_DISTINCTION_THRESHOLD:
                    errors.append({
                        "title": definitions[i].get("title", ""),
                        "error": f"新Hub Scope 间区分度不足: cosine={sim:.4f} >= {SPLIT_DISTINCTION_THRESHOLD}",
                    })
            for j in range(n_new, len(check_units)):
                sim = float(check_units[i] @ check_units[j])
                if sim >= SPLIT_DISTINCTION_THRESHOLD:
                    existing_path = existing_hubs[j - n_new].path
                    errors.append({
                        "title": definitions[i].get("title", ""),
                        "error": f"与已有 Hub {existing_path} Scope 区分度不足: cosine={sim:.4f}",
                    })
    for definition in definitions:
        title = str(definition.get("title", "")).strip()
        scope = str(definition.get("scope", "")).strip()
        parent = str(definition.get("parent", "")).strip()
        members = definition.get("members", [])
        if not title or not scope:
            errors.append({"title": title or "(empty)", "error": "title 或 scope 为空"})
            continue
        if not title or not scope:
            continue
        path = f"academic/wiki/hubs/{title}"
        try:
            result = create_hub(
                conn, path=path, title=title, scope=scope,
                parent=parent, agent_confirmed=True,
            )
            created.append(result)
            all_members.extend(members)
        except (ValueError, PermissionError) as exc:
            errors.append({"title": title, "error": str(exc)})
            continue
    if all_members:
        membership = plan_memberships(conn, all_members)
        apply_membership_plan(conn, membership)
    return {
        "created": created,
        "errors": errors,
        "memberships_reapplied": len(all_members),
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    route = sub.add_parser("route")
    route.add_argument("page")
    profile = sub.add_parser("profile")
    profile.add_argument("node")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("hub")
    dynamics = sub.add_parser("dynamics-plan")
    dynamics.add_argument("--node", action="append", default=[])
    dynamics.add_argument("--apply-membership", action="store_true")
    split = sub.add_parser("split-plan")
    split.add_argument("hub")
    migrate = sub.add_parser("migrate-root-scopes")
    migrate.add_argument("--apply", action="store_true")
    create = sub.add_parser("create")
    create.add_argument("--path", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--scope", required=True)
    create.add_argument("--parent", default="")
    create.add_argument("--agent-confirmed", action="store_true")
    merge = sub.add_parser("merge")
    merge.add_argument("--survivor", required=True)
    merge.add_argument("--retired", required=True)
    merge.add_argument("--scope", required=True)
    merge.add_argument("--title", default="")
    merge.add_argument("--agent-confirmed", action="store_true")
    split_apply = sub.add_parser("split-apply")
    split_apply.add_argument("--parent", required=True)
    split_apply.add_argument("--plan", required=True, help="Agent 完成 title/Scope/members 的 JSON 文件")
    split_apply.add_argument("--agent-confirmed", action="store_true")
    auto_create = sub.add_parser("auto-create")
    auto_create.add_argument("--check", action="store_true", help="分析达标候选，输出 JSON")
    auto_create.add_argument("--node", action="append", default=[], help="仅检查受影响节点")
    auto_create.add_argument("--apply", metavar="FILE", help="Agent 完成的 hub 定义 JSON 文件")
    args = parser.parse_args()
    conn = gl.connect()
    try:
        if args.command == "route":
            _print(route_paper(conn, args.page))
        elif args.command == "profile":
            value = node_profile(conn, args.node) or read_paper_profile(args.node)
            _print(asdict(value) if value else {"node_id": args.node, "profile": None})
        elif args.command == "inspect":
            _print(inspect_hub(conn, args.hub))
        elif args.command == "dynamics-plan":
            result = dynamics_plan(
                conn, args.node or None, apply_membership=args.apply_membership,
            )
            if args.apply_membership:
                conn.commit()
            _print(result)
        elif args.command == "split-plan":
            _print(analyze_split(conn, args.hub))
        elif args.command == "migrate-root-scopes":
            result = migrate_root_scopes(conn, apply=args.apply)
            if args.apply:
                conn.commit()
            _print(result)
        elif args.command == "create":
            result = create_hub(
                conn, path=args.path, title=args.title, scope=args.scope, parent=args.parent,
                agent_confirmed=args.agent_confirmed,
            )
            conn.commit()
            _print(result)
        elif args.command == "merge":
            result = merge_hubs(
                conn, survivor=args.survivor, retired=args.retired, scope=args.scope,
                title=args.title, agent_confirmed=args.agent_confirmed,
            )
            conn.commit()
            _print(result)
        elif args.command == "auto-create":
            if args.check:
                result = auto_create_check(conn, args.node or None)
                _print(result)
            elif args.apply:
                definitions = json.loads(Path(args.apply).read_text(encoding="utf-8"))
                if isinstance(definitions, dict):
                    definitions = definitions.get("hubs", definitions.get("definitions", []))
                result = create_hubs_from_definitions(conn, definitions)
                conn.commit()
                _print(result)
        else:
            children = json.loads(Path(args.plan).read_text(encoding="utf-8"))
            if isinstance(children, dict):
                children = children.get("children", [])
            result = apply_split(
                conn, args.parent, children, agent_confirmed=args.agent_confirmed,
            )
            conn.commit()
            _print(result)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
