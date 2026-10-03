# 知识图可视化

## 功能边界

Canonical ID：`knowledge.visualize`，成熟度 preview。输入主题关键词、节点或关系筛选，输出本地交互 HTML / 静态 PNG 与 JSON 导航清单，完成后即可结束。它是可组合功能，沿用调用方的工作状态。

先加载 `python3 .scripts/route.py --capability graph-visualize --capability-profile general`。统一入口 `python3 .scripts/wg.py graph-visualize` 返回 WG JSON envelope；确定性底层入口 `graph_visualize.py` 同样可用。代码通过 `graph_lib.connect(read_only=True)` 读取已有图的一致性快照，生成派生产物。

## 选择与输出

```bash
python3 .scripts/wg.py graph-visualize --query "矩阵乘积态" --radius 1 --html
python3 .scripts/wg.py graph-visualize --node academic/wiki/<节点路径> --radius 2 --predicates 包含,相关 --max-nodes 60 --max-edges 120 --html --output projects/<项目>/outputs/<新文件>.html
python3 .scripts/wg.py graph-visualize --types hub --max-nodes 40 --output temp/<新文件>.png
python3 .scripts/wg.py graph-visualize --graph private --query "关键词" --html
```

`--node` 是图中精确节点 path；`--query` 在标题/path 上做字面关键词匹配，空格分词取交集，最多选10个匹配种子。可先用 `wg lookup` 定位。`--types` / `--predicates` 用逗号分隔精确类型。默认排除“相似”导航边，可加 `--sim` 纳入。

先按节点类型和关系过滤，再按无向邻接发现种子的邻域；呈现时保留数据库中的每一条有向关系及其证据地址。无主题/节点参数时按连接度选取概览。半径默认1、范围0–4；节点默认80、上限300；边默认200、上限1200。达到上限时，界面与 JSON 明示种子、候选节点和所示节点间关系的省略数，避免把局部视图误解为全库。

公共图产物写入新 `temp/` 文件或项目 `outputs/` 文件；private 物理隔离读取 `private/graph.db`，产物只写 `private/temp/`。自动命名带时间戳，显式输出同样要求新文件。HTML/PNG 旁生成同名 `.json`，包含筛选参数、节点/边、来源 locator 与产物哈希。

## 阅读与溯源

HTML 在本地浏览器打开，支持缩放、平移、关系筛选和节点选择。箭头表方向，节点侧栏列入边/出边及来源。Wiki/Raw 文件存在且位于对应知识域时，提供相对文件链接；缺失或越界 locator 只保留文本。来源可包含 `#anchor`，实际定位能力取决于本地查看器。PNG 展示方向、关系标签和类型图例，完整来源记录在旁侧 JSON。

图展示的是发现与到达路径；邻接不构成因果、支持或学术结论的自动证明。事实判断仍经 Wiki 桥接并回溯 Raw。产物是可重建的导航视图。基础流程由当前 Agent 和本地脚本执行；图内容不会因查看或导出而改变。

## 完成条件

输出图文件与 JSON 清单成功生成，读取范围、截断和来源状态明确。依赖缺失、数据库缺失、筛选无匹配、输出越界或同名文件已存在时返回错误，保留已有文件。布局采用固定随机种子的力导向算法；密集图建议缩小半径或筛选关系后重新生成。
