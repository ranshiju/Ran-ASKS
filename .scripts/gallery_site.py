#!/usr/bin/env python3
"""Build a standalone Gallery from exact-approved public assets; no network/model calls."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import yaml
import gallery_metrics

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
    for name in ('style.css', 'app.js', 'analytics.js'):
        public(f'gallery/site/{name}')
    public('gallery/RIGHTS.md')
    return catalog, works, inputs


def site_settings(root: Path, catalog: dict, token: str = '') -> dict:
    url = catalog.get('site_url', '')
    if not isinstance(url, str):
        raise ValueError('Gallery site_url must be a string')
    if url:
        parsed = urlparse(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.params or '\\' in url
                or any(c.isspace() for c in url) or '..' in unquote(parsed.path).split('/')):
            raise ValueError('Gallery site_url requires a canonical HTTPS URL without credentials, query or fragment')
        url = url.rstrip('/') + '/'
    if token and (not url or not re.fullmatch(r'[a-fA-F0-9]{32}', token)):
        raise ValueError('Cloudflare token requires a site_url and 32 hexadecimal characters')
    version_path = confined(root, 'VERSION')
    version = version_path.read_text().strip() if version_path.is_file() else 'development'
    if version != 'development' and not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Invalid Gallery source VERSION')
    return {'url': url, 'token': token, 'version': version}


def shell(title: str, description: str, body: str, prefix: str = '', *,
          site: dict | None = None, page: str = 'index.html', image: str = '') -> str:
    site = site or {'url': '', 'token': '', 'version': 'development'}
    canonical = urljoin(site['url'], page) if site['url'] else ''
    metadata = ''
    if canonical:
        metadata = (f'<link rel="canonical" href="{esc(canonical)}">'
                    f'<meta property="og:url" content="{esc(canonical)}">'
                    f'<meta name="gallery-site-url" content="{esc(site["url"])}">')
    if image and site['url']:
        metadata += f'<meta property="og:image" content="{esc(urljoin(site["url"], image))}">'
    if site['token']:
        metadata += f'<meta name="gallery-cloudflare-token" content="{esc(site["token"])}">'
    version = '开发预览' if site['version'] == 'development' else 'v' + site['version']
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · ASKS Gallery</title><meta name="description" content="{esc(description)}"><meta name="theme-color" content="#f7f7f2">
<meta property="og:type" content="website"><meta property="og:locale" content="zh_CN"><meta property="og:site_name" content="ASKS Gallery"><meta property="og:title" content="{esc(title)}"><meta property="og:description" content="{esc(description)}"><meta name="twitter:card" content="summary_large_image">{metadata}
<link rel="icon" href="{prefix}assets/mark.svg" type="image/svg+xml"><link rel="stylesheet" href="{prefix}assets/style.css"><script src="{prefix}assets/analytics.js" defer></script><script src="{prefix}assets/app.js" defer></script></head>
<body><a class="skip-link" href="#main">跳到主要内容</a>
<header class="site-header"><a class="brand" href="{prefix}index.html" aria-label="ASKS Gallery 首页"><span class="brand-mark" aria-hidden="true">A<span>↗</span></span><span>ASKS <b>GALLERY</b></span></a>
<nav aria-label="主导航"><a href="{prefix}index.html#works">作品</a><a href="{prefix}index.html#capabilities">功能</a><a href="{PUBLIC_REPO}">GitHub <span aria-hidden="true">↗</span></a></nav></header>
<main id="main">{body}</main>
<footer class="site-footer"><div><a class="footer-brand" href="{prefix}index.html">ASKS / GALLERY</a><p>每一件作品，都是知识的一种表达。</p></div><div class="footer-links"><a href="{prefix}rights.html">作品使用说明</a><a href="{prefix}privacy.html">访问统计说明</a><a href="{PUBLIC_REPO}/blob/main/README.zh-CN.md">当前系统说明 ↗</a><a href="{PUBLIC_REPO}/blob/main/CHANGELOG.md">更新日志 ↗</a><a href="mailto:sjran@cnu.edu.cn">联系作者 ↗</a></div><span class="footer-note">由 ASKS 创作 · 与你分享 · {esc(version)}</span></footer>
</body></html>'''


def tags(work: dict, caps: dict) -> str:
    return ''.join(f'<span class="tag">{esc(caps[c]["label"])}</span>' for c in work['capabilities'])


def statistics_section(metrics: dict, works: list[dict]) -> str:
    available = metrics['status'] != 'unavailable'
    cards = ''
    for days in ('7','30'):
        for key,label in [('page_views','浏览量'),('visits','访问次数')]:
            value = f'{metrics["windows"][days][key]:,}' if available else '—'
            cards += f'<div class="stat-card"><span>近{days}天{label}</span><strong>{value}</strong></div>'
    note = '统计尚未就绪，待取得汇总数据后显示。'
    popular = ''
    if available:
        note = ('更新于 '+metrics['generated_at'].replace('T',' ').replace('Z',' UTC')+
                ' · 按 UTC 自然日汇总，包含当日。')
        if metrics['status'] == 'stale':note += ' 最新刷新暂不可用，显示上次数据。'
        if metrics['status'] == 'empty':note += ' 暂未收到访问数据。'
        names={w['id']:w['presentation']['name'] for w in works}
        links=''.join(f'<li><a href="works/{item["id"]}/index.html">{esc(names[item["id"]])}</a>'
                      f'<span>{item["page_views"]:,} 次浏览</span></li>' for item in metrics['popular'])
        if links:popular=f'<div class="popular-works"><h3>近30天热门作品</h3><ol>{links}</ol></div>'
    return (f'<section class="statistics wrap" id="statistics"><div class="section-heading"><div>'
            '<p class="eyebrow">GALLERY IN NUMBERS</p><h2>展厅访问</h2></div>'
            '<a class="text-link" href="privacy.html">统计口径与选择 →</a></div>'
            f'<div class="stats-grid">{cards}</div><p class="stats-note">{esc(note)}</p>{popular}'
            '<p class="stats-note">访问次数不等于独立人数；脚本拦截与采样会影响统计。数据每日更新。</p></section>')


def index_page(catalog: dict, works: list[dict], site: dict | None = None, metrics: dict | None = None) -> str:
    caps = {c['id']: c for c in catalog['capabilities']}
    featured = next((w for w in works if w['id'] == catalog['featured']), None)
    hero_art = ''
    if featured:
        p = featured['presentation']; slug = featured['id']
        hero_art = f'''<a class="hero-art" href="works/{slug}/index.html" aria-label="查看{esc(p['name'])}"><span class="art-caption"><span>FEATURED / 精选</span><span>{esc(p['format_label'])}</span></span>
<div class="poster-stage"><img src="assets/works/{slug}/{esc(p['preview'])}" alt="{esc(p['name'])}作品预览" width="1800" height="2546" fetchpriority="high"></div><div class="art-bottom"><span>{esc(p['name'])}</span><span class="circle-arrow" aria-hidden="true">↗</span></div></a>'''
    hero = f'''<section class="hero wrap"><div class="hero-copy"><p class="eyebrow"><span class="status-dot"></span> MADE WITH ASKS</p><h1>让知识成为<br>看得见的<span class="accent">作品。</span></h1><p class="hero-description">文章、海报、报告与简历，呈现知识的不同表达。<br>探索使用 ASKS 创作的成果，找到适合你的功能。</p><a class="button primary" href="#works">浏览精选作品 <span aria-hidden="true">↓</span></a><div class="hero-footnote"><span class="tiny-rule"></span> 从真实成果出发，了解系统能做什么。</div></div>{hero_art}</section>'''
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
    image = f'assets/works/{featured["id"]}/{featured["presentation"]["cover"]}' if featured else ''
    stats = statistics_section(metrics or {'status':'unavailable'},works)
    return shell('精选作品', '使用 ASKS 制作的精选成果，按功能发现作品，查看成品与使用方式。', hero+stats+collection+capabilities, site=site, image=image)


def work_page(work: dict, catalog: dict, works: list[dict], site: dict | None = None) -> str:
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
    return shell(p['name'], p['summary'], body, '../../', site=site,
                 page=f'works/{work["id"]}/index.html', image=f'assets/works/{work["id"]}/{p["cover"]}')


def build(root: Path, output: Path, *, cloudflare_token: str = '', metrics: dict | None = None) -> dict:
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
    site = site_settings(root, catalog, cloudflare_token)
    metrics = gallery_metrics.clean_snapshot(metrics or {'schema':gallery_metrics.SCHEMA,'status':'unavailable'},set(catalog['works']))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.gallery-build-', dir=output.parent) as temporary:
        staging = Path(temporary)/'site';staging.mkdir()
        (staging/'assets').mkdir()
        (staging/'assets/mark.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><rect width="48" height="48" rx="3" fill="#203c3a"/><text x="12" y="36" fill="#f7f7f2" font-size="36" font-family="Georgia,serif">A</text></svg>',encoding='utf-8')
        for name in ('style.css','app.js','analytics.js'):
            shutil.copyfile(inputs[f'gallery/site/{name}'],staging/'assets'/name)
        (staging/'index.html').write_text(index_page(catalog,works,site,metrics),encoding='utf-8')
        (staging/'metrics.json').write_text(json.dumps(metrics,ensure_ascii=False,indent=2)+'\n')
        for work in works:
            slug=work['id'];p=work['presentation'];dest=staging/'works'/slug;dest.mkdir(parents=True)
            (dest/'index.html').write_text(work_page(work,catalog,works,site),encoding='utf-8')
            media=staging/'assets/works'/slug;media.mkdir(parents=True)
            for name in {p['cover'],p['preview'],*[d['file'] for d in p['downloads']],*[v['file'] for v in p.get('previews', [])]}:
                shutil.copyfile(inputs[f'gallery/works/{slug}/{name}'],media/name)
        rights=(root/'gallery/RIGHTS.md').read_text().split('\n\n')
        rights_html=''.join(f'<p>{esc(p)}</p>' for p in rights[1:])
        rights_html=rights_html.replace('[sjran@cnu.edu.cn](mailto:sjran@cnu.edu.cn)','<a href="mailto:sjran@cnu.edu.cn">sjran@cnu.edu.cn</a>')
        (staging/'rights.html').write_text(shell('作品使用说明','作品的展示、阅读和使用说明。',f'<section class="wrap prose"><p class="eyebrow">TERMS OF USE</p><h1>作品使用说明</h1>{rights_html}</section>',site=site,page='rights.html'),encoding='utf-8')
        status = ('本站已配置 Cloudflare Web Analytics，用于了解页面浏览、访问来源、设备及加载表现。'
                  if site['token'] else '本站尚未启用访问统计。')
        privacy = (f'<section class="wrap prose"><h1>访问统计说明</h1><p>{status}</p>'
                   '<p>统计只用于公开 Gallery 的展示改进，不接入你的知识库或任务内容。'
                   '启用时由 Cloudflare 处理统计请求；不记录搜索词，不使用 Cookie 或跨站个人标识。</p>'
                   '<p>页面浏览与访问次数反映趋势，不等同独立人数。当前不统计下载完成或指令复制。'
                   '直接打开 PDF、脚本拦截和采样可能影响统计范围。</p>'
                   '<p><a href="https://developers.cloudflare.com/web-analytics/about/">Cloudflare 统计说明</a></p>'
                   '<p><a href="privacy.html?analytics=off">在此浏览器停用统计</a> · '
                   '<a href="privacy.html?analytics=on">恢复允许统计</a></p>'
                   '<p data-analytics-status role="status"></p></section>')
        (staging/'privacy.html').write_text(shell('访问统计说明','公开 Gallery 的访问统计范围与浏览器选择。',privacy,site=site,page='privacy.html'),encoding='utf-8')
        pages = ['index.html','rights.html','privacy.html',*[f'works/{w["id"]}/index.html' for w in works]]
        if site['url']:
            locations = ''.join(f'<url><loc>{esc(urljoin(site["url"], p))}</loc></url>' for p in pages)
            (staging/'sitemap.xml').write_text('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'+locations+'</urlset>')
            (staging/'robots.txt').write_text('User-agent: *\nAllow: /\nSitemap: '+urljoin(site['url'],'sitemap.xml')+'\n')
        (staging/'site-info.json').write_text(json.dumps({'schema':'gallery-site-info-v1','version':site['version'],
            'works':len(works),'site_url':site['url'],'analytics_configured':bool(site['token']),
            'metrics_status':metrics['status']},ensure_ascii=False,indent=2)+'\n')
        (staging/'.nojekyll').write_text('')
        (staging/MARKER).write_text('asks-gallery-site-v1\n')
        backup=Path(temporary)/'previous'
        if output.exists():output.rename(backup)
        try:staging.rename(output)
        except OSError:
            if backup.exists():backup.rename(output)
            raise
    return {'status':'built','output':str(output),'version':site['version'],'works':len(works),
            'analytics_configured':bool(site['token']),'files':len([p for p in output.rglob('*') if p.is_file()])}


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=REPO/'temp/gallery-site')
    parser.add_argument('--cloudflare-token',default=os.environ.get('GALLERY_CLOUDFLARE_TOKEN',''),
                        help='Public Cloudflare Web Analytics site token; empty disables collection')
    parser.add_argument('--metrics',type=Path,help='Validated aggregate snapshot from gallery_metrics.py')
    args=parser.parse_args()
    try:result=build(REPO,args.output,cloudflare_token=args.cloudflare_token,
                     metrics=json.loads(args.metrics.read_text()) if args.metrics else None)
    except (ValueError,KeyError,TypeError,OSError) as error:parser.exit(2,f'ERROR: {error}\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
