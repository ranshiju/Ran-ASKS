#!/usr/bin/env python3
"""graph_visualize.py — graph.db 可视化

布局在 Python(networkx spring layout)端完成,支持两种输出:
  - PNG(matplotlib 静态图,默认)
  - HTML(canvas 交互图,--html):坐标由 Python 预算,浏览器零物理模拟

用法:
  graph_visualize.py                          # 全图 PNG
  graph_visualize.py --html                   # 全图交互 HTML
  graph_visualize.py --html -o out.html --open
  graph_visualize.py --node <path> --radius 1 # 自我中心图
  graph_visualize.py --sim                    # 含相似边(默认排除)
  graph_visualize.py --types page,people      # 仅含指定节点类型
  graph_visualize.py --dpi 200                # PNG 分辨率
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".scripts"))
import graph_lib as gl

SIMILAR_PRED = "相似"
TYPE_COLOR = {
    "page": "#5b8ff9",
    "hub": "#f5222d",
    "people": "#5ad8a6",
    "raw": "#f6a623",
    "entity": "#6e7681",
}
TYPE_ORDER = ["page", "hub", "people", "raw", "entity"]
TYPE_INDEX = {t: i for i, t in enumerate(TYPE_ORDER)}


def build_graph(conn, args):
    import networkx as nx

    include_sim = args.sim
    type_filter = set(args.types.split(",")) if args.types else None

    all_nodes = list(conn.execute(
        "SELECT path, title, type FROM nodes ORDER BY path"
    ))
    if type_filter:
        all_nodes = [r for r in all_nodes if r["type"] in type_filter]
    idx = {r["path"]: i for i, r in enumerate(all_nodes)}

    preds = [r[0] for r in conn.execute(
        "SELECT DISTINCT predicate FROM edges ORDER BY predicate"
    )]
    pid = {p: i for i, p in enumerate(preds)}
    sim_idx = pid.get(SIMILAR_PRED, -1)

    G = nx.Graph()
    for r in all_nodes:
        G.add_node(idx[r["path"]],
                   title=r["title"] or r["path"].split("/")[-1],
                   type=r["type"])

    for r in conn.execute("SELECT subject, predicate, object FROM edges"):
        si = idx.get(r["subject"])
        oi = idx.get(r["object"])
        if si is None or oi is None:
            continue
        is_sim = (pid[r["predicate"]] == sim_idx)
        if is_sim and not include_sim:
            continue
        G.add_edge(si, oi, pred=r["predicate"])

    if args.node:
        if not gl.node_exists(conn, args.node):
            print(f"节点不存在: {args.node}", file=sys.stderr)
            sys.exit(1)
        seed = idx.get(args.node)
        if seed is None:
            print(f"种子不在范围内: {args.node}", file=sys.stderr)
            sys.exit(1)
        ego = nx.ego_graph(G, seed, radius=args.radius)
        G = ego

    G.remove_nodes_from(list(nx.isolates(G)))
    return G


def compute_layout(G, seed=42):
    import networkx as nx
    import numpy as np
    N = G.number_of_nodes()
    deg = dict(G.degree())
    print(f"节点 {N} · 边 {G.number_of_edges()} · 布局计算中(spring layout)...")
    pos = nx.spring_layout(G, k=1.5 / max(1, np.sqrt(N)), iterations=100, seed=seed)
    return pos, deg


def render_png(G, pos, deg, out_path, args):
    import matplotlib
    matplotlib.use("Agg")
    import networkx as nx
    import matplotlib.pyplot as plt
    import numpy as np

    N = G.number_of_nodes()
    E = G.number_of_edges()

    node_sizes = [max(3, 3 + np.sqrt(deg.get(n, 0)) * 2) for n in G.nodes()]
    node_colors = [TYPE_COLOR.get(G.nodes[n].get("type", "entity"), "#888")
                   for n in G.nodes()]
    edge_colors = ["#8b949e"] * len(G.edges())

    fig, ax = plt.subplots(1, 1, figsize=(24, 16), dpi=args.dpi)
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#ffffff")

    print("渲染边...")
    nx.draw_networkx_edges(G, pos, ax=ax, edge_color=edge_colors,
                          width=0.2, alpha=0.15)
    print("渲染节点...")
    nx.draw_networkx_nodes(G, pos, ax=ax, node_size=node_sizes,
                           node_color=node_colors, alpha=0.8,
                           linewidths=0.3, edgecolors="#24292f")

    ax.axis("off")
    ax.set_title(f"WikiGraph · {N} nodes · {E} edges",
                color="#0969da", fontsize=14, pad=12)

    from matplotlib.lines import Line2D
    legend_handles = []
    for t in TYPE_ORDER:
        cnt = sum(1 for n in G.nodes() if G.nodes[n].get("type") == t)
        if cnt > 0:
            legend_handles.append(Line2D([0], [0], marker='o', color='w',
                                        markerfacecolor=TYPE_COLOR[t],
                                        markersize=8, label=f"{t} ({cnt})"))
    ax.legend(handles=legend_handles, loc="lower left",
              facecolor="#f6f8fa", edgecolor="#d0d7de",
              labelcolor="#24292f", fontsize=9)

    plt.tight_layout()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    print(f"✅ 已生成: {out} ({out.stat().st_size // 1024} KB)")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WikiGraph 可视化</title>
<style>
*{margin:0;box-sizing:border-box}
html,body{height:100%;background:#ffffff;overflow:hidden;
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;color:#24292f}
#cv{display:block;cursor:grab}
#cv:active{cursor:grabbing}
#tip{position:fixed;pointer-events:none;background:#ffffff;border:1px solid #d0d7de;
  border-radius:6px;padding:5px 9px;font-size:12px;line-height:1.5;display:none;z-index:10;
  max-width:340px;word-break:break-all;box-shadow:0 4px 12px rgba(0,0,0,.15)}
#bar{position:fixed;top:10px;left:10px;background:rgba(255,255,255,.92);border:1px solid #d0d7de;
  border-radius:8px;padding:8px 14px;font-size:13px}
#bar b{color:#0969da}
#leg{position:fixed;bottom:10px;left:10px;background:rgba(255,255,255,.92);border:1px solid #d0d7de;
  border-radius:8px;padding:8px 14px;font-size:12px}
#leg .row{display:flex;align-items:center;gap:6px;margin:2px 0}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block;flex-shrink:0}
#hint{position:fixed;bottom:10px;right:10px;font-size:11px;color:#57606a;
  background:rgba(255,255,255,.8);padding:4px 8px;border-radius:6px}
#zoom{position:fixed;top:10px;right:10px;display:flex;flex-direction:column;gap:4px}
#zoom button{width:34px;height:34px;border:1px solid #d0d7de;background:rgba(255,255,255,.92);
  color:#24292f;border-radius:6px;font-size:18px;cursor:pointer}
#zoom button:hover{background:#eaeef2}
</style>
</head>
<body>
<canvas id="cv"></canvas>
<div id="tip"></div>
<div id="bar"><b>WikiGraph</b> · <span id="cnt"></span></div>
<div id="leg"></div>
<div id="hint">滚轮缩放 · 拖拽平移 · 双击重置 · 悬停查看</div>
<div id="zoom"><button id="zin">+</button><button id="zout">-</button><button id="zres">o</button></div>
<script>
var DATA = /*__DATA__*/{};
(function(){
  var cv=document.getElementById('cv'),ctx=cv.getContext('2d');
  var tip=document.getElementById('tip');
  var nodes=DATA.nodes,edges=DATA.edges,colors=DATA.colors,titles=DATA.titles;
  var W,H,DPR;
  var view={x:0,y:0,s:1};
  var hover=-1;

  function resize(){
    DPR=window.devicePixelRatio||1;
    W=window.innerWidth;H=window.innerHeight;
    cv.width=W*DPR;cv.height=H*DPR;
    cv.style.width=W+'px';cv.style.height=H+'px';
    ctx.setTransform(DPR,0,0,DPR,0,0);
  }
  function fit(){
    var minx=1e9,maxx=-1e9,miny=1e9,maxy=-1e9;
    for(var i=0;i<nodes.length;i++){
      var n=nodes[i];
      if(n[0]<minx)minx=n[0];if(n[0]>maxx)maxx=n[0];
      if(n[1]<miny)miny=n[1];if(n[1]>maxy)maxy=n[1];
    }
    var dx=Math.max(1e-6,maxx-minx),dy=Math.max(1e-6,maxy-miny);
    var pad=50;
    var sx=(W-2*pad)/dx,sy=(H-2*pad)/dy;
    view.s=Math.min(sx,sy);
    view.x=(W-dx*view.s)/2-minx*view.s;
    view.y=(H-dy*view.s)/2-miny*view.s;
    draw();
  }
  function nodeR(d){return 1.2+Math.sqrt(Math.max(0,d))*0.7;}
  function draw(){
    ctx.fillStyle='#ffffff';
    ctx.fillRect(0,0,W,H);
    var s=view.s,ox=view.x,oy=view.y;
    ctx.strokeStyle='rgba(36,41,47,0.25)';
    ctx.lineWidth=0.7;
    ctx.beginPath();
    for(var i=0;i<edges.length;i++){
      var e=edges[i];
      var ns=nodes[e[0]],nt=nodes[e[1]];
      ctx.moveTo(ox+ns[0]*s,oy+ns[1]*s);
      ctx.lineTo(ox+nt[0]*s,oy+nt[1]*s);
    }
    ctx.stroke();
    var pad=30;
    for(var i=0;i<nodes.length;i++){
      var n=nodes[i];
      var sx=ox+n[0]*s,sy=oy+n[1]*s;
      if(sx<-pad||sx>W+pad||sy<-pad||sy>H+pad)continue;
      ctx.fillStyle=colors[n[2]];
      ctx.beginPath();
      ctx.arc(sx,sy,nodeR(n[3]),0,6.283);
      ctx.fill();
    }
    if(hover>=0){
      var n=nodes[hover];
      var sx=ox+n[0]*s,sy=oy+n[1]*s;
      ctx.strokeStyle='#0969da';
      ctx.lineWidth=2;
      ctx.beginPath();
      ctx.arc(sx,sy,nodeR(n[3])+3,0,6.283);
      ctx.stroke();
    }
  }
  function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
  var drag=false,lx,ly;
  cv.addEventListener('mousedown',function(e){drag=true;lx=e.clientX;ly=e.clientY;});
  window.addEventListener('mouseup',function(){drag=false;});
  window.addEventListener('mousemove',function(e){
    if(drag){
      view.x+=e.clientX-lx;view.y+=e.clientY-ly;
      lx=e.clientX;ly=e.clientY;draw();tip.style.display='none';hover=-1;return;
    }
    var r=cv.getBoundingClientRect();
    var mx=e.clientX-r.left,my=e.clientY-r.top;
    var best=-1,bd=1e9;
    var ss=view.s,ox=view.x,oy=view.y;
    for(var i=0;i<nodes.length;i++){
      var n=nodes[i];
      var dx=mx-(ox+n[0]*ss),dy=my-(oy+n[1]*ss);
      var d2=dx*dx+dy*dy;
      var rr=nodeR(n[3])+3;if(rr*rr<d2)continue;
      if(d2<bd){bd=d2;best=i;}
    }
    if(best!==hover){hover=best;draw();}
    if(best>=0){
      var n=nodes[best];
      tip.style.display='block';
      tip.style.left=Math.min(e.clientX+14,window.innerWidth-360)+'px';
      tip.style.top=(e.clientY+14)+'px';
      tip.innerHTML='<b style="color:'+colors[n[2]]+'">'+esc(titles[best])+'</b>'+
        '<br>'+DATA.types[n[2]]+' &middot; 度 '+n[3];
    }else{tip.style.display='none';}
  });
  cv.addEventListener('wheel',function(e){
    e.preventDefault();
    var r=cv.getBoundingClientRect();
    var mx=e.clientX-r.left,my=e.clientY-r.top;
    var f=e.deltaY<0?1.2:1/1.2;
    view.x=mx-(mx-view.x)*f;
    view.y=my-(my-view.y)*f;
    view.s*=f;draw();
  },{passive:false});
  cv.addEventListener('dblclick',fit);
  function zc(f){view.x=W/2-(W/2-view.x)*f;view.y=H/2-(H/2-view.y)*f;view.s*=f;draw();}
  document.getElementById('zin').onclick=function(){zc(1.3);};
  document.getElementById('zout').onclick=function(){zc(1/1.3);};
  document.getElementById('zres').onclick=fit;
  window.addEventListener('resize',function(){resize();draw();});
  var leg=document.getElementById('leg');var h='';
  for(var i=0;i<DATA.legend.length;i++){
    var l=DATA.legend[i];
    h+='<div class="row"><span class="dot" style="background:'+l.color+'"></span>'+
       l.type+' ('+l.count+')</div>';
  }
  leg.innerHTML=h;
  document.getElementById('cnt').textContent=DATA.meta.n+' 节点 · '+DATA.meta.e+' 边';
  resize();fit();
})();
</script>
</body>
</html>
"""


def render_html(G, pos, deg, out_path, args):
    nodes = list(G.nodes())
    idmap = {n: i for i, n in enumerate(nodes)}
    node_arr = []
    title_arr = []
    type_counts = {}
    for n in nodes:
        nd = G.nodes[n]
        t = nd.get("type", "entity")
        ti = TYPE_INDEX.get(t, len(TYPE_ORDER) - 1)
        title = (nd.get("title") or str(n))[:80]
        x, y = pos[n]
        node_arr.append([round(float(x), 5), round(float(y), 5), ti, deg.get(n, 0)])
        title_arr.append(title)
        type_counts[t] = type_counts.get(t, 0) + 1
    edge_arr = [[idmap[s], idmap[t]] for s, t in G.edges()]
    legend = [{"type": t, "color": TYPE_COLOR[t], "count": type_counts[t]}
              for t in TYPE_ORDER if t in type_counts]
    data = {
        "nodes": node_arr,
        "titles": title_arr,
        "edges": edge_arr,
        "colors": [TYPE_COLOR[t] for t in TYPE_ORDER],
        "types": TYPE_ORDER,
        "legend": legend,
        "meta": {"n": len(nodes), "e": len(edge_arr)},
    }
    json_str = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    json_str = json_str.replace("<", "\\u003c")
    html = HTML_TEMPLATE.replace("/*__DATA__*/{}", json_str, 1)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"✅ 已生成: {out} ({out.stat().st_size // 1024} KB)")


def main():
    ap = argparse.ArgumentParser(description="graph.db 可视化")
    ap.add_argument("-o", "--output", default=None, help="输出路径(默认按模式)")
    ap.add_argument("--html", action="store_true", help="输出交互 HTML(canvas)")
    ap.add_argument("--graph", default="main", choices=["main", "private"])
    ap.add_argument("--node", help="自我中心图种子")
    ap.add_argument("--radius", type=int, default=1, help="ego 半径")
    ap.add_argument("--sim", action="store_true", help="含相似边")
    ap.add_argument("--types", help="类型过滤(page,hub,people,raw,entity)")
    ap.add_argument("--dpi", type=int, default=150, help="PNG 分辨率(默认 150)")
    ap.add_argument("--open", action="store_true", help="生成后打开")
    args = ap.parse_args()

    if args.output is None:
        args.output = "temp/graph-visual.html" if args.html else "temp/graph-visual.png"

    db_path = gl.PRIVATE_GRAPH_DB if args.graph == "private" else gl.GRAPH_DB
    if not Path(db_path).exists():
        print(f"图数据库不存在: {db_path}", file=sys.stderr)
        sys.exit(1)
    conn = gl.connect(db_path)

    G = build_graph(conn, args)
    pos, deg = compute_layout(G)

    if args.html:
        render_html(G, pos, deg, args.output, args)
    else:
        render_png(G, pos, deg, args.output, args)

    conn.close()

    if args.open:
        import subprocess
        subprocess.run(["open", args.output], check=False)


if __name__ == "__main__":
    main()
