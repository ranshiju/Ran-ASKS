# {{PROJECT_NAME}} 代码工程文档

## 代码定位

本目录是 `{{PROJECT_ID}}` 的数值/分析代码工作区。

## 目录

- `experiments/`：实验入口
- `analysis/`：绘图、对比、数据分析
- `tests/`：验证、回归、正确性测试
- `docs/`：物理/数学与代码映射
- `formulas/`：实现公式

## 运行与修改纪律

- 先读 `docs/PHYSICS.md`，再改模型或目标函数。
- 所有生成物写 `projects/{{PROJECT_ID}}/outputs/`。
- 新增实验、分析、测试分别放入对应子目录。
