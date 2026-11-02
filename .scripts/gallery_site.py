#!/usr/bin/env python3
"""Build a standalone Gallery from exact-approved public assets; no network/model calls."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yaml

REPO = Path(__file__).resolve().parent.parent
PUBLIC_REPO = 'https://github.com/ranshiju/Ran-ASKS'
MARKER = '.asks-gallery-site'
SLUG = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\Z')


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'Missing text: {label}')
    return value


def confined(root: Path, relative: str) -> Path:
    p = Path(relative)
    if p.is_absolute() or '..' in p.parts or '\\' in relative:
        raise ValueError(f'Unsafe path: {relative}')
    current = root
    for part in p.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f'Symlink not allowed: {relative}')
    if not current.resolve().is_relative_to(root.resolve()):
        raise ValueError(f'Path escapes source: {relative}')
    return current


def load_source(root: Path) -> tuple[dict, list[dict], dict[str, Path]]:
    manifest = yaml.safe_load((root / 'operations/engineering/open-source-manifest.yaml').read_text())
    import fnmatch
    includes = manifest['include']
    if any(rule.startswith('gallery/') and any(c in rule for c in '*?[') for rule in includes):
        raise ValueError('Gallery requires exact-file include rules')
    approved = set(includes)
    excludes = manifest.get('exclude', [])
    inputs: dict[str, Path] = {}

    def public(relative: str) -> Path:
        if relative not in approved or relative.startswith('gallery/local/') or any(fnmatch.fnmatch(relative, pat) for pat in excludes):
            raise ValueError(f'Not an approved public file: {relative}')
        path = confined(root, relative)
        if not path.is_file():
            raise ValueError(f'Missing file: {relative}')
        inputs[relative] = path
        return path

    catalog = json.loads(public('gallery/catalog.json').read_text())
    if catalog.get('schema') != 'gallery-catalog-v1':
        raise ValueError('Unsupported catalog schema')
    capabilities = catalog['capabilities']
    ids = [c['id'] for c in capabilities]
    if len(set(ids)) != len(ids) or any(not SLUG.fullmatch(i) for i in ids):
        raise ValueError('Invalid or duplicate capability ID')
    for c in capabilities:
        for k in ('label', 'description', 'doc'):
            text(c.get(k), k)
        doc = confined(root, c['doc'])
        if not c['doc'].startswith('operations/') or not doc.is_file():
            raise ValueError('Capability documentation must exist under operations/')
    work_ids = catalog['works']
    if len(set(work_ids)) != len(work_ids) or any(not SLUG.fullmatch(i) for i in work_ids):
        raise ValueError('Invalid or duplicate work ID')
    if catalog.get('featured') is not None and catalog['featured'] not in work_ids:
        raise ValueError('Featured work is not in the catalog')
    works = []
    for slug in work_ids:
        base = f'gallery/works/{slug}'
        m = json.loads(public(f'{base}/metadata.json').read_text())
        if m.get('schema') != 'gallery-work-v1' or m.get('id') != slug:
            raise ValueError(f'Invalid metadata: {slug}')
        for k in ('title', 'kind', 'language', 'selected_on'):
            text(m.get(k), k)
        if not SLUG.fullmatch(m['kind']):
            raise ValueError('Invalid work kind')
        related = m.get('related_works', [])
        if (not isinstance(related, list) or any(not isinstance(x, str) for x in related)
                or len(set(related)) != len(related) or slug in related
                or not set(related).issubset(work_ids)):
            raise ValueError('Related works must be distinct catalog IDs other than the current work')
        caps = m['capabilities']
        if not caps or len(set(caps)) != len(caps) or not set(caps).issubset(ids):
            raise ValueError('Unknown or duplicate work capability')
        for name, digest in m['files'].items():
            if Path(name).name != name or Path(name).suffix.lower() not in {'.md', '.png', '.jpg', '.jpeg', '.webp', '.pdf', '.pptx', '.svg'}:
                raise ValueError(f'Invalid artifact filename: {name}')
            path = public(f'{base}/{name}')
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError(f'Artifact hash mismatch: {name}')
        display = m['presentation']
        for k in ('name', 'summary', 'type_label', 'format_label', 'audience', 'prompt', 'source_note', 'credit', 'boundary'):
            text(display.get(k), k)
        for key in ('cover', 'preview'):
            if display[key] not in m['files'] or Path(display[key]).suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp'}:
                raise ValueError('Preview must reference a verified raster image')
        if 'previews' in display:
            if not isinstance(display['previews'], list) or not display['previews']:
                raise ValueError('Previews must be a non-empty list')
            for preview in display['previews']:
                text(preview.get('label'), 'preview label')
                if preview.get('file') not in m['files'] or Path(preview['file']).suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp'}:
                    raise ValueError('Preview must reference a verified raster image')
                if preview.get('document') not in {d['file'] for d in display['downloads']}:
                    raise ValueError('Preview document must reference a listed download')
        for download in display['downloads']:
            text(download.get('label'), 'download label')
            if download['file'] not in m['files']:
                raise ValueError('Download must reference a verified artifact')
        uses = [u['id'] for u in display['capability_uses']]
        if set(uses) != set(caps) or len(uses) != len(caps):
            raise ValueError('Capability descriptions must match work capabilities')
        for use in display['capability_uses']:
            text(use.get('text'), 'capability contribution')
        for ref in m['references']:
            parsed = urlparse(ref['url'])
            if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError('References require public HTTPS URLs')
        works.append(m)
    for name in ('style.css', 'app.js'):
        public(f'gallery/site/{name}')
    public('gallery/RIGHTS.md')
    return catalog, works, inputs


def shell(title: str, description: str, body: str, prefix: str = '') -> str:
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · ASKS Gallery</title><meta name="description" content="{esc(description)}"><meta name="theme-color" content="#f7f7f2">
<link rel="icon" href="{prefix}assets/mark.svg" type="image/svg+xml"><link rel="stylesheet" href="{prefix}assets/style.css"><script src="{prefix}assets/app.js" defer></script></head>
<body><a class="skip-link" href="#main">跳到主要内容</a>
<header class="site-header"><a class="brand" href="{prefix}index.html" aria-label="ASKS Gallery 首页"><span class="brand-mark" aria-hidden="true">A<span>↗</span></span><span>ASKS <b>GALLERY</b></span></a>
<nav aria-label="主导航"><a href="{prefix}index.html#works">作品</a><a href="{prefix}index.html#capabilities">功能</a><a href="{PUBLIC_REPO}">GitHub <span aria-hidden="true">↗</span></a></nav></header>
<main id="main">{body}</main>
<footer class="site-footer"><div><a class="footer-brand" href="{prefix}index.html">ASKS / GALLERY</a><p>每一件作品，都是知识的一种表达。</p></div><div class="footer-links"><a href="{prefix}rights.html">作品使用说明</a><a href="{PUBLIC_REPO}">关于 ASKS ↗</a><a href="mailto:sjran@cnu.edu.cn">联系作者 ↗</a></div><span class="footer-note">由 ASKS 创作 · 与你分享</span></footer>
</body></html>'''


def tags(work: dict, caps: dict) -> str:
    return ''.join(f'<span class="tag">{esc(caps[c]["label"])}</span>' for c in work['capabilities'])


def index_page(catalog: dict, works: list[dict]) -> str:
    caps = {c['id']: c for c in catalog['capabilities']}
    featured = next((w for w in works if w['id'] == catalog['featured']), None)
    hero_art = ''
    if featured:
        p = featured['presentation']; slug = featured['id']
        hero_art = f'''<a class="hero-art" href="works/{slug}/index.html" aria-label="查看{esc(p['name'])}"><span class="art-caption"><span>FEATURED / 精选</span><span>{esc(p['format_label'])}</span></span>
<div class="poster-stage"><img src="assets/works/{slug}/{esc(p['preview'])}" alt="{esc(p['name'])}作品预览" width="1800" height="2546" fetchpriority="high"></div><div class="art-bottom"><span>{esc(p['name'])}</span><span class="circle-arrow" aria-hidden="true">↗</span></div></a>'''
    hero = f'''<section class="hero wrap"><div class="hero-copy"><p class="eyebrow"><span class="status-dot"></span> MADE WITH ASKS</p><h1>让知识成为<br>看得见的<span class="accent">作品。</span></h1><p class="hero-description">一张海报，一份简历，一次新的表达。<br>探索使用 ASKS 创作的成果，找到适合你的功能。</p><a class="button primary" href="#works">浏览精选作品 <span aria-hidden="true">↓</span></a><div class="hero-footnote"><span class="tiny-rule"></span> 从真实成果出发，了解系统能做什么。</div></div>{hero_art}</section>'''
    filter_buttons = '<button class="filter active" data-capability="all" aria-pressed="true">全部功能 <span>'+str(len(works))+'</span></button>'
    features = ''
    for n, c in enumerate(catalog['capabilities'], 1):
        count = sum(c['id'] in w['capabilities'] for w in works)
        if not count:
            continue
        filter_buttons += f'<button class="filter" data-capability="{c["id"]}" aria-pressed="false">{esc(c["label"])} <span>{count}</span></button>'
        features += f'''<article class="capability-card"><span class="cap-number">{n:02d}</span><h3>{esc(c['label'])}</h3><p>{esc(c['description'])}</p><a href="?capability={c['id']}#works">查看相关作品 <span aria-hidden="true">↗</span><span class="count-pill">{count}</span></a></article>'''
    kinds = dict((w['kind'], w['presentation']['type_label']) for w in works)
    options = ''.join(f'<option value="{esc(k)}">{esc(v)}</option>' for k, v in kinds.items())
    cards = ''
    for w in works:
        p = w['presentation']; slug = w['id']
        search = ' '.join([p['name'], w['title'], p['summary'], p['type_label'], *[caps[c]['label'] for c in w['capabilities']]])
        cards += f'''<article class="work-card" data-work data-capabilities="{' '.join(w['capabilities'])}" data-kind="{esc(w['kind'])}" data-search="{esc(search)}"><a class="card-image" href="works/{slug}/index.html" tabindex="-1" aria-hidden="true"><img src="assets/works/{slug}/{esc(p['cover'])}" alt="" width="640" height="905" loading="lazy"><span class="format-label">{esc(p['format_label'])}</span></a><div class="card-copy"><p class="eyebrow">{esc(p['type_label'])} <span class="muted">/ {esc(w['selected_on'][:4])}</span></p><h3><a href="works/{slug}/index.html">{esc(p['name'])}<span aria-hidden="true">↗</span></a></h3><p class="card-summary">{esc(p['summary'])}</p><div class="tags">{tags(w,caps)}</div><a class="text-link" href="works/{slug}/index.html">查看作品与功能 <span aria-hidden="true">→</span></a></div></article>'''
    collection = f'''<section class="collection wrap" id="works"><div class="section-heading"><div><p class="eyebrow">THE COLLECTION</p><h2>作品目录<span class="section-count">{len(works):02d}</span></h2></div><p>从作品出发，也可以按功能发现。</p></div><div class="collection-layout"><aside class="filter-panel" aria-label="按功能筛选"><p class="filter-title">创作功能</p><div class="filters">{filter_buttons}</div><p class="filter-hint">一件作品可以结合多种功能。</p></aside><div class="collection-main"><form class="search-bar" role="search"><label class="search-field"><span aria-hidden="true">⌕</span><span class="sr-only">搜索作品</span><input type="search" name="q" placeholder="搜索作品、主题或功能" autocomplete="off"></label><label class="type-field"><span class="sr-only">作品类型</span><select name="kind"><option value="all">全部类型</option>{options}</select></label></form><p class="result-count" role="status" aria-live="polite">显示 {len(works)} 件作品</p><div class="work-grid{' single-work' if len(works)==1 else ''}">{cards}</div><div class="empty-state" {'hidden' if works else ''}><span aria-hidden="true">∅</span><h3>还没有匹配的作品</h3><p>试试其他关键词，或浏览全部作品。</p><button class="button secondary" data-reset>清除筛选</button></div><noscript><p class="no-script">当前展示全部作品。启用 JavaScript 后可使用搜索与筛选。</p></noscript></div></div></section>'''
    capabilities = f'''<section class="capabilities wrap" id="capabilities"><div class="section-heading"><div><p class="eyebrow">EXPLORE BY CAPABILITY</p><h2>这些作品，用到了什么？</h2></div><p>了解功能如何参与创作。</p></div><div class="capability-grid">{features}</div></section><section class="closing wrap"><span class="eyebrow">YOUR NEXT IDEA</span><h2>下一件作品，从你的想法开始。</h2><p>带上论文或素材，告诉 ASKS：你想表达什么，想让谁看懂。</p><a class="text-link" href="{PUBLIC_REPO}#readme">了解 ASKS <span aria-hidden="true">↗</span></a></section>'''
    return shell('精选作品', '使用 ASKS 制作的精选成果，按功能发现作品，查看成品与使用方式。', hero+collection+capabilities)


def work_page(work: dict, catalog: dict, works: list[dict]) -> str:
    p = work['presentation']; slug = work['id']; caps = {c['id']: c for c in catalog['capabilities']}
    media = f'../../assets/works/{slug}/'
    downloads = ''.join(f'<a class="button primary" href="{media}{esc(d["file"])}" download>{esc(d["label"])} <span aria-hidden="true">↓</span></a>' for d in p['downloads'])
    contributions = ''.join(f'''<li><span class="use-number">{i:02d}</span><div><a href="{PUBLIC_REPO}/blob/main/{esc(caps[u['id']]['doc'])}">{esc(caps[u['id']]['label'])} <span aria-hidden="true">↗</span></a><p>{esc(u['text'])}</p></div></li>''' for i,u in enumerate(p['capability_uses'],1))
    refs = ''.join(f'<a href="{esc(r["url"])}">{esc(r.get("doi", "研究来源"))} ↗</a>' for r in work['references'])
    previews = p.get('previews', [{'file': p['preview'], 'label': p['name'] + ' · 完整预览'}])
    figures = []
    for preview in previews:
        document = preview.get('document')
        link = (f'<a href="{media}{esc(document)}">阅读全文 PDF ↗</a>' if document else
                f'<a href="{media}{esc(preview["file"])}" download>下载图片 ↓</a>')
        figures.append(f'''<figure class="artifact-preview"><figcaption>{esc(preview['label'])}</figcaption><a class="full-poster" href="{media}{esc(preview['file'])}" aria-label="查看{esc(preview['label'])}高清图片"><img src="{media}{esc(preview['file'])}" alt="{esc(preview['label'])}" loading="lazy"></a><div class="image-caption"><span>点击图片放大阅读</span>{link}</div></figure>''')
    visual = ''.join(figures)
    by_id = {w['id']: w for w in works}
    related_links = ''.join(
        f'<a class="related-work" href="../{related}/index.html"><span class="eyebrow">{esc(by_id[related]["presentation"]["type_label"])}</span>'
        f'<h3>{esc(by_id[related]["presentation"]["name"])} <span aria-hidden="true">↗</span></h3>'
        f'<p>{esc(by_id[related]["presentation"]["summary"])}</p></a>'
        for related in work.get('related_works', []))
    related_section = (f'<section class="related-section" aria-label="关联作品"><h2>关联作品</h2><div class="related-grid">{related_links}</div></section>'
                       if related_links else '')
    body = f'''<div class="wrap detail"><a class="back-link" href="../../index.html#works">← 返回作品目录</a><div class="detail-heading"><div><p class="eyebrow">{esc(p['type_label'])} / {esc(work['selected_on'][:4])}</p><h1>{esc(p['name'])}</h1></div><span class="detail-edition">精选作品 / {esc(work['language'])}</span></div><div class="detail-grid"><div class="detail-visual{' multi-preview' if len(previews) > 1 else ''}">{visual}</div><div class="detail-info"><h2>{esc(work['title'])}</h2><p class="detail-summary">{esc(p['summary'])}</p><dl class="work-facts"><div><dt>作品形式</dt><dd>{esc(p['format_label'])}</dd></div><div><dt>面向读者</dt><dd>{esc(p['audience'])}</dd></div></dl><div class="download-actions">{downloads}</div><div class="uses"><p class="eyebrow">MADE WITH</p><h2>使用的系统功能</h2><ol>{contributions}</ol></div></div></div><div class="detail-bottom"><section class="start-making"><p class="eyebrow">MAKE SOMETHING SIMILAR</p><h2>想制作类似作品？</h2><p>准备论文或素材，向 ASKS 提出你的需求。</p><blockquote id="starter-prompt">{esc(p['prompt'])}</blockquote><button class="button secondary" data-copy="starter-prompt">复制这段指令 <span aria-hidden="true">↗</span></button><span class="copy-status" role="status" aria-live="polite"></span></section><section class="source-note"><p class="eyebrow">ABOUT THE WORK</p><h2>作品与来源</h2><p>{esc(p['credit'])}</p><p>{esc(p['source_note'])}</p><div class="source-links">{refs}</div><p class="boundary-note">{esc(p['boundary'])}</p><a class="text-link" href="../../rights.html">作品使用说明 →</a></section></div></div>'''
    body = body.replace('<div class="detail-bottom">', related_section + '<div class="detail-bottom">', 1)
    return shell(p['name'], p['summary'], body, '../../')


def build(root: Path, output: Path) -> dict:
    root = root.resolve(); output = output.absolute()
    # Refuse overwriting source areas or arbitrary directories. Existing generated sites are replaceable.
    current = output
    while current != current.parent:
        if current.is_symlink():
            raise ValueError('Output may not contain symlinks')
        current = current.parent
    output = output.resolve()
    if output == root or root.is_relative_to(output) or (output.is_relative_to(root) and not output.is_relative_to(root/'temp')):
        raise ValueError('Output must be outside the source tree or under temp/')
    if output.exists() and (not output.is_dir() or not (output/MARKER).is_file() or (output/MARKER).is_symlink() or (output/MARKER).read_text() != 'asks-gallery-site-v1\n'):
        raise ValueError('Refusing to replace a directory without the Gallery build marker')
    catalog, works, inputs = load_source(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.gallery-build-', dir=output.parent) as temporary:
        staging = Path(temporary)/'site';staging.mkdir()
        (staging/'assets').mkdir()
        (staging/'assets/mark.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><rect width="48" height="48" rx="3" fill="#203c3a"/><text x="12" y="36" fill="#f7f7f2" font-size="36" font-family="Georgia,serif">A</text></svg>',encoding='utf-8')
        for name in ('style.css','app.js'):
            shutil.copyfile(inputs[f'gallery/site/{name}'],staging/'assets'/name)
        (staging/'index.html').write_text(index_page(catalog,works),encoding='utf-8')
        for work in works:
            slug=work['id'];p=work['presentation'];dest=staging/'works'/slug;dest.mkdir(parents=True)
            (dest/'index.html').write_text(work_page(work,catalog,works),encoding='utf-8')
            media=staging/'assets/works'/slug;media.mkdir(parents=True)
            for name in {p['cover'],p['preview'],*[d['file'] for d in p['downloads']],*[v['file'] for v in p.get('previews', [])]}:
                shutil.copyfile(inputs[f'gallery/works/{slug}/{name}'],media/name)
        rights=(root/'gallery/RIGHTS.md').read_text().split('\n\n')
        rights_html=''.join(f'<p>{esc(p)}</p>' for p in rights[1:])
        rights_html=rights_html.replace('[sjran@cnu.edu.cn](mailto:sjran@cnu.edu.cn)','<a href="mailto:sjran@cnu.edu.cn">sjran@cnu.edu.cn</a>')
        (staging/'rights.html').write_text(shell('作品使用说明','作品的展示、阅读和使用说明。',f'<section class="wrap prose"><p class="eyebrow">TERMS OF USE</p><h1>作品使用说明</h1>{rights_html}</section>'),encoding='utf-8')
        (staging/'.nojekyll').write_text('')
        (staging/MARKER).write_text('asks-gallery-site-v1\n')
        backup=Path(temporary)/'previous'
        if output.exists():output.rename(backup)
        try:staging.rename(output)
        except OSError:
            if backup.exists():backup.rename(output)
            raise
    return {'status':'built','output':str(output),'works':len(works),'files':len([p for p in output.rglob('*') if p.is_file()])}


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=REPO/'temp/gallery-site')
    args=parser.parse_args()
    try:result=build(REPO,args.output)
    except (ValueError,KeyError,TypeError,OSError) as error:parser.exit(2,f'ERROR: {error}\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
