# 图片/PDF → 可编辑 PowerPoint

## 定位

`.scripts/visual_to_editable_ppt.py` 将现有图片或 PDF 页面尽量忠实地重建为
PowerPoint 原生对象。它是写新产物的转换能力，不是事实提取器，也不改变源文件、
`raw/`、`wiki/` 或 `graph.db`。

转换采用确定性优先的混合策略：

1. 矢量 PDF 直接读取文字、直线、矩形、椭圆、Bezier 路径、颜色、线宽和透明度；
2. 混合 PDF 保留原生矢量，同时把页面内位图登记为独立 fallback 对象；
3. 图片和扫描 PDF 用本地 Tesseract OCR、线段检测和色块/轮廓检测生成原生对象；
4. 无法可靠对象化的前景像素写成一个透明 residual 图层，并在报告中明确计数和覆盖率；
5. 非敏感页面在 API 已配置时可由视觉模型补充高置信度缺失对象；精确几何仍由本地算法约束。

## 调用

```bash
# 默认 balanced；输出到 projects/visual-reconstruction/outputs/
python3 .scripts/visual_to_editable_ppt.py figure.png --deterministic-only

# 显式输出与页面范围
python3 .scripts/visual_to_editable_ppt.py document.pdf \
  --output projects/example/figures/document-editable.pptx \
  --pages 1,3-5 --mode balanced --deterministic-only

# 普通非敏感材料允许已配置视觉模型辅助时，去掉 --deterministic-only
python3 .scripts/visual_to_editable_ppt.py diagram.png \
  --output projects/visual-reconstruction/outputs/diagram-editable.pptx

# raw/inbox/private/sources/source-local 或 paper profile 页面远程处理前必须显式授权
python3 .scripts/visual_to_editable_ppt.py manuscript.pdf \
  --profile paper --allow-remote --output projects/example/manuscript-editable.pptx
```

支持输入：PNG、JPEG、WebP、BMP、TIFF 和 PDF。输出必须是 `.pptx`。

## 模式

- `faithful`：高置信度阈值最高；宁可保留 residual，也不激进猜测对象。
- `balanced`：默认；兼顾视觉忠实和可编辑覆盖率。
- `editable`：更积极对象化并丢弃 residual；适合后续人工重排，不保证像素级忠实。

图片中的照片、复杂插画、无法可靠 OCR 的公式或纹理不会被伪装成原生形状；它们保留为
fallback。`fully_editable=true` 只在输出没有任何图片 fallback 时出现。

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
- `input_unchanged`：转换前后源文件哈希是否一致。

模型/OCR/API 失败不得伪装为完全可编辑；fallback 和低覆盖率需要人工复核。

## 视觉模型与隐私

配置优先级：

```text
VISUAL_RECONSTRUCTION_* → VISUAL_QA_* → LLM_API_*
```

默认模型来自 `operations/config/llm-models.yaml`：主模型 `GLM-4.6V`，回退
`GLM-4.5V`。API key、Authorization header 和图片 data URL 不进入 checkpoint 或 DSH log。

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

Agent 模式先返回 tool schema handoff，要求确认源路径、输出路径、覆盖权限和远程上传权限；
API/direct 模式在路径齐备时才执行。

## 验证

```bash
python3 .scripts/test_visual_to_editable_ppt.py
python3 dsh/test_visual_reconstruction_tools.py
python3 .scripts/test_visual_qa.py
python3 dsh/test_visual_tools.py
python3 .scripts/engineering_graph.py validate
```
