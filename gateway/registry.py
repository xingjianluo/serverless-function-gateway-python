"""函数注册中心：版本不可变、别名解析、启停。

并发约定：
- 注册表结构由 ``Registry._lock`` 保护（取/建函数记录）；
- 单个函数记录内部（版本表、别名表、状态）由该记录自己的 ``lock`` 保护；
- 版本对象本身不可变：解析到的版本在执行期间不会被任何切换篡改。
"""

from __future__ import annotations

import threading

from .errors import (
    AliasNotFoundError,
    FunctionDisabledError,
    FunctionNotFoundError,
    VersionAlreadyExistsError,
    VersionNotFoundError,
)
from .models import FunctionRecord, FunctionStatus, FunctionVersion, UserFunction


class Registry:
    def __init__(self) -> None:
        self._functions: dict[str, FunctionRecord] = {}
        self._lock = threading.RLock()

    def register(
        self,
        name: str,
        version: str,
        func: UserFunction,
        *,
        timeout_seconds: float = 5.0,
        concurrency_limit: int = 1,
    ) -> FunctionVersion:
        if not name or not version:
            raise ValueError("name 与 version 不能为空")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须为正数")
        if concurrency_limit < 1:
            raise ValueError("concurrency_limit 至少为 1")

        with self._lock:
            record = self._functions.get(name)
            if record is None:
                record = FunctionRecord(name=name)
                self._functions[name] = record

        with record.lock:
            if version in record.versions:
                # 版本一经注册不可篡改：同名同版本一律拒绝，绝不覆盖
                raise VersionAlreadyExistsError(
                    f"函数 {name} 版本 {version} 已存在，禁止重复注册或篡改"
                )
            ver = FunctionVersion(
                name=name,
                version=version,
                func=func,
                timeout_seconds=float(timeout_seconds),
                concurrency_limit=int(concurrency_limit),
            )
            record.versions[version] = ver
            return ver

    def resolve(
        self, name: str, version_or_alias: str, *, kind: str = "auto"
    ) -> tuple[FunctionVersion, str | None]:
        """把 版本/别名 解析为具体不可变版本。

        kind: "version" 仅按版本查；"alias" 仅按别名查；"auto" 先版本后别名。
        返回 ``(版本对象, 命中的别名或 None)``。
        """
        record = self.get_record(name)
        with record.lock:
            if record.status is FunctionStatus.DISABLED:
                raise FunctionDisabledError(f"函数 {name} 已停用")

            if kind in ("version", "auto") and version_or_alias in record.versions:
                return record.versions[version_or_alias], None

            if kind in ("alias", "auto") and version_or_alias in record.aliases:
                target = record.aliases[version_or_alias]
                return record.versions[target], version_or_alias

            if kind == "alias":
                raise AliasNotFoundError(
                    f"函数 {name} 的别名 {version_or_alias} 不存在"
                )
            raise VersionNotFoundError(
                f"函数 {name} 的版本 {version_or_alias} 未发布"
            )

    def set_alias(self, name: str, alias: str, version: str) -> None:
        """把别名指向一个已发布版本（别名可反复切换；版本必须存在）。"""
        record = self.get_record(name)
        with record.lock:
            if version not in record.versions:
                raise VersionNotFoundError(
                    f"无法为未发布版本 {version} 设置别名"
                )
            record.aliases[alias] = version

    def enable(self, name: str) -> None:
        record = self.get_record(name)
        with record.lock:
            record.status = FunctionStatus.ENABLED

    def disable(self, name: str) -> None:
        record = self.get_record(name)
        with record.lock:
            record.status = FunctionStatus.DISABLED

    def status(self, name: str) -> FunctionStatus:
        return self.get_record(name).status

    def get_record(self, name: str) -> FunctionRecord:
        with self._lock:
            record = self._functions.get(name)
        if record is None:
            raise FunctionNotFoundError(f"函数 {name} 不存在")
        return record
