# API 漫画生成规范

> 通用 Agent 绘图工具：从受控模型目录选择远程图片生成模型，将漫画图片与可审计记录写入明确的 `outputs/`。它不是事实源，也不修改文章、知识库或图数据库。

## 使用边界

- 适用于研究写作、公众号文章、教学材料、行政宣传材料及其他明确要求生成漫画配图的任务。
- 语义工作由 Agent 完成：确定单图知识点、分镜、构图和事实约束。
- 程序完成机械工作：模型白名单、路径校验、API 请求、图片验证、原子落盘和 receipt。
- 图片中的科学结构、文字、箭头、数字和单位必须人工复核；生成成功不等于内容正确。

## 模型目录

候选模型登记在 `operations/config/llm-models.yaml` 的 `image_generation.candidates`。候选默认均为 `probe_status: pending`，只有实际探测并记录 endpoint、请求兼容性、成本和效果后才能设为默认模型。

列出候选不联网：

```bash
python3 .scripts/comic_generation.py models
```

## API 配置

工具读取仓库根 `.env`，也接受同名进程环境变量；进程环境优先。同文件 `${NAME}` 引用会展开。

```text
COMIC_IMAGE_API_BASE=${LLM_API_BASE}
COMIC_IMAGE_API_KEY=${LLM_API_KEY}
COMIC_IMAGE_MODEL=Doubao-Seedream-4.5
COMIC_IMAGE_ENDPOINT=/v1/images/generations
COMIC_IMAGE_RESPONSE_FORMAT=b64_json
```

- `COMIC_IMAGE_MODEL` 可省略，调用时显式传 `--model`。
- endpoint 默认采用 HTTPS OpenAI-compatible `/v1/images/generations`；API POST 不跟随重定向。
- 不同供应商的附加请求字段用 `--parameters` 或 storyboard 的 `parameters` 传递。
- API key 不得出现在命令行、storyboard、manifest、receipt 或日志中。

## 单图生成

先用 dry-run 检查模型、路径和参数；dry-run 不联网、不写文件：

```bash
python3 .scripts/comic_generation.py generate \
  --project asks-ai-agent \
  --article-id 00-introduction \
  --asset-id cover \
  --model Doubao-Seedream-4.5 \
  --size 1792x1024 \
  --prompt '清爽的知识科普漫画；画面只表达一个主题……' \
  --dry-run
```

真实调用必须额外传 `--allow-remote`。该参数明确授权把 Prompt 发给远程供应商：

```bash
python3 .scripts/comic_generation.py generate ... --allow-remote
```

一般任务也可使用显式输出根：

```bash
python3 .scripts/comic_generation.py generate \
  --output-root teaching/outputs \
  --article-id agent-intro \
  --asset-id panel-01 \
  --model GLM-CogView3-Flash \
  --prompt '……' \
  --allow-remote
```

输出根必须是仓库内已经存在、且目录名为 `outputs` 的目录；`raw/`、`wiki/`、`private/`、`inbox/` 和 `.research-memory/` 下路径一律拒绝。

## 批量分镜

storyboard 必须是仓库内的 YAML/JSON 源文件，不能来自 `outputs/`、`.research-memory/` 或受保护事实目录：

```yaml
version: 1
article_id: 00-introduction
style: >-
  清爽的编辑类知识漫画，明确线条，简化背景，手机端缩小后仍清楚，无水印。
assets:
  - id: cover
    size: 1792x1024
    prompt: 用一个人与 AI 助手协作的场景提出“什么是 AI Agent”。
  - id: panel-01
    prompt: 用单幅漫画表现聊天机器人只回答问题，而 Agent 会调用工具完成任务。
```

```bash
python3 .scripts/comic_generation.py batch \
  --project asks-ai-agent \
  --storyboard projects/asks-ai-agent/notes/storyboard-example.yaml \
  --model Doubao-Seedream-4.5 \
  --dry-run
```

真实批量调用同样需要 `--allow-remote`。批量任务逐图提交；中途失败会保留已完成图片并显式退出，不自动切换模型掩盖风格变化。

## 输出与审核

输出位于 `<output-root>/<article-id>/images/`：

```text
images/
  cover.png
  panel-01.png
  manifest.json
  prompts.jsonl
  receipts/
    <run-id>.json
```

- `manifest.json` 记录成功产物、模型、哈希、耗时和 `review_status: pending`。
- `prompts.jsonl` 保存生成时的完整 Prompt，便于复现和编辑。
- receipt 不保存 API key、Authorization、base64 图片、远程临时 URL或完整供应商响应。
- 已存在图片默认拒绝覆盖；确需替换时显式传 `--overwrite`，旧版本应先由人工归档。
- 定稿前可另行调用 `visual_qa.py` 做布局与可读性质检，但视觉 QA 不能判断科学事实真伪。

## Agent 工具接口

需要绘图的 DSH Agent 显式加载：

```python
from dsh.comic_tools import build_comic_tools

for tool in build_comic_tools():
    registry.register(tool)
```

接口包含 `comic_models`、`comic_generate` 和 `comic_batch`。API base、endpoint 和 key 不在工具 schema 中，Agent 无法通过一次工具调用将现有密钥改发到其他地址。该工具集不得注册进 `query_actions`、CitationGuard 事实查询链或只读 `VisualAgentLoop`。

## 最小验证

```bash
python3 .scripts/test_comic_generation.py
python3 .scripts/engineering_graph.py validate
```
