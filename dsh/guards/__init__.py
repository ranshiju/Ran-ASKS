"""guards — DSH cockpit 可插拔守卫包。

借鉴 DSH packages/guard/：每个 guard 是自包含插件，监听事件，转换行为。
- repeat_tool_reminder: 检测重复工具调用，注入提醒（不否决）
- timeout_policy: 每次调用的截止时间策略
- citation_guard: 回答引用必须存在于已读来源
- build_locator_guard: 建设任务必须先生成影响面，再通过 locator 精读
"""
