#!/usr/bin/env python3
"""extract_abbreviations.py — 从 raw 关键段提取缩写配对(ABBR: Full Name)

对齐 INGEST.md 摄入读取范围: 只扫 Abstract + Method + Results 关键段,
跳过 Related Work / References / Appendix / Summary / Acknowledgments。
不读全文(原则: 缩写表与 wiki summary 同源,可追溯到 LLM 实际读的内容)。

段定位规则(同 INGEST 步骤):
1. grep -n "^## " 列标题
2. 按段名关键词定位 Abstract / Method / Results
3. 无段标题的扁平 letter(PRL/letter 类): 扫前 40%(LLM 实际读的范围)

提取模式(机械可验证):
- Full Name (ABBR): 如 "network renormalization (TNR)"
- ABBR (full name): 如 "TNR (tensor network renormalization)"
- 中文全称（ABBR）: 如 "矩阵乘积态(MPS)"
噪音过滤: ABBR 在扫描段内出现≥2次 + 停用词排除

用法:
  python3 extract_abbreviations.py <wiki-page>          # 提取并打印
  python3 extract_abbreviations.py <wiki-page> --apply   # 写入 frontmatter
  python3 extract_abbreviations.py --batch               # 批量回补所有 academic 论文页
"""
import re, sys, argparse, os, glob, subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

# 虚词: 取全称首字母时跳过(冠词/介词/连词)
FUNCTION_WORDS = {'the','a','an','of','for','and','or','in','on','to','by','with','from','as','is','are','that','this'}

# 原文拼写错误纠错表: 原文错误词 -> 正确拼写(仅收录确认的原文 typo)
# 原文拼写错误保留可追溯: full 写正确拼写, raw_form 记录原文错误形式
RAW_TYPO_FIXES = {
    'Decompostion': 'Decomposition',
    'Diferentiable': 'Differentiable',
    'Orbita': 'Orbital',
}

def fix_typo(full):
    """检查 full 里的词是否有已知原文拼写错误,返回 (纠正后full, 原文错误形式或None)。"""
    raw_forms = []
    fixed = full
    for typo, correct in RAW_TYPO_FIXES.items():
        if typo in fixed:
            fixed = fixed.replace(typo, correct)
            raw_forms.append(typo)
    raw_form = ', '.join(raw_forms) if raw_forms else None
    return fixed, raw_form

STOPWORDS = {
    'ONLINE','COLOR','COLOUR','LEFT','RIGHT','TOP','BOTTOM','CENTER','MIDDLE',
    'LONDON','AMSTERDAM','BARCELONA','BERLIN','PARIS','TOKYO','BEIJING','SHANGHAI',
    'NEW','YORK','BOSTON','CAMBRIDGE','OXFORD','SINGAPORE','SYDNEY','TORONTO',
    'PHYS','REV','LETT','PRL','PRB','PRX','NATURE','SCIENCE','ARXIV','IEEE','ACM',
    'EUROPHYSICS','ELSEVIER','SPRINGER','WORLD','SCIENTIFIC','PRESS',
    'NO','VOL','PP','ET','AL','IBID','FIG','TAB','APP','SEC','CH','CHAP','EQ',
    'DOI','URL','HTTP','HTTPS','HTML','PDF','XML','JSON','YAML',
    'ECS','NSFC','CAS','NSC','DFG','NSF','EPSRC','ARC','MEST',
}

# 关键段标题关键词(扫这些段)
KEY_SECTION_RE = re.compile(
    r'(?:abstract|introduction|method|framework|approach|algorithm|'
    r'result|experiment|numerical|calculation|model|setup|implementation|'
    r'preliminar)',
    re.I,
)
# 跳过段标题关键词
SKIP_SECTION_RE = re.compile(
    r'(?:reference|appendix|acknowledg|summary|conclusion|discussion|'
    r'related\s*work|bibliography|author\s*contrib|data\s*avail|'
    r'conflict\s*of\s*interest|statement)',
    re.I,
)


def find_raw(page_path):
    """从 wiki 页 frontmatter sources 找 raw 文件实际路径。"""
    txt = Path(page_path).read_text(encoding='utf-8')
    m = re.match(r'^---\n(.*?)\n---', txt, re.S)
    if not m: return None
    fm = m.group(1)
    sm = re.search(r'^sources:\s*\n((?:  - .+\n)+)', fm, re.M)
    if not sm: return None
    src_line = sm.group(1).strip().split('\n')[0].replace('  - ','').strip()
    candidates = [src_line, src_line.replace('raw/', 'academic/raw/'),
                  _REPO / src_line, _REPO / src_line.replace('raw/', 'academic/raw/')]
    for c in candidates:
        if isinstance(c, str): c = Path(c)
        if c.exists(): return str(c)
    slug = Path(page_path).stem
    r = subprocess.run(['find', str(_REPO), '-name', 'paper.md', '-path', f'*{slug}*'],
                       capture_output=True, text=True)
    paths = [p for p in r.stdout.strip().split('\n') if p]
    return paths[0] if paths else None


def locate_key_sections(raw_text):
    """定位关键段行范围,对齐 INGEST 读取范围(跳过 References/Appendix/Summary/Acknowledgments)。
    黑名单制: 扫所有段,只跳过显式排除段。因物理论文段标题常以方法名命名(如"## A. HOTRG"),
    白名单(只扫含 method 关键词段)会漏掉方法段。"""
    lines = raw_text.split('\n')
    headers = [(i, l) for i, l in enumerate(lines) if re.match(r'^#{1,3}\s+', l)]
    
    # 扁平 letter(≤1 header,仅标题): 整篇即 abstract+method+results(PRL/letter 类), 扫前 40%
    if len(headers) <= 1:
        cut = max(50, int(len(lines) * 0.4))
        return ['\n'.join(lines[:cut])]
    
    segments = []
    # Abstract = 标题后到第一个 ## 段(通常含 abstract)
    first_section_idx = headers[0][0]
    if first_section_idx > 5:
        pre = '\n'.join(lines[:first_section_idx])
        if pre.strip():
            segments.append(pre)
    
    for idx, (h_i, h_line) in enumerate(headers):
        title = re.sub(r'^#{1,3}\s+', '', h_line).strip()
        end_i = headers[idx+1][0] if idx+1 < len(headers) else len(lines)
        # 黑名单: 只跳过 References/Appendix/Summary/Acknowledgments/Related Work
        if SKIP_SECTION_RE.search(title):
            continue
        segments.append('\n'.join(lines[h_i:end_i]))
    return segments


def extract_from_segments(segments):
    """从关键段文本提取缩写配对。返回 [(abbr, full), ...]"""
    pairs = {}
    for seg in segments:
        # 模式1: Full Name (ABBR) — 允许首字母大写词(如 Higher-Order),用末尾 N 实义词验证首字母
        p1 = re.findall(r'([A-Za-z][a-z]{1,}(?:[-\s]+[A-Za-z]?[a-z]{1,}){1,5})\s*\(([A-Z][A-Z0-9-]{1,7})\)', seg)
        for full, abbr in p1:
            abbr = abbr.upper()
            if abbr in STOPWORDS or len(abbr) < 2: continue
            words = full.split()
            n = len(abbr)
            content_words = [w for w in words if w.lower() not in FUNCTION_WORDS]
            tail = content_words[-n:] if len(content_words) >= n else content_words
            initials = ''.join(part[0] for w in tail for part in w.split('-') if part)
            if initials.upper() == abbr:
                full_str = ' '.join(tail).title()
                fixed, raw_form = fix_typo(full_str)
                pairs[abbr] = (fixed, raw_form)
        # 模式2: ABBR (full name) — 同样允许首字母大写词
        p2 = re.findall(r'\b([A-Z]{2,8})\s*\(([A-Za-z][a-z]{1,}(?:[-\s]+[A-Za-z]?[a-z]{1,}){1,5})\)', seg)
        for abbr, full in p2:
            abbr = abbr.upper()
            if abbr in STOPWORDS: continue
            words = full.split()
            n = len(abbr)
            content_words = [w for w in words if w.lower() not in FUNCTION_WORDS]
            tail = content_words[-n:] if len(content_words) >= n else content_words
            initials = ''.join(part[0] for w in tail for part in w.split('-') if part)
            if initials.upper() == abbr:
                full_str = ' '.join(tail).title()
                fixed, raw_form = fix_typo(full_str)
                pairs[abbr] = (fixed, raw_form)
        # 模式3(中文): 中文全称（ABBR）
        p3 = re.findall(r'([\u4e00-\u9fa5]{2,12})[（(]([A-Z][A-Z0-9-]{1,7})[）)]', seg)
        for cn_full, abbr in p3:
            abbr = abbr.upper()
            if abbr in STOPWORDS: continue
            if abbr not in pairs:
                fixed, raw_form = fix_typo(cn_full)
                pairs[abbr] = (fixed, raw_form)
    
    # 噪音过滤: 扫描段内总出现≥2次(重要方法缩写会反复用)
    all_scan_text = ' '.join(segments)
    filtered = {}
    for abbr, full in pairs.items():
        total = len(re.findall(rf'\b{re.escape(abbr)}\b', all_scan_text))
        if total >= 2:
            filtered[abbr] = full
    return sorted(filtered.items())

def extract_for_page(page_path):
    raw_path = find_raw(page_path)
    if not raw_path or not os.path.exists(raw_path):
        return [], None
    raw = open(raw_path, encoding='utf-8', errors='ignore').read()
    segments = locate_key_sections(raw)
    return extract_from_segments(segments), raw_path


def apply_to_page(page_path, pairs):
    txt = Path(page_path).read_text(encoding='utf-8')
    m = re.match(r'(^---\n)(.*?)(\n---)', txt, re.S)
    if not m: return False
    fm = m.group(2)
    parts = []
    for a, pf in pairs:
        full_val = pf[0] if isinstance(pf, tuple) else pf
        raw_form = pf[1] if isinstance(pf, tuple) and pf[1] else None
        line = f'  - abbr: {a}\n    full: "{full_val}"'
        if raw_form:
            line += f'\n    raw_form: "{raw_form}"  # 原文拼写错误,已纠正为 full'
        parts.append(line)
    new_field = "abbreviations:\n" + "\n".join(parts) if parts else "abbreviations: []"
    if re.search(r'^abbreviations:', fm, re.M):
        fm = re.sub(r'^abbreviations:.*?(?=\n[a-z]|\Z)', new_field + "\n", fm, flags=re.M | re.S)
    elif re.search(r'^tags:', fm, re.M):
        fm = re.sub(r'(^tags:.*$)', r'\1\n' + new_field, fm, count=1, flags=re.M)
    else:
        fm = fm.rstrip() + "\n" + new_field
    new_txt = m.group(1) + fm + m.group(3) + txt[m.end():]
    Path(page_path).write_text(new_txt, encoding='utf-8')
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('page', nargs='?', help='wiki 页路径')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--batch', action='store_true')
    args = ap.parse_args()
    
    if args.batch:
        pages = sorted(glob.glob(str(_REPO / 'academic/wiki/papers/*.md')))
        updated, skipped, no_raw = 0, 0, 0
        for p in pages:
            pairs, raw = extract_for_page(p)
            if not raw:
                no_raw += 1; continue
            if not pairs:
                skipped += 1; continue
            if apply_to_page(p, pairs):
                updated += 1
                print(f'  ✓ {Path(p).name}: {len(pairs)} abbreviations')
        print(f'\n批量完成: 更新 {updated}, 无缩写跳过 {skipped}, 无 raw {no_raw}')
        return
    
    if not args.page:
        print("用法: extract_abbreviations.py <page> [--apply] | --batch", file=sys.stderr)
        sys.exit(1)
    pairs, raw = extract_for_page(args.page)
    print(f"raw: {raw}")
    print(f"提取到 {len(pairs)} 个缩写:")
    for a, pf in pairs:
        full = pf[0] if isinstance(pf, tuple) else pf
        raw = pf[1] if isinstance(pf, tuple) and pf[1] else None
        suffix = f"  (原文拼作 {raw},已纠正)" if raw else ""
        print(f"  {a}: {full}{suffix}")
    if args.apply:
        apply_to_page(args.page, pairs)
        print(f"已写入 {args.page}")


if __name__ == '__main__':
    main()
