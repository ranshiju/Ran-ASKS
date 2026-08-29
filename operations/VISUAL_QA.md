# 视觉产物 QA

视觉 QA 用于发现图片、PDF 页面和 PPT/PPTX 静态页面中的可见交付缺陷。它由本地确定性检查与可选视觉模型检查组成，不修改输入产物，也不判断科学数据或结论是否真实。

## 支持范围

- 图片：PNG、JPEG、WebP、TIFF；
- PDF：PyMuPDF 按页渲染；
- PPT/PPTX：LibreOffice `soffice` 先转 PDF，再按页渲染；
- 不覆盖动画、嵌入视频、演讲者备注和切换效果。

默认视觉模型为 `GLM-4.6V`，主模型失败时回退 `GLM-4.5V`。二者登记在 `operations/config/llm-models.yaml`。模型调用使用 OpenAI-compatible `chat/completions` 接口：

```bash
VISUAL_QA_API_BASE=...
VISUAL_QA_API_KEY=...
VISUAL_QA_MODEL=GLM-4.6V
VISUAL_QA_FALLBACK_MODEL=GLM-4.5V
```

脚本会自动读取项目根目录 `.env`，并展开 `${LLM_API_BASE}`、`${LLM_API_KEY}` 形式的同文件引用；进程环境中的同名变量优先。无需在每次 CLI 调用前手工 `source .env`。

## 使用

本地确定性检查：

```bash
python3 .scripts/visual_qa.py path/to/figure.png --deterministic-only
python3 .scripts/visual_qa.py path/to/paper.pdf --pages 1,3-5 --profile paper --deterministic-only
python3 .scripts/visual_qa.py path/to/slides.pptx --profile slides --deterministic-only
```

启用视觉模型时去掉 `--deterministic-only`。可以用 `--context` 提供作图数据或设计说明文本。缺少 API 配置、请求失败或返回非法 JSON 时，结果必须为 `partial/not_checked`，不会把仅完成本地检查的页面伪装为视觉模型 `pass`。

`raw/`、`inbox/`、`private/`、`sources/` 和 `source-local/` 路径，以及 `profile=paper` 的论文/稿件 PDF，默认禁止远程上传，只执行本地检查并返回部分完成。确认材料允许发送给远程服务后，调用方才可显式传入 `--allow-remote`。API key 不进入 receipt。图件 PDF 可显式使用 `--profile figure`，但路径保护仍优先。

## Receipt 与断点续做

默认输出位于：

```text
temp/visual-qa/<artifact-sha>/<profile>/<check-key>/
├── manifest.json
├── renders/page-0001.png
├── pages/page-0001.json
└── summary.json
```

`check-key` 绑定模型、回退模型、prompt/schema 版本、渲染配置、context 哈希、远程权限和检查模式。再次运行时，只有输入与这些参数全部一致且逐页 receipt 为 `complete` 的页面才会跳过；失败或部分完成的页面会重试。修改输入文件会进入新的 artifact hash 目录，不覆盖旧记录。用 `--no-resume` 可强制重新检查，但仍不修改原文件。

可以用 `--receipt-root` 指定仓库外缓存目录；若指定仓库内路径，则必须位于 `temp/` 下。程序会在建目录前拒绝把 receipt 写入 `raw/`、`wiki/`、`private/` 或其他源码/知识库目录。

## DSH 自动调用

DSH 注册独立工具 `visual_check`。Agent 在以下两类情形调用：

1. 用户显式要求视觉检查、视觉质检或页面检查，例如“视觉检查这张图”“检查 PDF 页面排版”“幻灯片质检”；
2. 用户要求修改图片、PDF 页面或 PPT/PPTX 页面，而修改依赖布局、位置、颜色、字号、间距、遮挡、裁切、比例、图例或页面流等可见状态。此时 Agent 需要先检查实际产物，才能可靠理解“往下移动一点”“让正文自然移上去”“与右侧对象对齐”等指令。

第二类调用的目的是建立修改前的视觉上下文，不等同于每次修改后的全量交付 QA。常规文字改写、LaTeX/代码编译、元数据修改和每轮交付不自动触发；需要最终视觉验收时由用户明确要求。普通“摄入论文 PDF”仍进入摄入 loop，“查询论文 PDF 作者”仍进入查询 loop。

`VisualAgentLoop` 不注册到 `query_actions`，也不挂接事实查询的 CitationGuard。Agent 模式返回包含候选路径和完整 schema 的 handoff，由外层 Agent 确认路径与远程权限后调用；API/direct 模式在获得路径后直接执行 `visual_check`。显式视觉验收应检查 `summary.json`，而不是依靠自由文本评价。

## 结果语义

- `pass`：请求的检查已完成，未发现问题；
- `warn`：存在应人工复核或改进的可见问题；
- `fail`：存在明显空白、严重低分辨率、视觉模型确认的严重缺陷等；
- `not_checked`：视觉模型检查未完成且本地检查不足以给出完整通过结论；
- `status=partial`：至少一页被隐私策略阻断、API 未配置或模型调用失败。

所有模型问题都是视觉建议，不是 Raw 事实证据；工具不会自动修改数据、图件、论文、Wiki 或 graph.db。
