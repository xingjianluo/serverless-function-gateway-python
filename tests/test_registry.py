"""版本不可变、重复注册拒绝、未发布版本拒绝、别名切换、启停语义。"""

import logging

import pytest

from gateway.errors import (
    AliasNotFoundError,
    FunctionDisabledError,
    FunctionNotFoundError,
    VersionAlreadyExistsError,
    VersionNotFoundError,
)
from gateway.models import FunctionStatus
from gateway.registry import Registry

logger = logging.getLogger("gateway.test_registry")


def _noop(_):
    return None


def test_register_and_resolve_version():
    reg = Registry()
    v1 = reg.register("f", "v1", _noop, timeout_seconds=2, concurrency_limit=3)
    got, alias = reg.resolve("f", "v1")
    assert got is v1 and alias is None


def test_duplicate_registration_rejected():
    reg = Registry()
    reg.register("f", "v1", _noop)

    def tampered(_):
        return "tampered"

    with pytest.raises(VersionAlreadyExistsError) as ei:
        reg.register("f", "v1", tampered)
    # 可区分的拒绝原因码
    assert ei.value.code == "version_already_exists"
    # 版本未被篡改：仍然是旧对象
    got, _ = reg.resolve("f", "v1")
    assert got.func is _noop
    logger.info("重复注册被拒绝，request_id=n/a state-basis=%s", ei.value.code)


def test_invoke_unpublished_version_rejected():
    reg = Registry()
    reg.register("f", "v1", _noop)
    with pytest.raises(VersionNotFoundError) as ei:
        reg.resolve("f", "v9")
    assert ei.value.code == "version_not_found"


def test_unknown_function_and_alias():
    reg = Registry()
    with pytest.raises(FunctionNotFoundError) as ei:
        reg.resolve("nope", "v1")
    assert ei.value.code == "function_not_found"

    reg.register("f", "v1", _noop)
    with pytest.raises(AliasNotFoundError) as ei:
        reg.resolve("f", "prod", kind="alias")
    assert ei.value.code == "alias_not_found"

    # 别名必须指向已发布版本
    with pytest.raises(VersionNotFoundError):
        reg.set_alias("f", "prod", "v9")


def test_alias_switch_points_to_distinct_versions():
    reg = Registry()
    reg.register("f", "v1", lambda p: "v1")
    reg.register("f", "v2", lambda p: "v2")
    reg.set_alias("f", "prod", "v1")
    got, alias = reg.resolve("f", "prod", kind="alias")
    assert got.version == "v1" and alias == "prod"

    reg.set_alias("f", "prod", "v2")  # 别名可切换
    got, alias = reg.resolve("f", "prod")
    assert got.version == "v2" and alias == "prod"


def test_disable_blocks_new_resolution_enable_restores():
    reg = Registry()
    reg.register("f", "v1", _noop)
    reg.disable("f")
    assert reg.status("f") is FunctionStatus.DISABLED
    with pytest.raises(FunctionDisabledError) as ei:
        reg.resolve("f", "v1")
    assert ei.value.code == "function_disabled"
    reg.enable("f")
    got, _ = reg.resolve("f", "v1")
    assert got.version == "v1"


def test_invalid_registration_arguments():
    reg = Registry()
    with pytest.raises(ValueError):
        reg.register("f", "v1", _noop, timeout_seconds=0)
    with pytest.raises(ValueError):
        reg.register("f", "v1", _noop, concurrency_limit=0)
