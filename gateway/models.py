"""核心数据模型与状态枚举（骨架：仅类型定义）。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class FunctionStatus(str, Enum):
    ENABLED = "enabled"
    DISABLED = "disabled"


class InvocationState(str, Enum):
    """一次调用的可解释终态（RUNNING 为中间态）。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"                 # 用户函数自身抛异常
    TIMED_OUT = "timed_out"           # 超出时限
    CONCURRENCY_REJECTED = "concurrency_rejected"  # 超出并发上限，立即拒绝
    REJECTED_DISABLED = "rejected_disabled"        # 函数已停用
    REJECTED_NO_VERSION = "rejected_no_version"    # 目标版本/别名不存在


# 用户函数签名：接收任意参数，返回任意结果
UserFunction = Callable[..., Any]


@dataclass
class FunctionVersion:
    """一个不可变的已发布版本。"""

    name: str
    version: str
    func: UserFunction
    timeout_seconds: float
    concurrency_limit: int
    registered_at: float = field(default_factory=time.time)


@dataclass
class FunctionRecord:
    """一个函数名的聚合记录：版本集合、别名、启停状态。"""

    name: str
    versions: dict[str, FunctionVersion] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)  # alias -> version
    status: FunctionStatus = FunctionStatus.ENABLED
    lock: threading.RLock = field(default_factory=threading.RLock)


@dataclass
class InvocationResult:
    """一次调用（同步/异步共用）的结论记录。"""

    request_id: str
    name: str
    target: str               # 实际调用的版本（解析别名后）
    via_alias: str | None     # 请求时使用的别名（若有）
    state: InvocationState
    payload: Any = None       # 成功结果 / 异常信息
    started_at: float = 0.0
    finished_at: float = 0.0
    transitions: list[str] = field(default_factory=list)  # 状态迁移依据

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "name": self.name,
            "target": self.target,
            "via_alias": self.via_alias,
            "state": self.state.value,
            "payload": self.payload,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "transitions": list(self.transitions),
        }
