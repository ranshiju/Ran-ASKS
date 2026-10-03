#!/usr/bin/env python3
"""Bounded, read-only graph visualization with directed edges and source navigation."""
import argparse
import hashlib
import json
import os
import sys
import sqlite3
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / '.scripts'))
import graph_lib as gl

COLORS = {'page':'#3979bc','hub':'#c5424a','people':'#23956b','raw':'#c58627',
          'entity':'#677588','timeline-summary':'#8b65ab'}


def require(value, message):
    if not value:
        raise ValueError(message)


def parser(*, add_help=True):
    p = argparse.ArgumentParser(description=__doc__, add_help=add_help)
    p.add_argument('-o', '--output')
    p.add_argument('--html', action='store_true', help='Generate interactive local HTML')
    p.add_argument('--graph', choices=['main','private'], default='main')
    group = p.add_mutually_exclusive_group()
    group.add_argument('--node', help='Exact node path')
    group.add_argument('--query', help='Literal topic keyword in node title/path; multiple terms use AND')
    p.add_argument('--radius', type=int, default=1)
    p.add_argument('--max-nodes', type=int, default=80)
    p.add_argument('--max-edges', type=int, default=200)
    p.add_argument('--predicates', help='Comma separated exact relation types')
    p.add_argument('--types', help='Comma separated exact node types')
    p.add_argument('--sim', action='store_true', help='Include similarity navigation edges')
    p.add_argument('--dpi', type=int, default=150)
    p.add_argument('--open', action='store_true')
    return p


def build_graph(conn, args):
    """Filter, choose seeds, traverse, then cap; preserve each directed database edge."""
    import networkx as nx
    require(0 <= args.radius <= 4, 'radius must be 0..4')
    require(1 <= args.max_nodes <= 300, 'max-nodes must be 1..300')
    require(1 <= args.max_edges <= 1200, 'max-edges must be 1..1200')
    types = {s.strip() for s in (args.types or '').split(',') if s.strip()}
    predicates = {s.strip() for s in (args.predicates or '').split(',') if s.strip()}
    rows = {r['path']:dict(r) for r in conn.execute('SELECT path,title,type FROM nodes ORDER BY path')
            if not types or r['type'] in types}
    edges = [dict(r) for r in conn.execute('SELECT id,subject,predicate,object,source,confidence FROM edges ORDER BY id')
             if r['subject'] in rows and r['object'] in rows
             and (args.sim or r['predicate'] != '相似')
             and (not predicates or r['predicate'] in predicates)]
    adj = {n:set() for n in rows}
    for e in edges:
        adj[e['subject']].add(e['object']); adj[e['object']].add(e['subject'])
    if args.node:
        require(args.node in rows, 'Node missing or excluded by type filter: '+args.node)
        matches = [args.node]
    elif args.query is not None:
        terms = args.query.casefold().split()
        require(terms, 'Provide a nonempty topic keyword')
        matches = [n for n,r in rows.items() if all(t in (n+' '+(r['title'] or '')).casefold() for t in terms)]
        require(matches, 'No nodes match the topic keyword')
    else:
        matches = []
    seeds = matches[:min(10,args.max_nodes)]
    if seeds:
        distance = {s:0 for s in seeds}; queue = deque(seeds)
        while queue:
            n = queue.popleft()
            if distance[n] == args.radius: continue
            for other in sorted(adj[n]):
                if other not in distance:
                    distance[other] = distance[n]+1; queue.append(other)
        candidates = sorted(distance, key=lambda n:(distance[n],-len(adj[n]),n))
    else:
        candidates = sorted(rows, key=lambda n:(-len(adj[n]),n))
    chosen = candidates[:args.max_nodes]; selected = set(chosen)
    available_edges = [e for e in edges if e['subject'] in selected and e['object'] in selected]
    displayed = available_edges[:args.max_edges]
    graph = nx.MultiDiGraph()
    for n in chosen: graph.add_node(n, **rows[n])
    has_evidence = conn.execute("SELECT 1 FROM sqlite_master WHERE name='edge_evidence' AND type='table'").fetchone()
    for e in displayed:
        sources = [e['source']] if e['source'] else []
        if has_evidence:
            sources += [r[0] for r in conn.execute('SELECT source FROM edge_evidence WHERE edge_id=? ORDER BY source',(e['id'],)) if r[0]]
        graph.add_edge(e['subject'],e['object'],key=e['id'], **e, sources=sorted(set(sources)))
    graph.graph['selection'] = {'query':args.query,'node':args.node,'radius':args.radius,
        'types':sorted(types),'predicates':sorted(predicates),'include_similarity':args.sim,
        'matched_seeds':len(matches),'seeds':seeds,'omitted_seeds':len(matches)-len(seeds),
        'eligible_nodes':len(candidates),'shown_nodes':len(chosen),
        'omitted_nodes':len(candidates)-len(chosen),'eligible_edges_between_shown_nodes':len(available_edges),
        'shown_edges':len(displayed),'omitted_edges_between_shown_nodes':len(available_edges)-len(displayed),
        'max_nodes':args.max_nodes,'max_edges':args.max_edges,
        'truncated':len(matches)>len(seeds) or len(candidates)>len(chosen) or len(available_edges)>len(displayed)}
    return graph


def source_link(locator, output, scope):
    """Resolve only local knowledge files in the selected physical domain."""
    text = str(locator or ''); path, _, anchor = text.partition('#')
    raw = Path(path)
    if raw.is_absolute(): return {'locator':text,'href':None}
    roots = [REPO/'private/raw',REPO/'private/wiki'] if scope=='private' else [REPO/d/k for d in ('academic','admin','teaching','business') for k in ('raw','wiki')]
    for candidate in (REPO/raw, Path(str(REPO/raw)+'.md')):
        target = candidate.resolve()
        if target.is_file() and any(target.is_relative_to(root) for root in roots):
            href = quote(os.path.relpath(target, output.parent),safe='/')
            if anchor: href += '#'+quote(anchor,safe='-_:')
            return {'locator':text,'href':href}
    return {'locator':text,'href':None}


def payload(graph, positions, output, scope):
    nodes = []
    for n,d in graph.nodes(data=True):
        nodes.append({'id':n,'title':d['title'] or n,'type':d['type'],'color':COLORS.get(d['type'],'#677588'),
            'x':round(float(positions[n][0])*450+500,3),'y':round(float(positions[n][1])*340+380,3),
            'link':source_link(n,output,scope)})
    edges = []
    for s,t,k,d in graph.edges(keys=True,data=True):
        edges.append({'id':k,'subject':s,'object':t,'predicate':d['predicate'],'confidence':d['confidence'],
            'sources':[source_link(v,output,scope) for v in d['sources']]})
    return {'schema':'graph-visualization-v1','scope':scope,'selection':graph.graph['selection'],
            'nodes':nodes,'edges':edges,'legend':COLORS,
            'interpretation':'Navigation relationships; verify factual claims against Raw sources.'}


HTML = '''<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>知识图可视化</title><style>
body{margin:0;background:#f5f8fc;color:#24344a;font:15px system-ui}header{padding:16px 24px;background:white;border-bottom:1px solid #dce4ef}h1{font-size:23px;margin:0 0 8px}main{display:flex;height:calc(100vh - 155px)}svg{width:70%;background:white;touch-action:none;cursor:grab}aside{width:30%;overflow:auto;padding:18px;box-sizing:border-box}select,button{padding:6px;margin:4px}p{line-height:1.5}a{color:#2865a7;overflow-wrap:anywhere}.edge{stroke:#a3b1c3;fill:none;stroke-width:1.5}.node{cursor:pointer}.label{font-size:13px;fill:#24344a}li{margin-bottom:12px;overflow-wrap:anywhere}#notice{color:#88551b}small{color:#65768b}</style>
<header><h1>知识图可视化</h1><div id="summary"></div><div id="notice"></div><label>关系 <select id="relation"><option value="">全部</option></select></label><label>节点 <select id="node"><option value="">选择查看来源</option></select></label><button id="reset">重置视图</button><small>箭头表示关系方向 · 滚轮缩放 · 拖动平移 · 点击节点查看来源</small></header>
<main><svg id="view" viewBox="0 0 1000 760"><defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#8798ae"/></marker></defs><g id="edges"></g><g id="nodes"></g></svg><aside><div id="legend"></div><h2 id="title">节点与来源</h2><div id="detail">选择节点后查看文件位置、关系及证据地址。图中的联系用于导航，事实结论请回溯 Raw。</div></aside></main>
<script>const DATA=__DATA__;
const NS='http://www.w3.org/2000/svg',el=id=>document.getElementById(id),byId=new Map(DATA.nodes.map(n=>[n.id,n]));
function svg(tag,attrs,parent){const e=document.createElementNS(NS,tag);for(const[k,v]of Object.entries(attrs))e.setAttribute(k,v);parent.append(e);return e;}
function link(obj,parent){const e=document.createElement(obj.href?'a':'span');e.textContent=obj.locator;if(obj.href){e.href=obj.href;e.target='_blank';e.rel='noopener';}parent.append(e);}
function detail(id){const n=byId.get(id);if(!n)return;el('title').textContent=n.title;el('node').value=id;const box=el('detail');box.replaceChildren();link(n.link,box);const list=document.createElement('ul');box.append(list);for(const e of DATA.edges.filter(e=>e.subject===id||e.object===id)){const row=document.createElement('li');row.textContent=byId.get(e.subject).title+' → '+e.predicate+' → '+byId.get(e.object).title+(e.confidence?' ['+e.confidence+']':'');list.append(row);for(const s of e.sources){row.append(document.createElement('br'));link(s,row);}if(!e.sources.length){row.append(document.createElement('br'),'未记录证据地址');}}}
for(const t of [...new Set(DATA.edges.map(e=>e.predicate))].sort()){const o=document.createElement('option');o.value=t;o.textContent=t;el('relation').append(o);}
for(const n of DATA.nodes){const o=document.createElement('option');o.value=n.id;o.textContent=n.title;el('node').append(o);}
el('node').onchange=e=>detail(e.target.value);
function draw(){el('edges').replaceChildren();el('nodes').replaceChildren();const filter=el('relation').value,counts=new Map();for(const e of DATA.edges){if(filter&&filter!==e.predicate)continue;const s=byId.get(e.subject),t=byId.get(e.object),key=[s.id,t.id].sort().join('|'),i=counts.get(key)||0;counts.set(key,i+1);const dx=t.x-s.x,dy=t.y-s.y,len=Math.hypot(dx,dy)||1,off=18+18*i;let d;if(s.id===t.id)d=`M ${s.x} ${s.y-8} C ${s.x-40-off} ${s.y-65-off},${s.x+40+off} ${s.y-65-off},${s.x+8} ${s.y}`;else d=`M ${s.x+dx/len*9} ${s.y+dy/len*9} Q ${(s.x+t.x)/2-dy/len*off} ${(s.y+t.y)/2+dx/len*off} ${t.x-dx/len*11} ${t.y-dy/len*11}`;const p=svg('path',{d,class:'edge','marker-end':'url(#arrow)'},el('edges'));svg('title',{},p).textContent=s.title+' → '+e.predicate+' → '+t.title;}
for(const n of DATA.nodes){const g=svg('g',{class:'node'},el('nodes'));svg('circle',{cx:n.x,cy:n.y,r:8,fill:n.color},g);svg('text',{x:n.x+12,y:n.y+4,class:'label'},g).textContent=n.title.length>20?n.title.slice(0,20)+'…':n.title;svg('title',{},g).textContent=n.title;g.onclick=()=>detail(n.id);}}
el('relation').onchange=draw;const m=DATA.selection;el('summary').textContent=m.shown_nodes+' 节点 · '+m.shown_edges+' 条有向关系'+(m.query?' · 关键词：'+m.query:'');el('notice').textContent=m.truncated?'已按上限截取：省略匹配种子 '+m.omitted_seeds+'，候选节点 '+m.omitted_nodes+'，所示节点间关系 '+m.omitted_edges_between_shown_nodes:'当前选择范围完整显示';
for(const[type,color]of Object.entries(DATA.legend)){if(!DATA.nodes.some(n=>n.type===type))continue;const p=document.createElement('span');p.textContent='● '+type+'  ';p.style.color=color;el('legend').append(p);}
let vb=[0,0,1000,760],drag=null;const view=el('view'),update=()=>view.setAttribute('viewBox',vb.join(' '));view.onwheel=e=>{e.preventDefault();const f=e.deltaY>0?1.15:1/1.15;vb=[vb[0]+vb[2]*(1-f)/2,vb[1]+vb[3]*(1-f)/2,vb[2]*f,vb[3]*f];update();};view.onpointerdown=e=>{drag=[e.clientX,e.clientY,...vb];view.setPointerCapture(e.pointerId);};view.onpointermove=e=>{if(!drag)return;vb[0]=drag[2]-(e.clientX-drag[0])*vb[2]/view.clientWidth;vb[1]=drag[3]-(e.clientY-drag[1])*vb[3]/view.clientHeight;update();};view.onpointerup=()=>drag=null;el('reset').onclick=()=>{vb=[0,0,1000,760];update();};draw();
</script></html>'''


def render_png(graph, positions, output, args):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import networkx as nx
    from matplotlib.lines import Line2D
    from matplotlib.patches import FancyArrowPatch
    fig,ax=plt.subplots(figsize=(14,10),dpi=args.dpi)
    try:
        for font in ('Hiragino Sans GB','Noto Sans CJK SC','Arial Unicode MS'):
            from matplotlib.font_manager import findfont,FontProperties
            try: findfont(FontProperties(family=font),fallback_to_default=False)
            except ValueError: continue
            plt.rcParams['font.sans-serif']=[font];break
        counts={}
        for s,t,k,d in graph.edges(keys=True,data=True):
            pair=tuple(sorted((s,t)));i=counts.get(pair,0);counts[pair]=i+1
            start, end = positions[s], positions[t]
            rad = 0.14+0.13*i
            if s == t:
                start = (start[0]-.03, start[1]+.02)
                end = (end[0]+.03, end[1]+.02)
                rad = -3-i
            arrow=FancyArrowPatch(start,end,connectionstyle=f'arc3,rad={rad}',
                arrowstyle='-|>',mutation_scale=12,shrinkA=9,shrinkB=9,color='#8798ae',alpha=.65)
            ax.add_patch(arrow)
            dx,dy=end[0]-start[0],end[1]-start[1]
            ax.text((start[0]+end[0])/2+dy*rad/2, (start[1]+end[1])/2-dx*rad/2,
                    d['predicate'],fontsize=6,color='#516177',ha='center')
        if graph:
            nx.draw_networkx_nodes(graph,positions,ax=ax,node_color=[COLORS.get(graph.nodes[n]['type'],'#677588') for n in graph],node_size=90)
            nx.draw_networkx_labels(graph,positions,ax=ax,labels={n:graph.nodes[n]['title'] or n for n in graph},font_size=8,font_family=plt.rcParams['font.sans-serif'][0])
        ax.legend(handles=[Line2D([],[],marker='o',linestyle='',color=c,label=t) for t,c in COLORS.items() if any(d['type']==t for _,d in graph.nodes(data=True))])
        ax.set_title(f'WikiGraph · {len(graph)} nodes · {graph.number_of_edges()} directed edges'+(' · truncated' if graph.graph['selection']['truncated'] else ''))
        ax.margins(.2);ax.axis('off');fig.tight_layout();fig.savefig(output,bbox_inches='tight')
    finally: plt.close(fig)


def output_path(args):
    suffix='.html' if args.html else '.png'
    root=REPO/('private/temp' if args.graph=='private' else 'temp')
    raw=Path(args.output).absolute() if args.output else root/('graph-'+datetime.now().strftime('%Y%m%dT%H%M%S%f')+suffix)
    require(not any(p.is_symlink() for p in [raw,*raw.parents]),'Symlink output is not allowed')
    path=raw.resolve()
    allowed=path.is_relative_to(root.resolve())
    if args.graph=='main':
        allowed=allowed or (path.is_relative_to((REPO/'projects').resolve()) and 'outputs' in path.relative_to(REPO/'projects').parts[:-1])
    require(allowed,'Output must stay in temp/project outputs; private output stays in private/temp')
    require(path.suffix==suffix,'Output suffix does not match selected format')
    require(not path.exists() and not path.with_suffix('.json').exists(),'Output exists; choose a new name')
    return path


def run(args):
    import networkx as nx
    require(50<=args.dpi<=300,'dpi must be 50..300')
    output=output_path(args)
    db=Path(gl.PRIVATE_GRAPH_DB if args.graph=='private' else gl.GRAPH_DB)
    conn=gl.connect(db,read_only=True)
    try:
        conn.execute('BEGIN')
        graph=build_graph(conn,args)
    finally: conn.close()
    positions=nx.spring_layout(graph,seed=42,iterations=80)
    data=payload(graph,positions,output,args.graph)
    data['database']=str(db.relative_to(REPO)) if db.is_relative_to(REPO) else str(db)
    output.parent.mkdir(parents=True,exist_ok=True)
    created_output = created_manifest = False
    try:
        if args.html:
            with output.open('x',encoding='utf-8') as f:
                created_output = True
                f.write(HTML.replace('__DATA__',json.dumps(data,ensure_ascii=False).replace('<','\\u003c')))
        else:
            with output.open('xb'):
                created_output = True
            render_png(graph,positions,output,args)
        data['artifact_sha256']=hashlib.sha256(output.read_bytes()).hexdigest()
        with output.with_suffix('.json').open('x',encoding='utf-8') as f:
            created_manifest = True
            json.dump(data,f,ensure_ascii=False,indent=2)
    except BaseException:
        if created_output: output.unlink(missing_ok=True)
        if created_manifest: output.with_suffix('.json').unlink(missing_ok=True)
        raise
    if args.open:
        import webbrowser
        webbrowser.open(output.as_uri())
    return {'status':'rendered','artifact':str(output),'manifest':str(output.with_suffix('.json')),
            'selection':data['selection'],'sources':[data['database']]}


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        result=run(args);code=0
    except (ValueError,OSError,ImportError,sqlite3.Error) as exc:
        result={'status':'error','error':str(exc)};code=2
    print(json.dumps(result,ensure_ascii=False,indent=2));return code


if __name__=='__main__':raise SystemExit(main())
