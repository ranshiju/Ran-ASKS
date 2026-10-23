# 视觉产物 QA

视觉 QA 用于发现图片、PDF 页面和 PPT/PPTX 静态页面中的可见交付缺陷。它由本地确定性检查与可选视觉模型检查组成，不修改输入产物，也不判断科学数据或结论是否真实。

## 支持范围

- 图片：PNG、JPEG、WebP、TIFF；
- PDF：PyMuPDF 按页渲染；
- PPT/PPTX：LibreOffice `soffice` 先转 PDF，再按页渲染；
- 不覆盖动画、嵌入视频、演讲者备注和切换效果。

默认视觉模型为 `GLM-5.3-FlashX`，主模型接口或输出失败时兼容回退 `GLM-4.6V`。回退模型不是真值裁判，不会因为主模型报告缺陷而再次请求回退模型以覆盖结论。二者登记在 `operations/config/llm-models.yaml`。模型调用使用 OpenAI-compatible `chat/completions` 接口：

```bash
VISUAL_QA_API_BASE=...
VISUAL_QA_API_KEY=...
VISUAL_QA_MODEL=GLM-5.3-FlashX
VISUAL_QA_FALLBACK_MODEL=GLM-4.6V
VISUAL_QA_REASONING_EFFORT=low
VISUAL_QA_FALLBACK_REASONING_EFFORT=default
VISUAL_QA_MAX_TOKENS=1800
```

脚本会自动读取项目根目录 `.env`，并展开 `${LLM_API_BASE}`、`${LLM_API_KEY}` 形式的同文件引用；进程环境中的同名变量优先。无需在每次 CLI 调用前手工 `source .env`。

主/回退推理档位分别设置，允许 `low/high/default`；`default` 表示不发送该字段。常规 QA 用 low，复杂页面可显式 `--reasoning-effort high`；`--max-tokens` 可单次覆盖输出预算。CLI/Python 显式参数优先于环境配置。这些设置与 OCR、文本模型独立，不因升级模型而改变远程授权。

2026-09-26 用户明确指定检查主模型为 `GLM-5.3-FlashX`，覆盖通用视觉 QA、PPT 检查及独立 OCR 复核；不把历史探测结果冒充 FlashX 的质量证明。

历史记录：2026-09-07 在三张固定合成页面上按原 QA 协议对照后，经用户确认启用当时的 `GLM-5.3-Flash` 默认；这不是整体准确率排名。可编辑 PPT 重建另由 `.env` 的 `VISUAL_RECONSTRUCTION_MODEL=GLM-5.3-Flash` 与 `VISUAL_RECONSTRUCTION_FALLBACK_MODEL=GLM-4.5V` 独立配置；重建主模型于 2026-09-11 按用户明确指令切换，并未进行重建质量对测。

## 使用

本地确定性检查：

```bash
python3 .scripts/visual_qa.py path/to/figure.png --deterministic-only
python3 .scripts/visual_qa.py path/to/paper.pdf --pages 1,3-5 --profile paper --deterministic-only
python3 .scripts/visual_qa.py path/to/slides.pptx --profile slides --deterministic-only
```

启用视觉模型时去掉 `--deterministic-only`。可以用 `--context` 提供作图数据或设计说明文本。缺少 API 配置、请求失败或返回非法 JSON 时，结果必须为 `partial/not_checked`，不会把仅完成本地检查的页面伪装为视觉模型 `pass`。

`raw/`、`inbox/`、`private/`、`sources/` 和 `source-local/` 路径，以及 `profile=paper` 的论文/稿件 PDF，默认禁止远程上传，只执行本地检查并返回部分完成。确认材料允许发送给远程服务后，调用方才可显式传入 `--allow-remote`。API key 不进入 receipt。图件 PDF 可显式使用 `--profile figure`，但路径保护仍优先。

## 共享本地材料准备

`artifact.visual_qa` 同时提供不依赖模型的本地材料接口；这不是 QA 判决，也不是另一个看图 Agent。当前宿主使用原生图片附件或查看工具阅读输出，API/DSH QA 控制循环不参与。

```bash
python3 .scripts/visual_qa.py <PNG或单页PDF> --prepare-views <regions.json>
```

区域由当前 Agent 根据实际需要明确选择，不由程序语义评分或自动分块。`regions.json` 示例：

```json
{"schema":"image-view-spec-v1","regions":[[0.05,0.1,0.45,0.25],[0.5,0.65,0.45,0.25]]}
```

- 坐标为整图归一化 `[x,y,width,height]`，1–6 个互不重复的有效区域；允许区域相交以保留上下文。
- 返回 `visual-material-v1`：`status=prepared`、匿名整图/局部 ID、来源及输出哈希、实际整图区域、像素范围与附件路径，`remote_calls=0`，没有质量 verdict。默认写 `temp/visual-qa/materials/<key>/`；`--receipt-root` 仅允许仓库 temp 或已存在项目的 outputs，拒绝 Raw/private、越界和符号链接。重新准备会从来源重建派生文件，不盲信旧缓存。
- PNG 局部只裁取源像素，必要时缩小，不上采样或恢复不存在的信息。单页 PDF 局部直接从对应区域渲染；局部最长边至多1600像素，渲染密度至多180 dpi，不先把整页做成高分辨率大图。来源至多32 MiB，PNG 至多16 MiB/2400万像素，局部总字节至多32 MiB。
- 共享 Python 接口：`convert_slides_to_pdf(path,target_pdf,soffice_bin=None)`、`prepare_image_views(png_bytes,spec,detail_pdf=None)`、`prepare_local_views(path,spec,receipt_root=None)`。旧私有转换名称仅保留兼容转发；新调用方用公开名称。带 PDF 的 image-views API 要求单页、无密码且比例与整图一致；跨文件同源性由上层成品绑定校验，不由页面比例推断。
- 本材料导出入口不支持仓库 private 或 .git 来源；不以复制改变保密范围。局部 pixel_bbox 为原整图的半开像素边界 `[left,top,right,bottom]`，region 是该实际边界对应的归一化范围，可能因取整略大于请求。输出材料不是“已看清”的证明：宿主可能缩放；实际显示尺寸未知就记录未知。显式视觉 QA 仍调用原 QA 路径并读取其 summary，不能用材料准备成功或 Agent 的自由评价代替。

## 模型选择与候选验证

`operations/config/llm-models.yaml` 将视觉模型分为三类：已进入本地契约的默认/回退模型、OCR 或文档版面专项候选，以及尚未确认图像输入能力的通用模型。这些字段只用于人工选择和配置审计，不是运行时白名单。

- 不得根据模型名称或版本号推定它支持图像输入。
- OCR/VL 专项模型可用于文字与版面辅助检查，但未经通用页面质检对照前不取代默认视觉 QA 模型。
- 通用模型晋级为候选前，先用无敏感信息的合成图像验证图像输入、严格 JSON 输出、超时和错误语义。
- 候选模型只有在固定页面集上与当前默认模型完成盲测，并验证缺陷召回、误报、结构化输出稳定性和成本/延迟后，才能提升为默认或回退模型。

## Receipt 与断点续做

默认输出位于：

```text
temp/visual-qa/<artifact-sha>/<profile>/<check-key>/
├── manifest.json
├── renders/page-0001.png
├── pages/page-0001.json
└── summary.json
```

`check-key` 绑定模型、回退模型、两者推理档位、输出 token 预算、prompt/schema 版本、渲染配置、context 哈希、远程权限和检查模式。再次运行时，只有输入与这些参数全部一致且逐页 receipt 为 `complete` 的页面才会跳过；失败或部分完成的页面会重试。修改输入文件会进入新的 artifact hash 目录，不覆盖旧记录。用 `--no-resume` 可强制重新检查，但仍不修改原文件。

可以用 `--receipt-root` 指定仓库外缓存目录；若指定仓库内路径，则必须位于 `temp/` 下。程序会在建目录前拒绝把 receipt 写入 `raw/`、`wiki/`、`private/` 或其他源码/知识库目录。

## 与模拟读者评审的区别

布局、清晰度检查不能证明读者理解研究内容。科普展板需要检验理解时，使用 `operations/SCIENCE_EXHIBIT.md` 的可选 API 模拟读者评审：复用本模块的 `call_json_vision` 传输，但采用独立的盲读角色、先复述后建议的结构，不沿用视觉 QA 的分数或通过结论。显式远程授权后只发送选定的实际 PNG；不发送论文、预期答案或项目上下文。模拟反馈不是科学认证、真人调查或用户验收。

## 调用边界

API backend 通过 DSH 注册的独立工具 `visual_check` 执行；当前宿主 Agent 按同一判据直接调用底层能力。以下两类情形需要视觉检查：

1. 用户显式要求视觉检查、视觉质检或页面检查，例如“视觉检查这张图”“检查 PDF 页面排版”“幻灯片质检”；
2. 用户要求修改图片、PDF 页面或 PPT/PPTX 页面，而修改依赖布局、位置、颜色、字号、间距、遮挡、裁切、比例、图例或页面流等可见状态。此时 Agent 需要先检查实际产物，才能可靠理解“往下移动一点”“让正文自然移上去”“与右侧对象对齐”等指令。

第二类调用的目的是建立修改前的视觉上下文，不等同于每次修改后的全量交付 QA。常规文字改写、LaTeX/代码编译、元数据修改和每轮交付不自动触发；需要最终视觉验收时由用户明确要求。普通“摄入论文 PDF”仍进入摄入 loop，“查询论文 PDF 作者”仍进入查询 loop。

`VisualAgentLoop` 只属于 API backend，不注册到 `query_actions`，也不挂接事实查询的 CitationGuard。当前宿主 Agent 自行确认目标路径与远程权限后直接调用 `visual_check`，不经过 DSH handoff；API loop 在获得路径后执行。显式视觉验收应检查 `summary.json`，而不是依靠自由文本评价。

## 结果语义

prompt-v2 仅将有可见证据且影响阅读、解释或可访问性的缺陷列入 issues。纯审美偏好与可选美化建议放 summary；没有实质缺陷则 issues 为空、verdict=pass。程序不按问题名称过滤或压制模型报告；模型报告仍须人工判断。非正常结束、截断或拒答响应走错误/回退路径，不能给出完整通过结论。

- `pass`：请求的检查已完成，未发现问题；
- `warn`：存在应人工复核或改进的可见问题；
- `fail`：存在明显空白、严重低分辨率、视觉模型确认的严重缺陷等；
- `not_checked`：视觉模型检查未完成且本地检查不足以给出完整通过结论；
- `status=partial`：至少一页被隐私策略阻断、API 未配置或模型调用失败。

所有模型问题都是视觉建议，不是 Raw 事实证据；工具不会自动修改数据、图件、论文、Wiki 或 graph.db。

### 本地 PPT 字体与重做核验

打包 macOS LibreOffice 渲染时，shared 入口显式加载随附的 fontconfig 配置以发现系统中文字体；调用方已有 `FONTCONFIG_FILE` / `FONTCONFIG_PATH` 时保留其选择，不安装或改写宿主字体。字体替代仍可能改变换行，须逐页核对。旧渲染缓存不覆盖；摄入核验发现坏渲染时，新建同源事务生成证据，成功后用 `inbox_state.py --supersede <旧事务> --by <完成事务>` 关闭未提交旧事务，保留审计。
