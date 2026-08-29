"""dsh — DSH cockpit 适配层：借鉴 DeepSeek Harness 概念的轻量 Python 实现。

不依赖 Cordis 运行时，不侵入 WikiGraph 核心。将 wg.py 能力包装为带 hook
守卫的工具，提供 agent loop（turn/step）、session log 不变量和可插拔 guard。

核心概念（对应 DSH 原版）：
- Hook 瀑布: tools/pre-execute → tools/execute → tools/post-execute
- Guard 包: 自包含插件，监听事件，转换行为
- Session log: "model-visible means logged"——到达模型的一切可从日志重建
- Capability seam: wg.py 能力 → 带 schema 的注册工具

硬边界：dsh/ 可调用 WikiGraph 工具，但绝不绕过 raw/ 红线、graph schema
或摄入校验。WikiGraph 保持文件型知识内核，dsh/ 是可选的 agent cockpit。
"""
