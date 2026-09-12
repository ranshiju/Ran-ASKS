# 独立图片 OCR

## 边界

`.scripts/image_ocr.py` 是可独立使用的 OCR 库和 CLI，不依赖 inbox 分类、Wiki 生成或图数据库。
shared 内核负责图片解码、尺寸/帧数检查、EXIF 方向归一、源 SHA-256、文字与回执校验；
`recognize_image()` 与 `review_image()` 是显式授权的 API-only adapter，分别执行转写和视觉复核，不使用 DSH 或语义生成控制循环。
摄入默认使用独立视觉 API；宿主只消费文字结果，不打开原图。无授权、API 失败或关键项未决时返回纯文字行动任务，不自动改用宿主看图。只有显式 `IMAGE_OCR_BACKEND=agent` 才使用宿主转写和复核。

支持单帧 PNG/JPEG/WebP/BMP/TIFF（大小写扩展名均可），最大 20 MiB、4000 万像素；
图片解码需要当前 Python 环境已有 Pillow。
多帧输入明确拒绝，避免丢失后续页。无字插图不伪造转写，返回文字说明并停止，不自动交接宿主看图。
现有 PDF `extractor.py` 级联保持不变；其他工具可直接 import 本模块复用，不应复制一套 HTTP/OCR 代码。

## 配置与模型

读取仓库 `.env`，进程环境优先，支持 `${NAME}` 引用：

```dotenv
IMAGE_OCR_BACKEND=api
IMAGE_OCR_ALLOW_REMOTE=false
IMAGE_OCR_API_BASE=${LLM_API_BASE}
IMAGE_OCR_API_KEY=${LLM_API_KEY}
IMAGE_OCR_MODEL=GLM-5.3-Flash
IMAGE_OCR_FALLBACK_MODEL=GLM-4.6V
IMAGE_OCR_REASONING_EFFORT=low
IMAGE_OCR_FALLBACK_REASONING_EFFORT=default
IMAGE_OCR_MAX_TOKENS=8192
```

这些配置均可省略：base/key 复用主 API，模型缺省 GLM-5.3-Flash；不复制密钥，也不改动主语义模型。
`IMAGE_OCR_ALLOW_REMOTE=false` 保留逐次 `--allow-remote-ocr` 授权；显式设为 `true` 是本项目图片摄入的持续上传许可，覆盖转写和复核两步，不扩展至 PDF/PPT 或其他视觉工具。仅配置 endpoint/key 或 `INGEST_BACKEND=api` 不构成许可。独立 OCR CLI 仍须 `--allow-remote`，不读取摄入编排器的持续许可。
可用模型目录的单一位置是 `operations/config/llm-models.yaml`，并非 `.env` 中的主模型名。
2026-09-07 同 endpoint 合成文字图实测：GLM-4.6V 正常转写；PaddleOCR-VL-1.5 HTTP 500；
DeepSeek-OCR 在 65 秒测试预算内超时。这只是本次 provider 探测，不代表模型本身不具备 OCR 能力。
随后对同一 inbox 表格实测 GLM-5.3-Flash：low 档正确转写，保留空字段与签名待核对标记；经用户确认设为主模型，GLM-4.6V 作为兼容回退。不把仅在目录出现的 OCR 模型自动提升为默认。
可显式指定其他模型复测；`--model` 固定一次模型，不启动 fallback。
未显式指定模型时，仅按配置的主模型/备用模型各尝试一次，失败原因保留安全摘要，不做自动模型搜索。

OCR 推理档位和输出预算与视觉 QA/文本模型独立。允许 `low/high/default`，其中 `default` 省略 reasoning_effort 字段；备用模型默认使用该档位。`--reasoning-effort`、`--max-tokens` 或同名 Python 参数可覆盖主模型本次请求；测试其他模型时可显式用 default 避免发送其未支持的推理参数。新 API 回执记录实际请求参数，旧版回执仍可经原图与文本哈希校验复用，不会为了切换模型而重做已完成 OCR。

## 独立使用

```bash
python3 .scripts/image_ocr.py /absolute/path/image.jpg --allow-remote \
  --output temp/image-ocr/example.json
python3 .scripts/image_ocr.py /absolute/path/image.jpg \
  --check temp/image-ocr/example.json
```

首次调用会上传图片，只有 `--allow-remote` 才放行；`--check` 仅读本地数据。
默认回执路径是 `temp/image-ocr/<原图sha256>/ocr.json`；已有输出须显式 `--force` 才能重提。
仓库内只允许输出到 `temp/`；外部输出目录允许复用，但拒绝 raw/wiki 及指向它们的符号链接。
不修改原图，回执不记录 endpoint、key、Authorization、图像 data URL 或 provider 错误正文。

Python 调用：将 `.scripts` 加入模块搜索路径后，使用
`recognize_image(Path(...), allow_remote=True)`、`save_receipt(...)`、`load_receipt(...)`。
返回 `image-ocr-v1` JSON，包含原图 SHA-256、尺寸、实际 backend/model、规范版本、Markdown、
文本哈希、调用摘要及初始 `review_required=true`。复核附加后由程序计算 review_status/review_required，不生成虚假的置信度。
空响应、非法结构、输出截断或非正常 finish_reason 均失败，不进入摄入。

## 摄入集成

图片进入 Wiki/语义生成前须完成风险评估。默认由视觉 API 转写，再独立调用视觉 API 对照原图复核，记录 `reviewer_kind=api`；不能称为人工确认。
无授权返回 `image_ocr_authorization`；API 失败返回 `image_api_retry`；关键项未决返回 `image_confirmation`。这些任务只提供文字摘要，不把图片作为宿主输入，也不要求宿主自行复核。宿主协调授权或用户确认后恢复同一事务。
成功转写先落暂存回执，复核失败不丢失转写。普通 resume 复用已验证产物，不重复付费复核；失败重试须显式 `--allow-remote-ocr`，关键未决项须提供可信的源绑定复核回执。失败不会隐式切换为宿主识图；单次调用内仍遵守配置的有界主/回退模型预算。

```bash
# 公开入口：inbox 文件直接摄入；外部附件先受管暂存进入 inbox。
python3 .scripts/wg.py ingest inbox/image.jpg --subproject admin
python3 .scripts/wg.py ingest /absolute/path/image.jpg --subproject admin

# OCR 结果复用：已有合格复核则不再上传；缺复核仍需授权视觉 API。
python3 .scripts/wg.py ingest inbox/image.jpg --subproject admin \
  --ocr-result temp/image-ocr/example.json

# 显式授权这次图片转写和复核调用 API，不改变 Wiki/语义 backend。
python3 .scripts/wg.py ingest inbox/image.jpg --subproject admin --allow-remote-ocr

# 处理行动任务后恢复原事务；授权/明确重试附加 --allow-remote-ocr。
python3 .scripts/wg.py ingest --resume <transaction-id>
```

底层统一入口 `ingest_inbox.py --run --file ...` 同样支持 `--ocr-result` 与 `--allow-remote-ocr`，
两者只允许单图片，不对整个 inbox 批量授予上传许可；Agent/API dispatch 原样透传。项目配置的持续授权则适用于后续图片摄入，请按隐私边界显式设置。

显式选择宿主模式时，Agent OCR 任务只有原图、暂存输出路径、转写协议、源哈希与 resume 命令。
源变化时拒绝消费旧产物；无效/缺失转写仍为 prepared，不误判为 completed。
API 摄入也必须获得图片上传许可；默认无授权只交接文字授权请求，不返回宿主 OCR task。视觉路由 `image_backend`、实际转写 `ocr_backend` 与 semantic backend 分别记入事务；
但 resume 时必须保持事务创建时的 semantic backend，禁止为完成 OCR 切换整个 `INGEST_BACKEND`。

图片扩展名只选择提取方式，不决定所属域或会议来源。图片按普通文档候选路由；
计划、locator 与指纹索引共享同一图片格式集合，避免新增格式误接受二进制行号或把 companion 当第二份来源。
academic 仍须显式提供非论文 document_type，不把截图当作 academic 论文 PDF。
摄入保留原图，并在同一个受管事务中归档同名 Markdown locator companion；Wiki 引用 companion 的行，
原图是最终复核依据。临时 OCR JSON/任务文件不是 Raw，不进入事实图，不替代来源。
finalize 前再次核对原图和暂存实体 SHA-256、companion 全文；缺失或变化时拒绝提交，
不会把过期转写与新原图配成一个 Raw 包。
程序哈希校验只能验证来源绑定与完整性，不能证明文字识别准确；金额、编号、表格空单元格和
手写内容须对照原图。签名用待核对标记，不从笔迹猜测身份；不得用文件名时间补填表单空日期。

### 图片 Raw 包与持久溯源

文字型图片与论文 PDF 一样，使用「原件 + 忠实 Markdown」双表示，共同组成一个 Raw 来源，
不是两份相互印证的证据。原图是最终复核依据；Markdown 提供检索和行号引用；摘要、解释、
项目关联及跨来源推断属于 Wiki，不写入转写正文。

同目录归档示例：

```text
预算调整申请表.jpg
预算调整申请表.md
预算调整申请表.jpg.source.json
```

第三项沿用 `document-source-context-v1` 来源 sidecar，不是新增的事实材料：其 `ocr` 字段保存
原图与 companion 的包内文件名、原图/转写正文 SHA-256、backend、model、prompt_version、
OCR 回执的 `created` 时间、`review_required` 和 warnings。只复制这些白名单元数据，
不归档临时回执路径、任务内容或调用凭据。旧回执未提供生成时间时写 `null`，不以本次摄入时间补造。
OCR 时间是处理元数据，不是表单申请日、生效日或审批日；Agent 转写不猜测模型名称。

转写正文不插入元数据头或说明段，保证暂存 `doc.md` 与最终 companion 的全文、行号一致。
原图、Markdown 与 sidecar 进入同一受管 manifest；提交前逐项核对 sidecar 与已校验回执，
并强制同 stem 配对。任何缺失、改动或配对不符均拒绝提交，不静默跳过缺件。
旧的在途图片事务若尚未生成 sidecar，须先重新完成 preprocess，而非绕过校验直接 finalize。
成功来源日志引用最终 sidecar；即使临时产物清理后仍可追溯转写过程，但不能把溯源元数据当正文事实。

指纹重建只把原图作为来源，排除同 stem Markdown 与保留后缀 `.source.json`；普通独立 JSON 来源照常索引。
更正转写、补做复核或重新 OCR 必须走受管更新并记录日志，不直接编辑既有 Raw；哈希一致不等于已人工核实。
纯照片或示意图若无可识别文字，当前 OCR 路径明确停止并返回文字说明，不默认交由宿主识图、不伪造转写，
也不自动把模型生成的图像描述冒充原图文字。

### 复核记录与风险闸门

用 `image-ocr-review-v1` JSON 记录 `source_sha256`、`text_sha256`、`reviewer_kind`（api/agent/human）、
`reviewer`、含时区的 `reviewed_at`、`risk`（ordinary/critical）、`checks` 和 `limitations`。
每条 check 包含 `field`、`locator`（转写正文 Lx 或 Lx-Ly）、`critical`（布尔）、
`status`（verified/unresolved）、`note`。API 只返回风险、核对项和限制；程序写入实际模型身份、时间和源/文本哈希，拒绝模型自报人工身份。显式宿主模式下 Agent 必须实际对照原图，不把自身复核声称为人工确认。
风险和关键字段由复核者依据内容判断，程序只验证记录、源绑定与闸门，不模拟语义判断。

```bash
python3 .scripts/image_ocr.py inbox/image.jpg --check temp/image-ocr/example.json \
  --review-file temp/image-ocr/review.json --output temp/image-ocr/reviewed.json
python3 .scripts/ingest_document.py --file inbox/image.jpg --subproject admin \
  --ocr-result temp/image-ocr/reviewed.json
```

附加复核只做本地校验，不调用模型、不覆盖旧回执。金额、编号、审批状态等高风险字段须逐项列为关键项，
关键项未核对时阻止摄入。空白栏可核对为“确为空白”，不推断缺失值；无法辨认且并非本次事实所必需的签名等，
可作为非关键未决项保留。普通来源和非关键未决项允许带限制归档，不能丢弃 warning。
无未决项且存在核对项时为 api-reviewed/agent-reviewed/human-reviewed，否则为 partial；这不证明来源真实，也不代表事项获批。转写与复核调用分别保留尝试摘要、耗时和 provider 返回的 token 用量，不记录密钥或图片 payload。

图片 Wiki 由程序写 `source_type: ocr`、`ocr_review_status`、`ocr_review_required`，
有实际核对项时 `confidence: medium`，否则 `low`，不因 OCR 或 Agent 复核自动升为 high。
正文有独立“图片转写说明”，语义生成同样接收复核限制；完整复核记录持久化在来源 sidecar。
Wiki 生成后复核记录变化必须重新 preprocess 和生成/校验 Wiki，不能仅更换 sidecar 绕过一致性检查。

来源清理统一推迟至图校验、收尾、validate_completion 和 completed 状态持久化之后。
清理失败不撤销已提交内容，记录 cleanup_pending；对该 completed 事务 resume 只重试清理，不重跑生成、写图或日志。

### 状态解释与交付表述

摄入执行状态、转写复核状态和原件业务状态相互独立。复核记录用于保留证据边界，不自动构成用户待办。

| 状态或字段 | 实际含义 | 是否要求用户补充 |
|---|---|---|
| 事务 `status: completed` | 摄入已通过完成校验；不能据此推断原件事项已提交、获批或执行 | 不因摄入完成而新增补充要求 |
| OCR `review_status: partial` | 有未决核对项，或尚无逐项核对记录；不等于整个摄入未完成 | 根据当前任务是否依赖该项判断，不自动要求 |
| OCR `review_required: true` / Wiki `ocr_review_required: true` | 转写仍有复核限制；可与事务 completed 并存，不单凭此标记阻断 | 不自动生成用户待办 |
| 风险未评估，或关键字段未核对 | `review_blockers()` 返回阻断项，不能进入 Wiki/图生成 | 先由当前 backend 的合规流程处理；确需用户信息时再明确请求 |
| `cleanup_pending: true` | 内容已提交，但文件清理尚需重试；不是原件字段缺失 | 通常由受管 resume 重试，不要求补填材料 |

区分“确认原件为空白”和“无法辨认原件”：

- 原件日期栏确实没有填写时，可将该核对项记为 verified，结论是“原件未填写日期”，不是“日期值已获确认”。保持转写空白，不用文件名、OCR 时间或摄入时间代填。
- 原件存在签名但未确认姓名时，若当前摄入只需保留申请内容且不建立具名负责人关系，可作为非关键 unresolved 保留，不要求用户立即辨认或补签。
- 若后续任务必须依赖签名者身份、具体日期或审批结论，应重新按该任务评估关键性，并索取可靠证据；不能把本次非关键处理沿用为未来任务的确定事实。

已完成且无阻断项时，交付先说“已完成入库”，再按需说明“原件日期未填写，签名者姓名未确认，均按原样保留；当前无需补充”。
避免孤立地写“签名待人工核对、日期待补”，使用户误以为流程失败或被分配了新待办。
确有阻断项时，应明确“哪一步未完成、哪个关键字段缺证据、需要什么补充”，不使用上述无阻断表述。
这些用语约定不改变或清空 persisted warnings、复核记录和质量报告，也不把 Agent 复核改称人工确认。

## 验证

```bash
python3 .scripts/test_image_ocr.py
python3 .scripts/test_source_fingerprints.py
python3 .scripts/test_inbox_plan.py
python3 .scripts/test_ingest_document.py
python3 .scripts/test_ingest_pipeline.py
python3 .scripts/engineering_graph.py validate
```
