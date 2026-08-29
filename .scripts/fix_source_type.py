#!/usr/bin/env python3
"""fix_source_type.py — 批量补齐缺失的 source_type(确定性映射,不调 LLM)

规则(源自 academic/SCHEMA.md source_type 表 + admin/SCHEMA.md 域默认 + INGEST.md 步骤31):
  1. sources 含 .txt 且路径含 conferences/ 或 discussions/ → speech-recognition (medium)
  2. type=web-reference 或 sources 含 raw/web-references/ 或有 url 字段 → web (medium)
  3. type=discussion → discussion (medium)
  4. 纯图片来源(无 .md/.pdf/.docx/.doc/.txt/.pptx) → ocr (medium)
  5. 其余 → official-doc (high);admin/business 域默认 official-doc

只补缺失字段:source_type 必补;若 confidence 也缺则按 source_type 默认值补。
不动已有字段,不改正文。frontmatter 行插入:source_type 紧跟 sources 块之后。
用法: fix_source_type.py [file1 file2 ...]  (无参则读 /tmp/missing_source_type.txt)
"""
import os, re, sys
from pathlib import Path

CONF_BY_ST = {"official-doc":"high","speech-recognition":"medium","ocr":"medium","web":"medium","discussion":"medium"}
TEXT_EXT = (".md",".pdf",".docx",".doc",".txt",".pptx",".xlsx",".xls")
IMG_EXT = (".jpg",".jpeg",".png",".gif",".bmp",".tiff")

def parse_fm(text):
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if not m: return None, text, None
    body = m.group(1)
    rest = text[m.end():]
    return body, rest, m

def get_field(body, key):
    m = re.search(r"^"+re.escape(key)+r":\s*(.*)$", body, re.M)
    return m.group(1).strip() if m else None

def get_sources(body):
    m = re.search(r"^sources:\s*\n((?:\s+-\s+.*\n)+)", body, re.M)
    if m:
        return [ln.strip().lstrip("- ").strip() for ln in m.group(1).splitlines()]
    m2 = re.search(r"^sources:\s*\[?(.*?)\]?\s*$", body, re.M)
    if m2:
        return [s.strip().strip('"') for s in m2.group(1).split(",") if s.strip()]
    return []

def determine(body):
    sources = get_sources(body)
    stype = get_field(body, "type")
    # 1 speech-recognition
    for s in sources:
        if ".txt" in s and ("conferences/" in s or "discussions/" in s):
            return "speech-recognition","medium"
    # 2 web
    if stype == "web-reference" or get_field(body,"url") is not None:
        return "web","medium"
    for s in sources:
        if "raw/web-references/" in s:
            return "web","medium"
    # 3 discussion
    if stype == "discussion":
        return "discussion","medium"
    # 4 ocr: 纯图片无文本
    has_text = any(s.lower().endswith(TEXT_EXT) for s in sources if s)
    has_img = any(s.lower().endswith(IMG_EXT) for s in sources if s)
    if has_img and not has_text:
        return "ocr","medium"
    # 5 default
    return "official-doc","high"

def insert_after_sources(body, st, conf_needed):
    lines = body.split("\n")
    out = []
    i = 0
    inserted = False
    while i < len(lines):
        out.append(lines[i])
        if lines[i].startswith("sources:") and not inserted:
            # 跳过列表项
            j = i + 1
            while j < len(lines) and (lines[j].startswith("  - ") or lines[j].strip()==""):
                if lines[j].strip()=="" :
                    break
                out.append(lines[j]); j += 1
            # 插入 source_type
            out.append("source_type: "+st)
            if conf_needed:
                out.append("confidence: "+CONF_BY_ST[st])
            inserted = True
            i = j
            continue
        i += 1
    if not inserted:
        # fallback: 插在首行后
        out = [lines[0], "source_type: "+st] + (["confidence: "+CONF_BY_ST[st]] if conf_needed else []) + lines[1:]
    return "\n".join(out)

def process(path):
    text = path.read_text(encoding="utf-8")
    body, rest, m = parse_fm(text)
    if body is None:
        return path.name, "SKIP(no fm)"
    if re.search(r"^source_type:", body, re.M):
        return path.name, "HAS"
    st, conf = determine(body)
    conf_needed = get_field(body,"confidence") is None
    new_body = insert_after_sources(body, st, conf_needed)
    new_text = "---\n"+new_body+"\n---\n"+rest
    path.write_text(new_text, encoding="utf-8")
    return path.name, f"+{st}"+(f"/{conf}" if conf_needed else "")

def main():
    if len(sys.argv) > 1:
        files = [Path(a) for a in sys.argv[1:]]
    else:
        files = [Path(l.strip()) for l in open("/tmp/missing_source_type.txt") if l.strip()]
    counts = {}
    for p in files:
        name, res = process(p)
        counts[res] = counts.get(res,0)+1
    for k,v in sorted(counts.items()):
        print(f"{v:4d}  {k}")
    print(f"TOTAL {sum(counts.values())}")

if __name__ == "__main__":
    main()
