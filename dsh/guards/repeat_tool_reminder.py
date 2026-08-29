"""repeat-tool-reminder — 重复工具调用提醒 guard。

借鉴 DSH packages/guard/repeat-tool-reminder：
- 观察 tools/post-execute，计数连续相同调用
- 达到阈值时注入 additional_contexts 提醒（不否决，不替换）
- 用户消息重置计数链
- 提醒标记为 plugin source（不混入用户消息）
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from dsh.harness import ToolExecution, ToolExecutionResult, PostToolDecision


GENTLE_REMINDER = (
    "你在用完全相同的参数重复调用同一个工具。"
    "请仔细分析上次结果后再决定：如果任务未完成，尝试不同方法或不同参数，"
    "而不是重复调用。"
)


def _canonicalize(args: dict) -> str:
    """深度键排序后序列化，使属性顺序不同的对象规范化一致。"""
    def sort_value(value):
        if isinstance(value, list):
            return [sort_value(v) for v in value]
        if isinstance(value, dict):
            return {k: sort_value(value[k]) for k in sorted(value)}
        return value
    return json.dumps(sort_value(args), sort_keys=True, ensure_ascii=False)


def detailed_reminder(tool_name: str, count: int, canonical_args: str) -> str:
    return (f"检测到重复工具调用:\n"
            f"- 工具: {tool_name}\n"
            f"- 连续次数: {count}\n"
            f"- 参数: {canonical_args[:500]}\n"
            f"重复调用没有进展。不要再用相同参数调用此工具。"
            f"检查最新结果，选择不同动作或不同参数，或已有足够证据时结束任务。")


@dataclass
class RepeatChain:
    """一个 agent 的连续重复链：上次跟踪调用的身份键和运行长度。"""
    key: str = ""
    count: int = 0


class RepeatToolReminder:
    """重复工具调用提醒 guard。

    config:
    - thresholds: 触发提醒的连续重复计数列表（默认 [3, 5, 8]）
    - first_threshold 用 gentle 提醒，后续用 detailed
    """

    def __init__(self, thresholds: list[int] | None = None):
        self.thresholds = sorted(thresholds or [3, 5, 8])
        self.threshold_set = set(self.thresholds)
        self._chains: dict[str, RepeatChain] = {}  # agent_id -> chain

    def _observe(self, exec_ctx: ToolExecution) -> dict | None:
        """推进调用 agent 的链，返回提醒消息（如果命中阈值）。"""
        if not exec_ctx.agent_id:
            return None
        canonical = _canonicalize(exec_ctx.arguments)
        key = json.dumps([exec_ctx.name, canonical], ensure_ascii=False)
        chain = self._chains.get(exec_ctx.agent_id)
        count = chain.count + 1 if chain and chain.key == key else 1
        self._chains[exec_ctx.agent_id] = RepeatChain(key=key, count=count)
        if count not in self.threshold_set:
            return None
        text = (GENTLE_REMINDER if count == self.thresholds[0]
                else detailed_reminder(exec_ctx.name, count, canonical))
        return {"role": "user", "content": text, "source": "plugin",
                "form": "notice", "summary": f"{exec_ctx.name} × {count}"}

    def on_post_execute(self, exec_ctx: ToolExecution,
                        result: ToolExecutionResult) -> PostToolDecision | None:
        """观察不否决：先计数，委托，然后把提醒附加到结果上。"""
        reminder = self._observe(exec_ctx)
        if not reminder:
            return None
        return PostToolDecision(kind="accept",
                                additional_contexts=[reminder])

    def on_pre_step(self, messages: list[dict]):
        """用户消息重置计数链（跨用户消息的重复不是循环）。"""
        # 简化：PreStepDecision 无返回值版本——guard 直接清链
        # 实际由 agent loop 调用此方法
        for msg in messages:
            if msg.get("source") == "user" or msg.get("role") == "user":
                self._chains.clear()
                break
        return None
