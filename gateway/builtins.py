"""HTTP 注册可用的内置函数类型（真实函数仍可通过 Python API 注册）。

payload 约定为 dict，各函数从中取参；也兼容 echo 回传任意 payload。
"""

from __future__ import annotations

import time
from typing import Any, Callable


def echo(payload: Any) -> Any:
    return payload


def add(payload: dict[str, Any]) -> float:
    return float(payload["a"]) + float(payload["b"])


def sleep_echo(payload: dict[str, Any]) -> dict[str, Any]:
    seconds = float(payload.get("seconds", 0.1))
    time.sleep(seconds)
    return {"slept": seconds}


def always_error(payload: Any) -> None:
    raise RuntimeError("用户函数主动失败")


BUILTINS: dict[str, Callable[..., Any]] = {
    "echo": echo,
    "add": add,
    "sleep_echo": sleep_echo,
    "error": always_error,
}
