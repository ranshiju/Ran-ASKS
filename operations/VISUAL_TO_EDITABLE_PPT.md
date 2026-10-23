# 图片/PDF → 可编辑 PowerPoint

## 定位

`.scripts/visual_to_editable_ppt.py` 将现有图片或 PDF 页面尽量忠实地重建为
PowerPoint 原生对象。它是写新产物的转换能力，不是事实提取器，也不改变源文件、
`raw/`、`wiki/` 或 `graph.db`。

转换采用确定性优先的混合策略。CLI 与 `run_visual_to_editable_ppt()` 均默认 Agent backend，
当前宿主负责语义重建和复核，不调用远程模型，也不进入 DSH。
API 调用方显式传 `backend="api"`，已有 DSH seam 已固定该参数，不改变 API 控制循环。

1. 矢量 PDF 直接读取文字、直线、矩形、椭圆、Bezier 路径、颜色、线宽和透明度；
2. 混合 PDF 保留原生矢量，同时把页面内位图登记为独立 fallback 对象；
3. 图片和扫描 PDF 用本地 Tesseract OCR、线段检测和色块/轮廓检测生成原生对象；
4. 无法可靠对象化的前景像素写成一个透明 residual 图层，并在报告中明确计数和覆盖率；
5. API backend 的非敏感页面在 API 已配置时可由视觉模型补充高置信度缺失对象；精确几何仍由本地算法约束。
6. Agent backend 的低覆盖率位图页在装配、回渲染前返回 `agent-task-v1`，由宿主重建后进入受管 check/commit。

## 调用

```bash
# 默认 Agent / balanced；高覆盖率直接转换，低覆盖率返回当前宿主任务
python3 .scripts/visual_to_editable_ppt.py figure.png --deterministic-only

# 同类中文信息图已经确认需要重建时，直接准备任务，不重复 OCR 和低质量装配
python3 .scripts/visual_to_editable_ppt.py figure.png --strategy reconstruct

# 批量输入，沿用每个源的 checkpoint；只在源和最终文件哈希一致时跳过已发布项
python3 .scripts/visual_to_editable_ppt.py figure-a.png figure-b.png \
  --output-dir projects/example/outputs --strategy reconstruct

# 显式输出与页面范围
python3 .scripts/visual_to_editable_ppt.py document.pdf \
  --output projects/example/figures/document-editable.pptx \
  --pages 1,3-5 --mode balanced --deterministic-only

# 普通非敏感材料允许已配置视觉模型辅助时，去掉 --deterministic-only
python3 .scripts/visual_to_editable_ppt.py diagram.png \
  --backend api --output projects/visual-reconstruction/outputs/diagram-editable.pptx

# raw/inbox/private/sources/source-local 或 paper profile 页面远程处理前必须显式授权
python3 .scripts/visual_to_editable_ppt.py manuscript.pdf \
  --backend api --profile paper --allow-remote --output projects/example/manuscript-editable.pptx
```

支持输入：PNG、JPEG、WebP、BMP、TIFF 和 PDF。输出必须是 `.pptx`。

## 模式

- `faithful`：高置信度阈值最高；宁可保留 residual，也不激进猜测对象。
- `balanced`：默认；兼顾视觉忠实和可编辑覆盖率。
- `editable`：更积极对象化并丢弃 residual；适合后续人工重排，不保证像素级忠实。

图片中的照片、复杂插画、无法可靠 OCR 的公式或纹理不会被伪装成原生形状；它们保留为
fallback。`fully_editable=true` 只在输出没有任何图片 fallback 时出现。
`editable` 模式丢弃的前景仍计入未重建部分，单列 `discarded_foreground_ratio` / `content_loss`，
不得通过删除 residual 将覆盖率变成 100% 或报告 `fully_editable=true`。

## 宿主重建与统一发布

低覆盖率启发式当前为位图前景估计覆盖率低于 0.65、OCR 不可用或存在前景丢弃。
它只负责选择处理路径，不是质量分数；照片、复杂背景较多时应由宿主判断是否保留位图。
原生矢量/混合 PDF 不因其内嵌照片走该启发式。`--strategy reconstruct` 用于已确认需要重建的
同类图片，可跳过重复分析。API backend 只附带质量提示，不伪造或派发宿主任务。

任务的三个声明输出均在本次 run 的 `reconstruction/` 内：

- `source-inventory.json`：先对照原图记录逐页精确文字块，再制作候选；绑定来源哈希和源页码。
- `candidate.pptx`：按 presentation skill 制作的原生对象候选，保留比例、文字、数字、机构名和流程关系。
- `review.json`：绑定源、候选及清单哈希，逐页声明来源核对与视觉复核；逐个登记位图原生名称及保留原因，历史候选的无名图片使用 `shape-id:<id>` 稳定定位。

完整字段样例和下一步命令由 `agent-task-v1.protocol` / `commands` 给出，不执行模型产出的代码、命令或路径。
宿主运行 task 的 `check` 获得逐页预览，修复后再次检查，视觉比较完成才填写对应 review 并 `commit`。
名称与分组应服务实际修改；同类页复用样式，中文换行按词组核查，不以原生形状数量代替可用性。

`check` 校验包完整性、页数、比例、对象边界、清单文字在原生文本及回渲染中的完整性，
并检查 PDF 字形 ID：文字可提取但缺字形（方框）也必须失败。PPTX 转 PDF 复用 visual_qa 的
受管字体环境，不另建裸 soffice 调用路径。
同一候选只转 PDF 一次，同时提取文字并生成预览。检查缓存绑定源、候选、清单、实现哈希与 DPI；
预览哈希也需一致。只修改复核声明不会触发重渲染；候选或清单改变即失效。
程序能验证“清单 → PPT”，不能独立证明“原图 → 清单”语义正确，也不能仅凭对象边界保证无重叠；
这两项必须由宿主对照原图核实。宿主声明不是机器验证出的事实。

`commit` 复用通过的检查并再次核对输入哈希、复核哈希及目标是否变化；无覆盖授权不覆盖旧文件。
最终 PPTX 与同名 `.reconstruction.json` 关联，sidecar 绑定最终输出哈希并作为精修后续做依据。
sidecar 同时保存清单和复核快照，清理 temp 后仍能辨认验收依据。
发布 sidecar 失败时回滚 PPTX。原自动分析回执仍描述其历史版本，不得当作精修后的当前回执。
无需为修正历史回执重做页面；已经完成的候选也可以通过该任务校验后登记发布。

## 断点续做

默认 checkpoint：

```text
temp/visual-to-ppt/<source-sha256>/<run-key>/
  manifest.json
  renders/page-0001.png
  pages/page-0001/
    objects.json
    receipt.json
    residual.png        # 仅需要时
  qa/
  final.json
  summary.json
```

`run-key` 绑定输入哈希、脚本哈希、页码、模式、DPI、OCR、模型、隐私参数和 schema。
`--resume` 为默认行为；`complete` 页面 checkpoint 在哈希一致时跳过。参数或脚本改变后自动
进入新 run，不覆盖旧 checkpoint。

输出已存在时默认失败。只有显式 `--overwrite` 才会在同目录先生成临时 PPTX，再原子替换
目标文件。输出路径禁止位于 `raw/`、`wiki/`、`inbox/`、`private/` 或 `cross-domain/`。

## 结果语义

最终 JSON/`summary.json` 的关键字段：

- `status=complete`：没有 fallback 图片；不等于内容或科学结论正确。
- `status=partial`：转换完成，但仍有 residual/嵌入位图对象。
- `fully_editable`：是否零 fallback。
- `editable_foreground_coverage_mean`：位图页中已对象化前景的估计比例。
- `assembly.object_counts`：原生文本、线段、矩形、椭圆、自由曲线和图片数。
- `visual_comparison`：输出回渲染与输入页面的逐页 RGB 差和相似度。
- `quality`：覆盖率分流提示；不代表内容、排版或可用性通过。
- `content_loss`：是否因 editable 模式丢弃未重建的前景；此时即使没有图片也返回 partial。
- `input_unchanged`：转换前后源文件哈希是否一致。

模型/OCR/API 失败不得伪装为完全可编辑；fallback 和低覆盖率需要人工复核。
RGB 相似度会受 residual 保留原图影响，高相似度不能替代内容清单与原生对象验收。
自动转换的比较图复用本地 visual_qa 已有渲染，不重复转换同一 PPTX。

## 视觉模型与隐私

配置优先级：

```text
VISUAL_RECONSTRUCTION_* → VISUAL_QA_* → LLM_API_*
```

默认模型在 `operations/config/llm-models.yaml` 登记：主模型 `GLM-5.3-Flash`，回退
`GLM-4.5V`。2026-09-11 按用户明确指令切换主模型；未进行重建质量 A/B 对测，不代表已验证优于旧模型。API key、Authorization header 和图片 data URL 不进入 checkpoint 或 DSH log。

模型返回只接受有限 JSON 对象类型、0–1 坐标和 0.90 以上置信度；路径、命令、XML、代码和
越界对象全部丢弃。模型不能决定输出路径，也不能直接写 PPT XML。

## DSH 注册与自动调用

工具名：`visual_to_editable_ppt`。

它只注册在 `VisualReconstructionAgentLoop`，不进入 `query_actions`、只读
`VisualAgentLoop`、摄入工具或事实查询 CitationGuard。`dsh.dispatch_loop()` 仅在明确出现以下
意图时路由：

- 图片/图像转 PPT；
- PDF 转 PPT；
- 转换为/转成可编辑 PPT；
- 复刻成/复刻为 PPT；
- 对象化为 PPT。

普通“视觉检查”“PDF 页面排版”“论文摄入”“查询 PDF 作者”不会触发转换。

当前宿主 Agent 确认源路径、输出路径、覆盖权限和远程上传权限后直接调用底层能力，不进入 DSH；API backend 仅在路径齐备时通过 `VisualReconstructionAgentLoop` 执行。

## 验证

```bash
python3 .scripts/test_visual_to_editable_ppt.py
python3 dsh/test_visual_reconstruction_tools.py
python3 .scripts/test_visual_qa.py
python3 dsh/test_visual_tools.py
python3 .scripts/engineering_graph.py validate
```
