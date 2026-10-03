# PPT模板管理与复用

## 功能边界

Canonical ID：`artifact.slide_library`，成熟度 preview。模板库是资源存储；围绕它的检索、收藏、查看和实例化构成可单独调用的功能，可由 workspace 或 research 工作状态调用。当前主 Agent 负责语义选择与槽位设计，共享确定性脚本负责校验与文件操作。

先加载 `python3 .scripts/route.py --capability slide-library --capability-profile general`。统一入口为 `python3 .scripts/wg.py slide-library <子命令>`，返回 WG JSON envelope；底层 `slide_library.py` 命令保持兼容。

## 两类模板

- **直接使用型（direct）**：内容本身可复用。实例化保留原生 PPTX 字节与来源信息，使用前确认原内容适合当前报告。
- **套用填充型（adaptable）**：复用版式与对象结构，按 `reuse.json` 的文字、图片槽位替换内容。每个槽位声明用途、填写指引与容量，其他对象明确固定。

旧组件保留 unclassified 标记；分类与槽位设计完成后才能用统一 `use` 入口。文字总字符、显式行数和每行字符数是机械预算；字体替代和实际换行仍可能影响呈现。当前自动填充支持原生文本框、PNG/JPEG 图片；原生表格、图表、框图等可保留，复杂编辑依具体对象进行。

## 用户入口

```bash
python3 .scripts/wg.py slide-library search "学术 方法" --kind adaptable
python3 .scripts/wg.py slide-library show --template slide-library/templates/<模板目录>
python3 .scripts/wg.py slide-library use --template slide-library/templates/<直接使用型> --output projects/<工作区>/drafts/<新单页目录>
python3 .scripts/wg.py slide-library use --template slide-library/templates/<套用填充型> --values temp/<填写值>.json --output projects/<工作区>/drafts/<新单页目录>
```

`show` 验证组件文件/哈希，返回来源、原页码、源哈希、模板回执哈希、用途、限制、槽位及容量。`search` 用设计说明关键词排序，最多返回30条，可按类型筛选。

`use` 按类型自动选择原样复制或填充；`fill` 仍可直接调用。项目输出要求祖先目录含 `workspace.yaml`，目标位于其 `drafts/` 下的新单页子目录且组件确为一页；临时实验可写新 `temp/` 目录。成功输出 `component.pptx`、结构信息、预览与 `instance.json`，保存模板版本和输入来源。修改在该单页目录开展，整套组装由演示文稿交付流程承担。

## 提取与收藏

```bash
python3 .scripts/wg.py slide-library extract --source <已归档公共域Raw中的PPTX> --pages 1 --output temp/<候选目录> --name <名称>
python3 .scripts/wg.py slide-library prepare-reuse --component <已收藏组件目录> --profile temp/<槽位设计>.json --output temp/<分类候选目录> --name <名称>
python3 .scripts/wg.py slide-library collect --prepared temp/<候选目录> --name <名称> --instruction '<用户实际选页指令>'
```

提取保留所选页原生对象与依赖、字体字号、几何布局、覆盖估算、表格图表等结构，生成设计说明。收藏在用户授权的页范围内进行，源文件须已通过受管摄入归档至公共域 Raw；按来源、页码、类型、槽位版本去重，以完整组件目录原子发布。`reindex` 可重建派生目录。

视觉设计分析可另用现有 API 视觉入口，并将源绑定的 layout 回执传入 `--layout-report`。API 的文字反馈支持 Agent 判断；结构参数与图像估读分别标注。Raw 与既有模板保持完整，新实例具备独立来源回执。

## 完成与检查

完成条件是输入校验通过、独立单页产物和回执已生成；这不表示用户已验收视觉效果。用户负责最终检查。可提醒用户按需发起 **PPT形式检查**、**PPT引用脱敏**；有具体疑问时再做 **PPT证据核查**。这些辅助动作遵循 `operations/PRESENTATION.md` 的独立触发规则。
