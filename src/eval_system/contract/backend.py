"""ExecutionBackend 协议 + Trial 句柄 — CONTRACT.md §4, §6.

后端协议只有 4 个能力：run / result / cancel / read_trial。
调度/并发/重试/队列不在协议里 —— 它们是上层或后端内部的事。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any

from eval_system.contract.specs import AgentSpec, EnvironmentSpec, TaskSpec
from eval_system.contract.trial import TrialResult


class Trial(ABC):
    """运行句柄（有状态，不落盘为契约）。

    给 hooks / 流式 / 控制面用；打分层永远只拿 TrialResult。
    """

    def __init__(self, trial_id: str, run_id: str | None = None):
        self.trial_id = trial_id
        self.run_id = run_id

    @property
    @abstractmethod
    def status(self) -> str:
        """pending | running | finished | failed | cancelled"""

    @abstractmethod
    async def result(self) -> TrialResult:
        """取终态（阻塞直到完成）。"""

    @abstractmethod
    async def cancel(self) -> None:
        """可选能力，不支持时抛 NotImplementedError。"""


class ExecutionBackend(ABC):
    """评测执行后端的最小协议面。"""

    name: str = "abstract"

    @abstractmethod
    async def run(
        self,
        task: TaskSpec,
        agent: AgentSpec,
        environment: EnvironmentSpec,
        *,
        run_id: str | None = None,
    ) -> Trial:
        """启动一次运行（内部负责落盘 specs/）。"""

    @abstractmethod
    async def read_trial(self, trial_id: str) -> TrialResult:
        """离线读取历史 trial（按 ID）。"""

    def list_trials(self) -> list[str]:
        """列出可读的 trial ID（后端可按需实现）。"""
        raise NotImplementedError

    async def run_and_result(
        self,
        task: TaskSpec,
        agent: AgentSpec,
        environment: EnvironmentSpec,
        *,
        run_id: str | None = None,
    ) -> TrialResult:
        """便捷：run + 等待终态。"""
        trial = await self.run(task, agent, environment, run_id=run_id)
        return await trial.result()


# ---------------------------------------------------------------------------
# 轮询句柄（供 subprocess 型后端复用）：包装一个 asyncio 任务
# ---------------------------------------------------------------------------
class AsyncTrial(Trial):
    """基于 asyncio 任务的通用句柄实现。"""

    def __init__(
        self,
        trial_id: str,
        run_id: str | None,
        task: asyncio.Task[TrialResult],
    ):
        super().__init__(trial_id, run_id)
        self._task = task

    @property
    def status(self) -> str:
        if self._task.done():
            return "finished" if self._task.exception() is None else "failed"
        return "running"

    async def result(self) -> TrialResult:
        return await self._task

    async def cancel(self) -> None:
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


def run_async(coro: Any) -> Any:
    """在当前或新建事件循环中执行协程（CLI/同步调用场景）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # 已在事件循环内：交由调用方 await（此处仅兜底，返回 coroutine）
    return coro
