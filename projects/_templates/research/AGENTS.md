# {{PROJECT_ID}} 项目规则

本目录是研究项目，须同时遵守仓库根目录 `AGENTS.md` 与 `operations/RESEARCH.md`。

进入项目先恢复上下文：

```bash
python3 .scripts/workspace_state.py recall {{PROJECT_ID}}
```

并读取同目录 `schema.yaml`。

## 产物边界

- 研究产物全部放置在 `projects/{{PROJECT_ID}}/` 的子文件夹内部。
- 代码生成物写入 `outputs/`；`codes/` 只放源码和文档。

## 代码管理

- 先读 `codes/README.md` 与 `codes/docs/PHYSICS.md`，再修改代码。
- 新实验放 `codes/experiments/`，分析/绘图放 `codes/analysis/`，验证/测试放 `codes/tests/`。
- 修改核心模块后运行最小验证，再启动大规模任务。
