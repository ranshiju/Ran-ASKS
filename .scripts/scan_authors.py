#!/usr/bin/env python3
"""Scan academic paper wiki pages: compare frontmatter authors count vs raw author line.

Usage:
  python3 .scripts/scan_authors.py                 # scan all paper pages
  python3 .scripts/scan_authors.py <page.md>       # scan single page

Reports pages where |wiki_authors - raw_authors| >= 2 (likely truncation/串号).
"""
import os, re, glob, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_skeleton import extract_authors_from_text

WIKI_DIR = "academic/wiki/papers"

def extract_wiki_authors(path):
    with open(path, encoding='utf-8') as f:
        text = f.read()
    m = re.search(r'^authors:\s*\[(.*?)\]', text, re.MULTILINE | re.DOTALL)
    if not m:
        return None
    return re.findall(r'"([^"]+)"', m.group(1))

def resolve_raw_path(sources_line):
    p = sources_line.strip().lstrip('-').strip()
    if p.startswith('academic/'):
        return p
    if p.startswith('raw/'):
        return 'academic/' + p
    return None

def extract_raw_author_count(raw_path):
    if not raw_path or not os.path.exists(raw_path):
        return None
    with open(raw_path, encoding='utf-8') as f:
        lines = f.readlines()
    return len(extract_authors_from_text(''.join(lines)))

def scan_page(wiki_path):
    authors = extract_wiki_authors(wiki_path)
    if authors is None:
        return None
    with open(wiki_path, encoding='utf-8') as f:
        text = f.read()
    sm = re.search(r'^\s*-\s+(.+\.md)', text, re.MULTILINE)
    if not sm:
        return (len(authors), None, "[no sources .md]")
    raw_path = resolve_raw_path(sm.group(1))
    raw_count = extract_raw_author_count(raw_path)
    return (len(authors), raw_count, raw_path or "[unresolved]")

def main():
    args = sys.argv[1:]
    paths = args if args else sorted(glob.glob(f"{WIKI_DIR}/*.md"))
    print(f"{'file':<50} {'wiki':>4} {'raw':>4}")
    print("-" * 70)
    flagged = []
    for path in paths:
        result = scan_page(path)
        if result is None:
            continue
        wiki_n, raw_n, info = result
        flag = ""
        if raw_n is not None and abs(wiki_n - raw_n) >= 2:
            flag = " <<<< CHECK"
            flagged.append(path)
        print(f"{os.path.basename(path):<50} {wiki_n:>4} {str(raw_n):>4}  {info}{flag}")
    print(f"\n=== Flagged (diff>=2): {len(flagged)} ===")
    for f in flagged:
        print(f"  {f}")

if __name__ == '__main__':
    main()
