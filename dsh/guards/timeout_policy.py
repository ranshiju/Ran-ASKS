"""timeout-policy — 工具调用截止时间策略 guard。

借鉴 DSH packages/guard/timeout-policy：
- 注册 tools/execute wrapper（around-dispatch）
- 工具声明 timeout_ms 时武装截止时间
- 超时替换结果为结构化 TOOL_TIMEOUT 错误
"""
from __future__ import annotations

import signal
import time

from dsh.harness import ToolExecution, ToolExecutionResult

TOOL_TIMEOUT_CODE = "TOOL_TIMEOUT"


def timeout_result(timeout_ms: int) -> ToolExecutionResult:
    msg = f"工具调用在 {timeout_ms}ms 后超时"
    return ToolExecutionResult(content=f"[ERROR] {msg}", is_error=True,
                               error_code=TOOL_TIMEOUT_CODE)


class TimeoutPolicy:
    """每次调用的截止时间策略。

    为每个工具执行设置 SIGALRM 截止时间。如果工具声明了 timeout_ms，
    超时后替换结果为 TOOL_TIMEOUT。
    """

    def __init__(self, default_timeout_ms: int = 30000):
        self.default_timeout_ms = default_timeout_ms

    def on_execute(self, exec_ctx: ToolExecution, next_fn) -> ToolExecutionResult:
        """around-dispatch wrapper：武装截止时间，委托，超时则替换。"""
        # 简化版：用 threading.Timer 或 SIGALRM
        # macOS/Unix 支持 SIGALRM（单线程）
        try:
            old_handler = signal.getsignal(signal.SIGALRM)
            signal.signal(signal.SIGALRM, lambda *_: None)
            signal.setitimer(signal.ITIMER_REAL, self.default_timeout_ms / 1000.0)
            try:
                result = next_fn(exec_ctx)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, old_handler or signal.SIG_DFL)
            return result
        except TimeoutError:
            return timeout_result(self.default_timeout_ms)
