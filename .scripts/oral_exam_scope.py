"""Pure shared oral-exam scope design, derived graph and bounded card selection."""
from __future__ import annotations

import re


SCHEMA = "oral-scope-graph-v1"
EMPTY_DESIGN = {"knowledge_points": [], "competency_points": [], "links": []}
LIMITS = {"knowledge_points": 40, "competency_points": 20, "links": 80}
IDENTIFIER = re.compile(r"[\w-]{1,80}\Z")


def _identifier(value, field):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"非法 scope_design {field}")


def _text(value, field, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"scope_design {field} 必须为 1..{maximum} 字符文本")


def validate_design(design, cards, *, complete=False):
    if not isinstance(design, dict) or set(design) != set(EMPTY_DESIGN):
        raise ValueError("scope_design 必须包含 knowledge_points/competency_points/links")
    for field, maximum in LIMITS.items():
        if not isinstance(design[field], list) or len(design[field]) > maximum:
            raise ValueError(f"scope_design {field} 必须为列表，最多{maximum}项")
    knowledge = {}
    for point in design["knowledge_points"]:
        if not isinstance(point, dict) or set(point) != {"id", "label", "evidence"}:
            raise ValueError("知识点必须包含 id/label/evidence")
        _identifier(point["id"], "knowledge id")
        _text(point["label"], "knowledge label", 200)
        if point["id"] in knowledge:
            raise ValueError("知识点 id 重复")
        evidence = point["evidence"]
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 4:
            raise ValueError("知识点需要 1..4 个已确认卡片 Raw locator")
        for locator in evidence:
            _text(locator, "knowledge evidence", 500)
        if len(set(evidence)) != len(evidence):
            raise ValueError("知识点依据重复")
        knowledge[point["id"]] = point
    competencies = {}
    for point in design["competency_points"]:
        if not isinstance(point, dict) or set(point) != {"id", "behavior"}:
            raise ValueError("能力点必须包含 id/behavior，可观察行为由教师审核")
        _identifier(point["id"], "competency id")
        _text(point["behavior"], "competency behavior", 500)
        if point["id"] in competencies:
            raise ValueError("能力点 id 重复")
        competencies[point["id"]] = point
    objectives = {card["id"]: {item["locator"] for item in card["evidence"]} for card in cards}
    all_evidence = set().union(*objectives.values()) if objectives else set()
    for point in knowledge.values():
        if not set(point["evidence"]) <= all_evidence:
            raise ValueError("知识点依据只能引用目标卡片中的 Raw locator，不可使用备选导航或新增来源")
    seen = set()
    used_knowledge, used_competencies, used_objectives = set(), set(), set()
    linked_evidence = {point_id: set() for point_id in knowledge}
    for link in design["links"]:
        if not isinstance(link, dict) or set(link) != {"knowledge_id", "competency_id", "objective_id"}:
            raise ValueError("范围映射必须包含 knowledge_id/competency_id/objective_id")
        for field, point_id in link.items():
            _identifier(point_id, field)
        knowledge_id, competency_id, objective_id = (link["knowledge_id"], link["competency_id"], link["objective_id"])
        if knowledge_id not in knowledge or competency_id not in competencies or objective_id not in objectives:
            raise ValueError("范围映射引用了不存在的知识点、能力点或目标")
        key = knowledge_id, competency_id, objective_id
        if key in seen:
            raise ValueError("范围映射重复")
        seen.add(key)
        if not set(knowledge[knowledge_id]["evidence"]) & objectives[objective_id]:
            raise ValueError("知识点与映射目标必须共享已确认 Raw 依据")
        linked_evidence[knowledge_id].update(objectives[objective_id])
        used_knowledge.add(knowledge_id)
        used_competencies.add(competency_id)
        used_objectives.add(objective_id)
    if complete:
        if not knowledge or not competencies or used_objectives != set(objectives):
            raise ValueError("请先完成考核范围图准备：每个目标须映射知识点和可观察能力点")
        if used_knowledge != set(knowledge) or used_competencies != set(competencies):
            raise ValueError("考核范围图包含未关联目标的知识点或能力点")
        if any(not set(point["evidence"]) <= linked_evidence[point_id] for point_id, point in knowledge.items()):
            raise ValueError("知识点依据必须来自其关联目标，而非其他无关目标")


def build_graph(plan, *, objective_ids=None, compact=False):
    design = plan.get("scope_design")
    if design is None:
        return None
    cards = [card for card in plan["objectives"] if objective_ids is None or card["id"] in objective_ids]
    selected_ids = {card["id"] for card in cards}
    links = [link for link in design["links"] if link["objective_id"] in selected_ids]
    knowledge_ids = {link["knowledge_id"] for link in links}
    competency_ids = {link["competency_id"] for link in links}
    locators = {item["locator"] for card in cards for item in card["evidence"]}
    nodes = []
    edges = []
    for point in design["knowledge_points"]:
        if objective_ids is not None and point["id"] not in knowledge_ids:
            continue
        node_id = f"knowledge:{point['id']}"
        nodes.append({"id": node_id, "kind": "knowledge", "label": point["label"]})
        for locator in point["evidence"] if not compact else []:
            if objective_ids is None or locator in locators:
                locators.add(locator)
                edges.append({"source": node_id, "relation": "grounded_in", "target": f"raw:{locator}"})
    for point in design["competency_points"]:
        if objective_ids is None or point["id"] in competency_ids:
            nodes.append({"id": f"competency:{point['id']}", "kind": "competency", "label": point["behavior"]})
    for card in cards:
        node_id = f"objective:{card['id']}"
        nodes.append({"id": node_id, "kind": "objective", **({"label": card["goal"]} if not compact else {})})
        for item in card["evidence"] if not compact else []:
            edges.append({"source": node_id, "relation": "grounded_in", "target": f"raw:{item['locator']}"})
    for locator in sorted(locators) if not compact else []:
        nodes.append({"id": f"raw:{locator}", "kind": "raw", "locator": locator})
    for link in links:
        edges.append({"source": f"knowledge:{link['knowledge_id']}", "relation": "assessed_as",
                      "target": f"competency:{link['competency_id']}", "objective_id": link["objective_id"]})
        edge = {"source": f"competency:{link['competency_id']}", "relation": "assessed_by",
                "target": f"objective:{link['objective_id']}"}
        if edge not in edges:
            edges.append(edge)
    return {"schema": SCHEMA, "mapping_authority": "plan_design_not_knowledge_fact",
            "fact_authority": "Raw", **({"raw_anchors": "cards.evidence"} if compact else {}),
            "nodes": nodes, "edges": edges}


def select_cards(plan, pending_id, covered):
    cards = plan["objectives"]
    selected = [card for card in cards if card["id"] == pending_id]
    unvisited = [card for card in cards if card["id"] not in covered and card not in selected]
    design = plan.get("scope_design")
    if design:
        knowledge = {card["id"]: set() for card in cards}
        competencies = {card["id"]: set() for card in cards}
        for link in design["links"]:
            knowledge[link["objective_id"]].add(link["knowledge_id"])
            competencies[link["objective_id"]].add(link["competency_id"])
        if pending_id and unvisited:
            def affinity(card):
                return (len(knowledge[card["id"]] & knowledge[pending_id]),
                        len(competencies[card["id"]] & competencies[pending_id]))
            related = max(unvisited, key=affinity)
            if affinity(related) != (0, 0):
                selected.append(related)
                unvisited.remove(related)
        while unvisited and len(selected) < 3:
            visited = set(covered) | {card["id"] for card in selected}
            known_knowledge = set().union(*(knowledge[card_id] for card_id in visited if card_id in knowledge))
            known_competencies = set().union(*(competencies[card_id] for card_id in visited if card_id in competencies))

            def novelty(card):
                return (len(knowledge[card["id"]] - known_knowledge),
                        len(competencies[card["id"]] - known_competencies))
            candidate = max(unvisited, key=novelty)
            selected.append(candidate)
            unvisited.remove(candidate)
    for card in unvisited + [card for card in cards if card["id"] in covered]:
        if card not in selected and len(selected) < 3:
            selected.append(card)
    return selected
