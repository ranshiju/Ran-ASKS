#!/usr/bin/env python3
"""answer_judge.py — 答案生成 + 异构 judge 评分（v3: section 感知 + 防参数化泄漏）

v3 变更（vs v2）：
1. section 感知读取：根据 retrieved item 的 section 字段，只读该 section 的实际内容
   - B3 只检索 ## Navigation → 只读 Navigation（不泄露 Content，忠实模拟"只读摘要层"）
   - B5-nav 的 Nav+Content → 读 Navigation + Content
   - B1/B2/B4 的任意 section → 读对应 section
2. judge 能看到检索内容：retrieved_text 传入 judge，可逐项核对答案事实是否在检索内容中
3. evidence_traceable 加严：答案包含检索内容中不存在的具体事实（数字/人名/日期/方法名）→ 记 0

v8 变更：retrieved_text[:6000] + frontmatter 元数据注入
v9 变更：事实层与判断层分离（已废弃，"标注缺失"与"不拒绝返回"规则冲突致 LLM 过度保守）
v10 变更：去掉主观缺失判断，改为来源标注——检索到的事实必须返回并标注来源文件，
          仅完全不相关才说"证据不足"；judge 同步去掉"标注缺失"标准
v11 变更：生成与评分统一消费结构化 Evidence Profile，来源适用性结合具体问题判断
"""
import json, sys, os, argparse, time, re, urllib.request
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
api_base = api_key = ""
env = REPO / ".env"
if env.exists():
    for line in env.read_text().splitlines():
        if line.startswith("LLM_API_BASE="): api_base = line.split("=",1)[1].strip()
        if line.startswith("LLM_API_KEY="): api_key = line.split("=",1)[1].strip()

import tiktoken
ENC = tiktoken.get_encoding("o200k_base")
EVIDENCE_PROFILE_FIELDS = (
    "source_presence", "source_types", "version_status", "conflict_markers",
)

def parse_sections(page_path):
    """解析页面，返回 (frontmatter_text, {section_header: section_body})"""
    p = REPO / page_path if not Path(page_path).is_absolute() else Path(page_path)
    if not p.exists(): return "", {}
    text = p.read_text(encoding="utf-8")
    frontmatter = ""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            frontmatter = text[3:end].strip()
            text = text[end+3:]
    sections = re.split(r'^(## .+)$', text, flags=re.MULTILINE)
    sec_map = {}
    for i, sec in enumerate(sections):
        sec = sec.strip()
        if sec.startswith("## ") and i+1 < len(sections):
            sec_map[sec] = sections[i+1].strip()
    return frontmatter, sec_map


def _empty_evidence_profile():
    return {field: [] for field in EVIDENCE_PROFILE_FIELDS}


def _normalize_evidence_profile(value):
    """Accept only the shared, mechanically reportable Evidence Profile shape."""
    if not isinstance(value, dict):
        return None
    if any(not isinstance(value.get(field), list) for field in EVIDENCE_PROFILE_FIELDS):
        return None
    return {field: list(value[field]) for field in EVIDENCE_PROFILE_FIELDS}


def build_evidence_profile(retrieved):
    """Reconstruct the shared Evidence Profile for legacy experiment outputs."""
    profile = _empty_evidence_profile()
    disclosures = []
    seen = set()
    for item in retrieved:
        page = str(item.get("page", "")).strip() if isinstance(item, dict) else ""
        if not page or page in seen:
            continue
        seen.add(page)
        frontmatter, _ = parse_sections(page)
        if not frontmatter:
            profile["source_presence"].append({"page": page, "count": 0})
            profile["source_types"].append({"page": page, "type": "unknown"})
            profile["version_status"].append(
                {"page": page, "status": "unknown", "superseded_by": ""}
            )
            profile["conflict_markers"].append(
                {"page": page, "marker": "frontmatter unavailable"}
            )
            disclosures.append(f"页 {page} frontmatter 不可用")
            continue
        try:
            metadata = yaml.safe_load(frontmatter) or {}
        except yaml.YAMLError as exc:
            metadata = {}
            profile["conflict_markers"].append(
                {"page": page, "marker": "frontmatter parse error"}
            )
            disclosures.append(f"页 {page} frontmatter 解析失败: {exc}")
        if not isinstance(metadata, dict):
            metadata = {}

        sources = metadata.get("sources", [])
        source_count = len(sources) if isinstance(sources, list) else (1 if sources else 0)
        source_type = metadata.get("source_type", "unknown")
        status = metadata.get("status", "unknown")
        superseded_by = metadata.get("superseded_by", "")
        related = metadata.get("related", [])
        profile["source_presence"].append({"page": page, "count": source_count})
        profile["source_types"].append({"page": page, "type": source_type})
        profile["version_status"].append({
            "page": page, "status": status, "superseded_by": superseded_by,
        })
        if source_count == 0:
            disclosures.append(f"页 {page} 无 sources（无 Raw 回溯链）")
        if status == "deprecated" and not superseded_by:
            profile["conflict_markers"].append(
                {"page": page, "marker": "deprecated 无 superseded_by"}
            )
            disclosures.append(f"页 {page} deprecated 但无 superseded_by（版本链未闭合）")
        if isinstance(related, list) and related:
            profile["conflict_markers"].append(
                {"page": page, "marker": "has related", "targets": related}
            )
    return profile, disclosures


def resolve_evidence_context(result):
    """Prefer producer-supplied evidence facts, with a legacy deterministic fallback."""
    supplied = _normalize_evidence_profile(result.get("evidence_profile"))
    derived_profile, derived_disclosures = build_evidence_profile(result.get("retrieved", []))
    raw_disclosures = result.get("required_disclosures")
    disclosures = (
        [str(item) for item in raw_disclosures]
        if isinstance(raw_disclosures, list)
        else derived_disclosures
    )
    if supplied is not None:
        return supplied, disclosures, "supplied"
    return derived_profile, disclosures, "derived-from-retrieved-frontmatter"


def _evidence_context_text(evidence_profile, required_disclosures):
    return json.dumps({
        "evidence_profile": evidence_profile or _empty_evidence_profile(),
        "required_disclosures": required_disclosures or [],
    }, ensure_ascii=False, indent=2)

def read_section_content(page_path, section_spec, max_chars=3000):
    """根据 section_spec 读取对应内容，section 感知 + 兜底"""
    frontmatter, sec_map = parse_sections(page_path)
    parts = []

    if "Nav+Content" in section_spec or "Content(" in section_spec:
        if "## Navigation" in sec_map:
            parts.append(f"## Navigation\n{sec_map['## Navigation'][:max_chars]}")
        if "## Content" in sec_map:
            parts.append(f"## Content\n{sec_map['## Content'][:max_chars]}")
    elif section_spec == "## Navigation":
        if "## Navigation" in sec_map:
            parts.append(f"## Navigation\n{sec_map['## Navigation'][:max_chars]}")
    elif section_spec == "## Content":
        if "## Content" in sec_map:
            parts.append(f"## Content\n{sec_map['## Content'][:max_chars]}")
    elif section_spec == "frontmatter":
        parts.append(f"[frontmatter]\n{frontmatter[:max_chars]}")
    else:
        if section_spec in sec_map:
            parts.append(f"{section_spec}\n{sec_map[section_spec][:max_chars]}")
        elif "## Content" in sec_map:
            parts.append(f"## Content\n{sec_map['## Content'][:max_chars]}")
    # Fallback: if nothing found, read first 3 content sections + frontmatter key fields
    if not parts:
        if frontmatter:
            parts.append(f"[frontmatter]\n{frontmatter[:500]}")
        body_sections = []
        for header, body in sec_map.items():
            body_sections.append(f"{header}\n{body.strip()}")
        if body_sections:
            combined = "\n\n".join(body_sections[:2])
            parts.append(combined[:min(max_chars, 2000)])
    return "\n\n".join(parts)

def llm_call(model, messages, max_tokens=1000):
    payload = json.dumps({"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.1}).encode()
    req = urllib.request.Request(f"{api_base}/v1/chat/completions", data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[LLM ERROR: {e}]"

def generate_answer(question, retrieved_text, model, evidence_profile=None,
                    required_disclosures=None):
    prompt = f"""你是一个知识库问答系统。严格根据以下检索到的知识库内容回答问题。

核心原则：
1. 只使用检索内容中的信息回答，不使用自身知识补充检索内容中没有的事实。
2. 检索到的相关信息必须返回，不得因信息不完整而拒绝返回已有事实。回答中标注关键信息的来源文件。
3. 仅当检索内容完全不包含与问题相关的任何信息时，才回答"证据不足"。
4. Evidence Profile 是程序机械确认的证据状态，不是回答事实。结合具体问题判断来源类型、版本状态和关系标记的意义，并在回答中落实 required_disclosures。

回答要简洁准确。

结构化证据状态：
{_evidence_context_text(evidence_profile, required_disclosures)}

检索内容（每个页面的 [type/source_type/status] 为 frontmatter 元数据）：
{retrieved_text[:6000]}

问题：{question}
"""
    return llm_call(model, [{"role":"user","content":prompt}])

def judge_answer(question, gold_answer, gold_posture, sys_answer, retrieved_text, model,
                 evidence_profile=None, required_disclosures=None):
    prompt = f"""请评估系统回答的质量。评分三个维度，每项 0 或 1 分：

1. correctness：系统回答的核心事实是否与标准答案一致
2. posture_correct：表态是否正确
   - 标准表态为"确定回答"时：系统应给出确定答案（非"证据不足"）
   - 标准表态含"限定性"时：系统应返回已找到的事实（而非仅说"证据不足"）
3. evidence_traceable：系统回答是否**仅基于**检索内容
   - 逐项检查系统回答中的具体事实（数字、人名、日期、方法名称、机构名等）
   - 如果这些事实能在下方"检索内容"中找到 → 记 1
   - 如果系统回答包含检索内容中**不存在**的具体事实细节（即来自 LLM 自身参数化知识而非检索内容）→ 记 0
   - 系统回答为"证据不足"且未编造事实 → 记 1

Evidence Profile 只提供程序机械确认的证据状态。结合本题判断回答姿态是否与版本、来源和冲突标记相符，并检查 required_disclosures 是否得到体现；各元数据字段本身不构成全局权威排名。

结构化证据状态：
{_evidence_context_text(evidence_profile, required_disclosures)}

检索内容：
{retrieved_text[:6000]}

标准答案：{gold_answer}
标准表态：{gold_posture}
系统回答：{sys_answer}

输出JSON：{{"correctness": 0或1, "posture_correct": 0或1, "evidence_traceable": 0或1, "reason": "简要说明各项判断依据"}}
"""
    result = llm_call(model, [{"role":"user","content":prompt}])
    try:
        m = re.search(r'\{[^}]+\}', result, re.DOTALL)
        if m: return json.loads(m.group())
    except: pass
    return {"correctness": 0, "posture_correct": 0, "evidence_traceable": 0, "reason": result[:200]}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--queries", required=True)
    ap.add_argument("--gen-model", default="DeepSeek-V4-Flash-0731")
    ap.add_argument("--judge-model", default="Qwen3.7-Max")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    results = json.loads(Path(args.results).read_text(encoding="utf-8"))
    queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))
    q_map = {q["id"]: q for q in queries}

    judged = []
    for i, r in enumerate(results):
        q = q_map[r["id"]]
        retrieved_parts = []
        for c in r.get("retrieved", []):
            page = c["page"]
            sec = c.get("section", "## Content")
            fm, _ = parse_sections(page)
            fm_meta = ""
            title = page
            if fm:
                import yaml
                try:
                    fm_dict = yaml.safe_load(fm) or {}
                    fm_meta = f" [type={fm_dict.get('type','?')} source_type={fm_dict.get('source_type','?')} status={fm_dict.get('status','?')}]"
                    title = fm_dict.get('title', page)
                except:
                    pass
            content = read_section_content(page, sec)
            if content:
                retrieved_parts.append(f"[来源: {title}{fm_meta}]\n{content}")
        retrieved_text = "\n\n---\n\n".join(retrieved_parts)
        evidence_profile, required_disclosures, profile_source = resolve_evidence_context(r)

        sys_answer = generate_answer(
            q["question"], retrieved_text, args.gen_model,
            evidence_profile, required_disclosures,
        )

        gold_answer = q["gold"]["answer"]
        gold_posture = q["gold"].get("posture", "确定回答")
        scores = judge_answer(
            q["question"], gold_answer, gold_posture, sys_answer, retrieved_text,
            args.judge_model, evidence_profile, required_disclosures,
        )

        judged.append({
            "id": r["id"], "question": q["question"],
            "primary_dim": q["primary_dim"], "method": r.get("method","?"),
            "sys_answer": sys_answer[:500], "gold_answer": gold_answer[:200],
            "scores": scores, "retrieval_tokens": r.get("retrieval_tokens",0),
            "loops": r.get("loops",0), "posture": r.get("posture","?"),
            "stop_reason": r.get("stop_reason","?"),
            "retrieved_text_len": len(retrieved_text),
            "evidence_profile": evidence_profile,
            "required_disclosures": required_disclosures,
            "evidence_profile_source": profile_source,
        })
        print(f"  {i+1}/{len(results)} {r['id']} c={scores.get('correctness',0)} p={scores.get('posture_correct',0)} t={scores.get('evidence_traceable',0)} txt={len(retrieved_text)}", file=sys.stderr)
        time.sleep(0.5)

    Path(args.output).write_text(json.dumps(judged, ensure_ascii=False, indent=2), encoding="utf-8")
    n = len(judged)
    correct = sum(j["scores"].get("correctness",0) for j in judged)
    posture = sum(j["scores"].get("posture_correct",0) for j in judged)
    trace = sum(j["scores"].get("evidence_traceable",0) for j in judged)
    print(f"\n汇总({n}题): correctness={correct}/{n} posture={posture}/{n} traceable={trace}/{n}")

if __name__ == "__main__":
    main()
