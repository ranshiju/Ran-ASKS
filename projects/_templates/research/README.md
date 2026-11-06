# {{PROJECT_NAME}} 研究项目

## 项目主题

{{TOPIC}}

## Agent 进入顺序

1. 恢复上下文：`python3 .scripts/research_memory.py recall {{PROJECT_ID}}`
2. 读 `schema.yaml` 与 `AGENTS.md`
3. 读 `codes/README.md` 与 `codes/docs/PHYSICS.md`
4. 按 `operations/RESEARCH.md` 执行研究工作流

## 目录

- `notes/`：研究笔记、推导、`status.md`
- `manu/`：论文文稿
- `codes/`：数值代码、实验、分析、测试
- `outputs/`：图、数据、checkpoint、日志
- `.research-memory/`：自动研究记忆

## 硬规则

- 研究产物全部放置在 `projects/{{PROJECT_ID}}/` 的子文件夹内部。
- 数值计算策略以 `schema.yaml` 的 `compute` 段为准。
- 研究内容不入 raw/wiki/graph；如需入库，另行切换 ingest 任务。
