#!/usr/bin/env python3
"""ingest_check.py — 摄入即时校验(只读,不修改任何文件)

职责边界:只校验本次摄入文件的**确定性结构正确性**(frontmatter/section/wikilink/
triples 语法/status 一致),不查语义,不做全局健康(那是离线 LINT 的事)。
失败输出结构化错误报告,交 LLM 定向修复;全过才提交。见
`operations/INGEST.md`「收尾」与 `notes/design-discussion.md`「摄入管道分工」。

用法:
  ingest_check.py <file1> [file2 ...] [dir1 ...]
  ingest_check.py --graph <file1> [file2 ...] [dir1 ...]
  - 文件:逐个校验
  - 目录:递归收集 .md(跳过 raw/、.git/、node_modules/)
退出码:0 = 无 ERROR(WARN 可有);1 = 有 ERROR;2 = 用法错误

严重度:
  ERROR  阻断提交(确定性硬错:缺必填字段/枚举非法/status 断链/section 重名/新页缺段)
  WARN   建议(可能合法:旧页缺段/悬空 wikilink/sources 路径/代码块 ## /triples 语法)
"""
import os
import re
import sys
import yaml
from datetime import date
from pathlib import Path
import sys as _sys; _sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl
import hub_semantics as hs
import wiki_locator as wl

REPO = Path(__file__).resolve().parent.parent

# ---- 规则表(源自各 SCHEMA.md Frontmatter 模板,按域分组) ----
# type/status 合法值按子项目域区分(academic/admin/business/teaching 各自 SCHEMA 定义);
# 路径首段定域,未知域(cross-domain/projects/agents 等)回落到全集并集避免误伤非知识页
TYPE_ENUM_BY_DOMAIN = {
    "academic": {"concept", "paper-summary", "people", "comparison", "review",
                 "review-guide", "conference-summary", "discussion", "web-reference", "research-project", "topic-hub"},
    "admin": {"policy", "procedure", "decision", "meeting-summary", "timeline-entry", "timeline-summary",
              "speech", "activity", "application", "profile", "reference", "web-reference", "topic-hub"},  # v4 加 timeline-summary; web-reference 2026-07-29
    "business": {"plan", "research", "competitor", "strategy", "project",
                 "meeting-summary", "contract", "financial", "web-reference", "topic-hub"},
    "teaching": {"course", "topic", "lecture", "assessment", "pedagogy", "web-reference", "topic-hub"},
    "cross-domain": {"topic-hub"},
    "private": {"health-record", "health-knowledge", "metaphysics-profile", "metaphysics-knowledge",
                "timeline-entry", "timeline-summary", "topic-hub", "web-reference"},
}
TYPE_ENUM_ALL = set().union(*TYPE_ENUM_BY_DOMAIN.values())
SOURCE_TYPE_ENUM = {"official-doc", "speech-recognition", "ocr", "web", "discussion", "user-assertion"}  # v4 加 user-assertion
CONFIDENCE_ENUM = {"high", "medium", "low"}
STATUS_ENUM_BY_DOMAIN = {
    "academic": {"current", "deprecated", "active", "completed", "draft"},
    "admin": {"active", "deprecated", "draft", "completed", "confirmed", "final"},
    "business": {"active", "completed", "archived", "draft"},
    "teaching": {"active", "completed", "draft", "current", "deprecated"},
    "cross-domain": {"active", "dormant", "archived"},
    "private": {"active", "deprecated", "draft", "completed", "confirmed", "final", "current"},
}
STATUS_ENUM_ALL = set().union(*STATUS_ENUM_BY_DOMAIN.values())
# 必填(ERROR):title/type/sources/source_type/date/status —— Q9 漏 source_type 即此类
REQUIRED = ["title", "type", "sources", "source_type", "date", "status"]
# Hub/导航聚合页(topic-hub 等):非源派生,不要求 sources/source_type/date
HUB_TYPES = {"topic-hub", "timeline-summary"}  # v4: 派生聚合页,经 contains 或语义边连通,无直接 raw; venue/institution 不再是 hub(2026-07-27)
REQUIRED_HUB = ["title", "type", "status", "created", "updated"]
# 软必填(WARN):模板列有但非阻断
SOFT_REQUIRED = ["confidence", "created", "updated"]  # v4: tags 已删
SECTION_CUTOVER = "2026-07-19"  # 标准 section 结构启用日(academic/SCHEMA.md)
STANDARD_SECTIONS = ["Navigation", "Content"]  # v4: Core Triples 段已删
TRIPLE_RE = re.compile(r"^.+?\s*→\s*.+?\s*→\s*.+?$")

# private 作为域段需后接知识库子目录(wiki/raw/topics 等),避免 macOS /private/var/... 误判
PRIVATE_SUBDIRS = {"wiki", "raw", "topics", "outputs"}


def domain_of(path):
    """从路径段判定子项目域;未知返回 None。

    private 需后接知识库子目录(wiki/raw/topics/outputs)才确认为 private 域,
    避免 macOS 临时目录 /private/var/... 的系统路径段误匹配。
    """
    parts = Path(path).parts
    for i, p in enumerate(parts):
        if p == "private":
            nxt = parts[i + 1] if i + 1 < len(parts) else None
            if nxt in PRIVATE_SUBDIRS:
                return p
            continue
        if p in TYPE_ENUM_BY_DOMAIN:
            return p
    return None


def type_enum_for(path):
    return TYPE_ENUM_BY_DOMAIN.get(domain_of(path), TYPE_ENUM_ALL)


def status_enum_for(path):
    return STATUS_ENUM_BY_DOMAIN.get(domain_of(path), STATUS_ENUM_ALL)


INFRA_BASENAMES = {
    "log.md", "index.md", "timeline.md", "README.md", "SCHEMA.md",
    "page-catalog.md", "_sync-state.md", "_index.md",
    "未归类关键词.md",  # catch-all 处理队列,基础设施工作文件,不进 graph.db(非知识页)
}
INFRA_PREFIXES = ("keyword-index", "triples", "outputs/")

def is_infra(path):
    """判断是否为基础设施文件(索引/日志/派生/规范),不校验 frontmatter。"""
    name = Path(path).name
    if name in INFRA_BASENAMES:
        return True
    sp = str(path)
    return any(p in sp for p in INFRA_PREFIXES)

def collect_files(args):
    files = []
    for a in args:
        p = Path(a)
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            for f in p.rglob("*.md"):
                sp = str(f)
                if "/raw/" in sp + "/" or sp.startswith("raw/") or "/.git/" in sp or "/node_modules/" in sp:
                    continue
                if is_infra(f):
                    continue
                files.append(f)
        else:
            print(f"WARN: 路径不存在,跳过: {a}", file=sys.stderr)
    return files


def parse_frontmatter(text):
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?", text, re.S)
    if not m:
        return None, None, text
    raw = m.group(1)
    try:
        fm = yaml.safe_load(raw)
        if not isinstance(fm, dict):
            fm = None
    except Exception as e:
        fm = None
    body = text[m.end():]
    return fm, raw, body


def split_code_blocks(body):
    """返回 (non_code_lines, code_has_hash2)。剥离 ``` 代码块内容,避免误判 ## section。"""
    lines = body.splitlines()
    in_code = False
    out = []
    code_has = False
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            out.append(ln)  # 保留围栏本身但不计入 section 提取
            continue
        if in_code:
            if re.match(r"^#{1,6}\s", ln):
                code_has = True
            continue
        out.append(ln)
    return out, code_has


def extract_h2_sections(non_code_lines):
    """返回 [(name, ...)]。section 名精确匹配 `## Name`(大小写敏感,标题无附加文字)。"""
    secs = []
    seen = {}
    for ln in non_code_lines:
        m = re.match(r"^##\s+(.+?)\s*$", ln)
        if m:
            name = m.group(1)
            secs.append(name)
            seen[name] = seen.get(name, 0) + 1
    return secs, seen


def build_wiki_index():
    """构建全库 wiki 文件索引,用于 wikilink 悬空检测。
    索引两类:(1) wiki 相对路径集(如 papers/foo)(2) 文件名集(foo.md)。
    wikilink [[a/b]] 优先按 wiki 相对路径解析,其次按文件名。"""
    rel_paths = set()
    basenames = set()
    for sub in ["academic", "admin", "teaching", "business"]:
        wdir = REPO / sub / "wiki"
        if not wdir.is_dir():
            continue
        for f in wdir.rglob("*.md"):
            rel = f.relative_to(wdir).with_suffix("").as_posix()  # papers/foo
            rel_paths.add(rel)
            basenames.add(f.name)
    # cross-domain 页面也可被引用(含 rel_paths 供 [[topics/xxx]] 解析)
    cd = REPO / "cross-domain"
    for f in cd.rglob("*.md"):
        rel = f.relative_to(cd).with_suffix("").as_posix()
        rel_paths.add(rel)
        basenames.add(f.name)
    # private 物理隔离域(自洽 wikilink 解析,不跨主库)
    pwdir = REPO / "private" / "wiki"
    if pwdir.is_dir():
        for f in pwdir.rglob("*.md"):
            rel = f.relative_to(pwdir).with_suffix("").as_posix()  # metaphysics/foo
            rel_paths.add(rel)
            basenames.add(f.name)
    return rel_paths, basenames


def resolve_wikilink(target, rel_paths, basenames):
    """target 已剥离 alias/锚点。返回是否可解析。"""
    if not target:
        return True  # 空目标(如 [[#anchor]])不算悬空
    # 精确 wiki 相对路径
    if target in rel_paths:
        return True
    # 去掉可能的 .md
    if target.endswith(".md") and target[:-3] in rel_paths:
        return True
    # 文件名兜底(宽松,避免误报)
    base = target.split("/")[-1]
    if base + ".md" in basenames:
        return True
    return False


def valid_partial_date(value, require_day=False):
    if not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value):
        return False
    if require_day and len(value) != 10:
        return False
    normalized = value if len(value) == 10 else f"{value}-01" if len(value) == 7 else f"{value}-01-01"
    try:
        date.fromisoformat(normalized)
    except ValueError:
        return False
    return True


def extract_wikilinks(text):
    """提取 [[...]],剥离 alias(|)与锚点(#)。返回 set of target。"""
    out = set()
    for m in re.finditer(r"\[\[([^\]]+)\]\]", text):
        t = m.group(1)
        t = t.split("|")[0]      # alias
        t = t.split("#")[0]      # 锚点
        t = t.strip()
        if t:
            out.add(t)
    return out


def check_file(path, rel_paths, basenames):
    errors = []
    warns = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception as e:
        return [f"读取失败: {e}"], []

    fm, raw_fm, body = parse_frontmatter(text)
    if fm is None:
        errors.append("frontmatter: 缺失或 YAML 解析失败(--- 块)")
        # 没有可用 frontmatter,后续检查无意义
        return errors, warns

    # --- frontmatter 必填字段 ---
    fm_type = fm.get('type', '')
    required = REQUIRED_HUB if fm_type in HUB_TYPES else REQUIRED
    for k in required:
        v = fm.get(k)
        if (k == "date" and k in fm and fm.get("date_status") == "unknown"
                and v in (None, "")):
            continue
        if v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, list) and len(v) == 0):
            errors.append(f"frontmatter: 必填字段缺失 '{k}'")
    # hub 是编译聚合页(无直接 raw 来源),confidence 语义不适用→跳过其软必填
    soft = [k for k in SOFT_REQUIRED if not (fm_type in HUB_TYPES and k == "confidence")]
    for k in soft:
        v = fm.get(k)
        if v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, list) and len(v) == 0):
            warns.append(f"frontmatter: 软必填字段缺失 '{k}'(模板列有,非阻断)")

    # --- 枚举值 ---
    type_enum = type_enum_for(path)
    if "type" in fm and fm["type"] not in type_enum:
        dom = domain_of(path) or "未知"
        errors.append(f"frontmatter: type 非法值 '{fm['type']}',合法: {sorted(type_enum)} (域: {dom})")
    if "source_type" in fm and fm["source_type"] not in SOURCE_TYPE_ENUM:
        errors.append(f"frontmatter: source_type 非法值 '{fm['source_type']}',合法: {sorted(SOURCE_TYPE_ENUM)}")
    if "confidence" in fm and fm["confidence"] not in CONFIDENCE_ENUM:
        errors.append(f"frontmatter: confidence 非法值 '{fm['confidence']}',合法: {sorted(CONFIDENCE_ENUM)}")
    status_enum = status_enum_for(path)
    if "status" in fm and fm["status"] not in status_enum:
        errors.append(f"frontmatter: status 非法值 '{fm['status']}',合法: {sorted(status_enum)} (域: {domain_of(path) or '未知'})")
    date_status = fm.get("date_status")
    if date_status not in (None, "unknown"):
        errors.append(f"frontmatter: date_status 非法值 '{date_status}'，合法: unknown")
    if date_status == "unknown":
        if fm.get("date") not in (None, ""):
            errors.append("frontmatter: date_status 为 unknown 时 date 必须为 null")
    elif "date" in fm:
        date_value = str(fm["date"])
        if not valid_partial_date(date_value):
            errors.append(f"frontmatter: 日期格式非法 '{date_value}'，应为有效 YYYY、YYYY-MM 或 YYYY-MM-DD")
    for field in ("created", "updated"):
        if field in fm:
            date_value = str(fm[field])
            if not valid_partial_date(date_value, require_day=True):
                errors.append(f"frontmatter: {field} 日期格式非法 '{date_value}'，应为有效 YYYY-MM-DD")
    effective_from = None
    effective_to = None
    for field in ("effective_from", "effective_to"):
        if field in fm and str(fm[field]).strip():
            date_value = str(fm[field])
            if not valid_partial_date(date_value, require_day=True):
                errors.append(f"frontmatter: {field} 日期格式非法 '{date_value}'，应为有效 YYYY-MM-DD")
            elif field == "effective_from":
                effective_from = date_value
            else:
                effective_to = date_value
    if effective_from and effective_to and effective_to < effective_from:
        errors.append("frontmatter: effective_to 早于 effective_from")
    if fm.get("created") and fm.get("updated") and str(fm["updated"]) < str(fm["created"]):
        errors.append("frontmatter: updated 早于 created")

    # --- status / superseded_by 一致性 ---
    status = fm.get("status")
    sup = fm.get("superseded_by")
    if status == "deprecated":
        if not sup or (isinstance(sup, str) and not sup.strip()):
            errors.append("frontmatter: status=deprecated 但无 superseded_by(版本链断裂)")
    elif sup and (isinstance(sup, str) and sup.strip()):
        warns.append(f"frontmatter: status='{status}' 非 deprecated 但填了 superseded_by(冗余字段)")

    # --- section 结构 ---
    non_code, code_has = split_code_blocks(body)
    secs, seen = extract_h2_sections(non_code)
    sec_set = set(secs)

    dup = [n for n, c in seen.items() if c > 1]
    if dup:
        errors.append(f"section: 同级 ## 重名 {dup}(read_section.sh 会误解析)")

    if code_has:
        warns.append("section: 代码块内出现 ## 标题(read_section.sh 可能误解析)")

    # Locator-aware pages opt into the minimal Wiki→Raw contract.  Temp wiki
    # drafts are checked by their pipeline with a final-Raw→temp override.
    try:
        is_temp_draft = Path(path).resolve().is_relative_to((REPO / "temp").resolve())
    except Exception:
        is_temp_draft = False
    if "[^" in text and not is_temp_draft:
        errors.extend(f"wiki-locator: {error}" for error in wl.validate_wiki_page(path))

    created = fm.get("created", "")
    created_str = str(created)
    is_new = created_str >= SECTION_CUTOVER  # 字符串比较 YYYY-MM-DD 有效

    if fm_type not in HUB_TYPES:
        for s in ["Navigation", "Content"]:
            if s not in sec_set:
                if is_new:
                    errors.append(f"section: 新页(created={created_str} ≥ {SECTION_CUTOVER})缺 '## {s}'(标准结构必备)")
                else:
                    warns.append(f"section: 缺 '## {s}'(旧页渐进迁移,建议补)")
        # v4: Core Triples 段已删,无 triples 语法校验(边在 graph.db)

    # --- wikilinks 悬空 ---
    links = extract_wikilinks(text)
    for t in sorted(links):
        if not resolve_wikilink(t, rel_paths, basenames):
            warns.append(f"wikilink: 悬空 [[{t}]](目标页未找到)")

    # --- sources 路径(仅内部路径) ---
    srcs = fm.get("sources", [])
    if not isinstance(srcs, list):
        srcs = [srcs]
    for s in srcs:
        ss = str(s).strip()
        if not ss:
            continue
        if ss.startswith("synology://") or ss.startswith("[[") or ss.startswith("http"):
            continue  # 外部/wikilink,跳过
        if any(ch in ss for ch in "*?[]"):
            continue  # glob 通配符(如 2010-*.md、**/*.pdf),非字面路径,跳过
        # 内部相对路径,检查存在(sources 相对子项目根,如 academic/raw/... 非 REPO/raw/...;
        # 未知域/cross-domain 回落 REPO/ss,兼容已含子项目前缀的路径)
        path_part = ss.split("#", 1)[0]
        if not path_part:
            continue
        dom = domain_of(str(path))
        candidates = [REPO / dom / path_part] if dom else []
        candidates.append(REPO / path_part)
        if not any(c.exists() for c in candidates):
            warns.append(f"sources: 内部路径不存在 '{ss}'")

    # --- 裸缩写 keyword 检查(缩写须在括号内,格式「中文英文(缩写)」) ---
    warns.extend(check_bare_abbreviation(fm))

    # --- 论文书目近端证据一致性（确定性冲突为 ERROR）---
    errors.extend(check_bibliographic_consistency(path, fm))

    # --- 覆盖度锚点检查(v6,2026-07-27,轻量grep非全读LLM) ---
    warns.extend(check_coverage_anchors(path, fm, body))

    # --- 提取引擎检查(v8,2026-07-30,非 mineru 提取的 raw 给 WARN) ---
    warns.extend(check_extract_engine(path, fm))

    return errors, warns


_APS_JOURNALS = {
    "physreva": "Phys. Rev. A", "physrevb": "Phys. Rev. B",
    "physrevc": "Phys. Rev. C", "physrevd": "Phys. Rev. D",
    "physreve": "Phys. Rev. E", "physrevlett": "Phys. Rev. Lett.",
    "revmodphys": "Rev. Mod. Phys.", "physrevx": "Phys. Rev. X",
    "prxquantum": "PRX Quantum",
}


def _aps_venue_from_doi(doi, year=""):
    match = re.fullmatch(
        r"10\.1103/([A-Za-z]+)\.(\d+)\.([A-Za-z0-9]+)", str(doi or "").strip(), re.I)
    if not match:
        return ""
    code, volume, article = match.groups()
    journal = _APS_JOURNALS.get(code.casefold(), "")
    if not journal:
        return ""
    suffix = f" ({year})" if re.fullmatch(r"(?:19|20)\d{2}", str(year or "")) else ""
    return f"{journal} {volume}, {article}{suffix}"


def _venue_key(value):
    value = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", str(value or "").strip())
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def check_bibliographic_consistency(path, fm):
    """用 raw 相邻 source.yaml 的 published/DOI 证据校验 paper frontmatter。"""
    if fm.get("type") != "paper-summary":
        return []
    srcs = fm.get("sources", [])
    if not isinstance(srcs, list):
        srcs = [srcs]
    raw_path = None
    for source in srcs:
        source = str(source).split("#", 1)[0].strip()
        if not source or source.startswith(("synology://", "[[", "http")):
            continue
        candidates = [REPO / source]
        dom = domain_of(str(path))
        if dom:
            candidates.insert(0, REPO / dom / source)
        raw_path = next((candidate for candidate in candidates
                         if candidate.is_file() and candidate.suffix == ".md"), None)
        if raw_path:
            break
    if not raw_path:
        return []
    source_yaml = raw_path.parent / "source.yaml"
    if not source_yaml.is_file():
        return []
    try:
        source_data = yaml.safe_load(source_yaml.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    bibliography = source_data.get("bibliographic")
    if not isinstance(bibliography, dict):
        return []
    evidence_lines = bibliography.get("first_page_evidence") or []
    if not isinstance(evidence_lines, list):
        evidence_lines = [evidence_lines]
    published_year = ""
    for line in evidence_lines:
        match = re.search(r"\bpublished\b.{0,64}?\b((?:19|20)\d{2})\b", str(line), re.I)
        if match:
            published_year = match.group(1)
            break
    expected_year = published_year or str(bibliography.get("year") or "").strip()
    expected_venue = str(bibliography.get("venue") or "").strip()
    if not expected_venue:
        expected_venue = _aps_venue_from_doi(bibliography.get("doi"), expected_year)
    errors = []
    if re.fullmatch(r"(?:19|20)\d{2}", expected_year) and str(fm.get("date") or "") != expected_year:
        errors.append(
            f"bibliographic: wiki date={fm.get('date')} 与 published year={expected_year} 冲突")
    if expected_venue and _venue_key(fm.get("venue")) != _venue_key(expected_venue):
        errors.append(
            f"bibliographic: wiki venue='{fm.get('venue', '')}' 与 DOI/近端证据 '{expected_venue}' 冲突")
    return errors


def check_bare_abbreviation(fm):
    """keyword 裸缩写校验:含英文缩写(≥2连续大写字母)的 keyword
    必须将缩写放入括号,格式「中文英文(缩写)」。裸缩写 → WARN(不阻断)。
    触发:keyword 含英文缩写且无括号。纯中文/纯英文不受约束。
    """
    warns = []
    kw_predicates = ("研究基础", "核心方法", "核心创新点", "局限性", "未来展望", "研究关键词")
    import re
    # 裸缩写判据:含≥2连续大写字母(或大写+数字的学术缩写如 TNR/HOTRG),且整体无括号释义
    abbr_re = re.compile(r"[A-Z]{2,}[A-Za-z0-9]*")
    paren_re = re.compile(r"[（(][^)）]*[)）]")
    for pred in kw_predicates:
        vals = fm.get(pred, [])
        if not isinstance(vals, list):
            vals = [vals]
        for kw in vals:
            kw = str(kw).strip()
            if not kw:
                continue
            if paren_re.search(kw):
                continue  # 有括号释义,合规
            if abbr_re.search(kw):
                warns.append(f"keyword: 裸缩写 '{kw}' 应写为「中文英文(缩写)」格式,见 INGEST.md keyword 写法")
    return warns


def check_coverage_anchors(path, fm, body):
    """轻量锚点覆盖检查(grep,非LLM全读)。
    从raw机械抽取硬锚点(标题/作者),检查wiki是否覆盖。
    缺失=WARN不阻断。仅paper-summary类且有paper.md源时触发。
    复用原则:此检查是脚本grep,不消耗LLM token;
    若LLM在上下文内已读raw,语义级覆盖判断由LLM复用上下文完成(见INGEST step8)。
    """
    warns = []
    if fm.get('type', '') != 'paper-summary':
        return warns
    srcs = fm.get('sources', [])
    if not isinstance(srcs, list):
        srcs = [srcs]
    raw_path = None
    for s in srcs:
        ss = str(s).strip()
        if not ss or ss.startswith(('synology://', '[[', 'http')):
            continue
        if any(ch in ss for ch in '*?[]'):
            continue
        dom = domain_of(str(path))
        cands = [REPO / dom / ss] if dom else []
        cands.append(REPO / ss)
        for c in cands:
            if c.exists() and c.suffix == '.md':
                raw_path = c
                break
        if raw_path:
            break
    if not raw_path:
        return warns
    try:
        raw_text = raw_path.read_text(encoding='utf-8', errors='replace')
    except Exception:
        return warns
    # 锚点1:raw标题(# 行) vs wiki frontmatter title
    raw_title = None
    for line in raw_text.splitlines():
        if line.startswith('# '):
            raw_title = line[2:].strip()
            break
    wiki_title = str(fm.get('title', ''))
    if raw_title and wiki_title:
        # 标题差异过大(无共同词)→WARN
        raw_words = set(re.findall(r'[A-Za-z]{3,}', raw_title.lower()))
        wiki_words = set(re.findall(r'[A-Za-z]{3,}', wiki_title.lower()))
        if raw_words and wiki_words and not (raw_words & wiki_words):
            warns.append(f"覆盖度: raw标题与wiki title无共同词(raw='{raw_title[:50]}')")
    raw_authors = []
    locked_authors_available = False
    source_yaml = raw_path.parent / "source.yaml"
    if source_yaml.is_file():
        try:
            source_data = yaml.safe_load(source_yaml.read_text(encoding="utf-8")) or {}
            bibliography = source_data.get("bibliographic") or {}
            review = bibliography.get("review") or {}
            locked_authors = bibliography.get("authors")
            if review.get("locked") is True and isinstance(locked_authors, list):
                locked_authors_available = True
                raw_authors = [str(name).strip() for name in locked_authors if str(name).strip()]
        except Exception:
            pass
    if not locked_authors_available:
        try:
            from wiki_skeleton import extract_authors_from_text
            raw_authors = extract_authors_from_text(raw_text)
        except Exception:
            raw_authors = []
    wiki_authors = fm.get('authors', [])
    if not isinstance(wiki_authors, list):
        wiki_authors = [wiki_authors]
    def author_key(value):
        return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value)).casefold()
    wiki_author_keys = {author_key(name) for name in wiki_authors if author_key(name)}
    missing_authors = [
        str(name).strip() for name in raw_authors
        if author_key(name) and author_key(name) not in wiki_author_keys
    ]
    if missing_authors and wiki_authors:
        warns.append("覆盖度: wiki authors 缺少raw书目作者: " + ", ".join(missing_authors))
    return warns


def check_extract_engine(path, fm):
    """检查 raw 全文 md 的提取引擎(v8,2026-07-30)。
    若 parse_meta.yaml 的 preferred 非 mineru → WARN(mineru 质量最高,见 extractor ENGINE_PRIORITY)。
    无 parse_meta.yaml 的 source 不触发(非 PDF 提取场景:会议纪要/docx/web 等)。
    """
    warns = []
    srcs = fm.get('sources', [])
    if not isinstance(srcs, list):
        srcs = [srcs]
    dom = domain_of(str(path))
    seen = set()
    for s in srcs:
        ss = str(s).strip()
        if not ss or ss.startswith(('synology://', '[[', 'http')):
            continue
        if any(ch in ss for ch in '*?[]'):
            continue
        path_part = ss.split('#', 1)[0]
        if not path_part:
            continue
        cands = [REPO / dom / path_part] if dom else []
        cands.append(REPO / path_part)
        raw_path = None
        for c in cands:
            if c.exists() and c.suffix == '.md':
                raw_path = c
                break
        if not raw_path:
            continue
        meta_path = raw_path.parent / "parse_meta.yaml"
        if not meta_path.exists() or str(meta_path) in seen:
            continue
        seen.add(str(meta_path))
        try:
            meta = yaml.safe_load(meta_path.read_text(encoding='utf-8', errors='replace'))
        except Exception:
            continue
        preferred = (meta or {}).get('preferred')
        if preferred and preferred != 'mineru':
            warns.append(f"引擎: raw 全文 md 由 '{preferred}' 生成(非 mineru),建议用 mineru 重提取保证质量: {meta_path.relative_to(REPO)}")
    return warns


def extract_section_body(non_code_lines, name):
    """从剥离代码块后的行中提取某 ## section 的正文(到下一个 ## 或文件尾)。"""
    out = []
    in_sec = False
    for ln in non_code_lines:
        m = re.match(r"^##\s+(.+?)\s*$", ln)
        if m:
            if in_sec:
                break
            if m.group(1) == name:
                in_sec = True
            continue
        if in_sec:
            out.append(ln)
    return "\n".join(out)


def graph_checks(path):
    """校验本页是否已入图，并做 paper 的确定性跨层一致性检查。"""
    import sqlite3
    try:
        rel = Path(path).resolve().relative_to(REPO).with_suffix("").as_posix()
    except ValueError:
        return ["graph: 页面不在仓库内，无法定位节点"], []
    # 按 db 选择:private 物理隔离,用 private/graph.db;其余用主库 cross-domain/graph.db
    # 用 ingest_check.REPO(测试可 monkeypatch),按 rel 域前缀定位库。
    if rel.startswith("private/"):
        db_path = REPO / "private" / "graph.db"
    else:
        db_path = REPO / "cross-domain" / "graph.db"
    if not db_path.exists():
        return ["graph: graph.db 不存在"], []
    conn = sqlite3.connect(db_path)
    try:
        node = conn.execute("SELECT 1 FROM nodes WHERE path=?", (rel,)).fetchone()
        if not node:
            return [f"graph: 缺页面节点 '{rel}'，先运行 graph_ingest.py ingest"], []
        bad_contains = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE (subject=? OR object=?) AND predicate='contains'",
            (rel, rel),
        ).fetchone()[0]
        errors = ["graph: 存在已废弃 contains 边"] if bad_contains else []
        warnings = []

        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            fm, _raw_fm, body = parse_frontmatter(text)
        except Exception:
            fm, body = {}, ""
        node_cols = {row[1] for row in conn.execute("PRAGMA table_info(nodes)")}

        # Core file graph: every internal Raw document package is a raw node,
        # and the Wiki page links to it directly.  Edge locator is optional.
        sources = fm.get("sources", []) if isinstance(fm, dict) else []
        if not isinstance(sources, list):
            sources = [sources]
        expected_raw_nodes = []
        for source in sources:
            raw_node = gl.raw_node_path(source, rel)
            if raw_node:
                expected_raw_nodes.append(raw_node)
        for raw_node in dict.fromkeys(expected_raw_nodes):
            raw_row = conn.execute(
                "SELECT type FROM nodes WHERE path=?", (raw_node,)
            ).fetchone()
            if not raw_row or raw_row[0] != "raw":
                errors.append(f"graph: 缺 Raw 文档包节点 '{raw_node}'")
                continue
            linked = conn.execute(
                "SELECT 1 FROM edges WHERE subject=? AND predicate='来源' AND object=?",
                (rel, raw_node),
            ).fetchone()
            if not linked:
                errors.append(f"graph: 缺 Wiki→来源→Raw 直连边 '{rel}' → '{raw_node}'")

        if fm.get("type") == "paper-summary" and {"title", "type"} <= node_cols:
            placeholder = re.compile(
                r"(?:未提供|未给出|未知|不详|待补|待定|wiki未|not\s*(?:provided|available|specified))",
                re.I,
            )

            def metadata_values(predicate, *, incoming=False):
                endpoint = "e.subject" if incoming else "e.object"
                node_join = "e.subject" if incoming else "e.object"
                where = "e.object=?" if incoming else "e.subject=?"
                rows = conn.execute(
                    f"SELECT {endpoint}, COALESCE(n.title, {endpoint}) AS label "
                    f"FROM edges e LEFT JOIN nodes n ON n.path={node_join} "
                    f"WHERE {where} AND e.predicate=?", (rel, predicate)
                ).fetchall()
                return [(row[0], str(row[1] or "").strip()) for row in rows]

            expected_authors = fm.get("authors", [])
            if not isinstance(expected_authors, list):
                expected_authors = [expected_authors]
            expected_authors = [str(name).strip() for name in expected_authors if str(name).strip()]
            graph_authors = []
            for predicate in ("作者", "第一作者", "通讯作者"):
                graph_authors.extend(metadata_values(predicate, incoming=True))
            graph_authors = list(dict.fromkeys(graph_authors))
            labels = [label for _node, label in graph_authors]
            bad_labels = [label for label in labels if placeholder.search(label.replace(" ", ""))]
            if bad_labels:
                errors.append(f"graph: 作者元数据含占位符节点 {sorted(set(bad_labels))}")

            def author_key(value):
                return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value)).casefold()

            table_names = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            identity_records = []
            for node_path, label in graph_authors:
                if placeholder.search(label.replace(" ", "")):
                    continue
                identities = {label, Path(node_path).name}
                if "aliases" in table_names:
                    identities.update(
                        str(row[0]) for row in conn.execute(
                            "SELECT alias FROM aliases WHERE node_path=?", (node_path,)
                        )
                    )
                identity_records.append((label, {
                    key for value in identities if (key := author_key(value))
                }))
            expected_by_key = {author_key(name): name for name in expected_authors if author_key(name)}
            missing_keys = {
                key for key in expected_by_key
                if not any(key in identities for _label, identities in identity_records)
            }
            expected_keys = set(expected_by_key)
            extra_labels = {
                label for label, identities in identity_records
                if identities.isdisjoint(expected_keys)
            }
            if expected_keys and (missing_keys or extra_labels):
                errors.append(
                    f"graph: 作者集合与 wiki frontmatter 不一致 "
                    f"(missing={sorted(expected_by_key[key] for key in missing_keys)}, "
                    f"extra={sorted(extra_labels)})"
                )

            venues = metadata_values("发表于")
            venue_labels = [label for _node, label in venues]
            bad_venues = [label for label in venue_labels if placeholder.search(label.replace(" ", ""))]
            if bad_venues:
                errors.append(f"graph: 期刊元数据含占位符节点 {sorted(set(bad_venues))}")
            expected_venue = str(fm.get("venue") or "").strip()
            if expected_venue:
                norm_expected = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", expected_venue).casefold()
                matched = any(
                    (norm := re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", label).casefold())
                    and (norm in norm_expected or norm_expected in norm)
                    for label in venue_labels if not placeholder.search(label.replace(" ", ""))
                )
                if not matched:
                    errors.append(
                        f"graph: 发表于边与 wiki venue 不一致 "
                        f"(wiki={expected_venue!r}, graph={venue_labels!r})"
                    )

            # 方向只作导航：新流程由可定位的研究方向句→canonical Hub Scope 路由。
            # 即时检查只守结构边界，不二次调用 embedding 重判语义；旧 Hub 无 Scope
            # 静默兼容，不产生迁移 WARN。
            direction_rows = conn.execute(
                "SELECT e.object, COALESCE(n.title,e.object), COALESCE(n.type,'') FROM edges e "
                "LEFT JOIN nodes n ON n.path=e.object WHERE e.subject=? AND "
                "e.predicate IN ('主要研究','涉及','紧密相关于','应用于','基于','贡献于','延伸至','探索','属于')",
                (rel,),
            ).fetchall()
            hub_defs = {}
            hubs_root = REPO / "academic" / "wiki" / "hubs"
            if hubs_root.is_dir():
                for hub_file in hubs_root.glob("*.md"):
                    try:
                        hub_text = hub_file.read_text(encoding="utf-8", errors="replace")
                        hub_fm, _raw, hub_body = parse_frontmatter(hub_text)
                    except Exception:
                        continue
                    title = str(hub_fm.get("title") or "").strip()
                    if not title:
                        continue
                    h1 = re.search(r"^#\s+(.+?)\s*$", hub_body, re.M)
                    scope = hs.read_hub_scope(hub_file)
                    hub_defs.setdefault(title, []).append({
                        "path": hub_file.relative_to(REPO).with_suffix("").as_posix(),
                        "h1": h1.group(1).strip() if h1 else "",
                        "scope": scope,
                    })
            for hub_path, hub_title, hub_type in direction_rows:
                if hub_type not in {"hub", "topic-hub"}:
                    errors.append(
                        f"graph: 方向边必须指向 Hub，当前 {hub_title!r} type={hub_type or 'missing'}"
                    )
                    continue
                defs = hub_defs.get(str(hub_title), [])
                if len(defs) != 1:
                    errors.append(f"graph: 方向 Hub 标题不唯一或缺失 {hub_title!r}: {[d['path'] for d in defs]}")
                    continue
                definition = defs[0]
                if definition["path"] != hub_path or definition["h1"] != hub_title:
                    errors.append(
                        f"graph: 方向 Hub 跨层不一致 path={hub_path!r}, "
                        f"frontmatter={hub_title!r}, h1={definition['h1']!r}"
                    )
                    continue

        return errors, warnings
    finally:
        conn.close()


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(2)
    graph = "--graph" in args
    paths = [a for a in args if a != "--graph"]
    files = collect_files(paths)
    if not files:
        print("未找到任何 .md 文件", file=sys.stderr)
        sys.exit(2)

    rel_paths, basenames = build_wiki_index()

    total_err = total_warn = 0
    for f in files:
        try:
            rel = f.resolve().relative_to(REPO).as_posix()
        except Exception:
            rel = str(f)
        errs, warns = check_file(f, rel_paths, basenames)
        if graph:
            graph_errs, graph_warns = graph_checks(f)
            errs.extend(graph_errs)
            warns.extend(graph_warns)
        if not errs and not warns:
            print(f"  PASS  {rel}")
            continue
        tag = "FAIL " if errs else "WARN "
        print(f"{tag} {rel}  (E={len(errs)} W={len(warns)})")
        for e in errs:
            print(f"   ERROR  {e}")
            total_err += 1
        for w in warns:
            print(f"    warn  {w}")
            total_warn += 1

    print(f"\n汇总: {len(files)} 文件, ERROR={total_err}, WARN={total_warn}")
    sys.exit(1 if total_err else 0)


if __name__ == "__main__":
    main()
