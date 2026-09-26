"""Registry semantics: immutability, distinct rejections, alias switching."""

from __future__ import annotations

import logging

import pytest

from app.errors import (
    AliasNotFoundError,
    FunctionDisabledError,
    FunctionNotFoundError,
    InvalidRequestError,
    VersionAlreadyExistsError,
    VersionNotFoundError,
)
from app.registry import FunctionRegistry
from app.types import Target, VersionConfig


@pytest.fixture()
def registry() -> FunctionRegistry:
    return FunctionRegistry()


def test_register_then_resolve(registry: FunctionRegistry) -> None:
    registry.register("echo", "v1", lambda p: p)
    fv = registry.resolve("echo", Target.version("v1"))
    assert fv.version == "v1"


def test_reregister_same_version_rejected_distinctly(
    registry: FunctionRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    registry.register("echo", "v1", lambda p: p)
    with caplog.at_level(logging.INFO, logger="gateway.registry"):
        with pytest.raises(VersionAlreadyExistsError) as exc:
            registry.register("echo", "v1", lambda p: "tampered")
    assert exc.value.code == "VERSION_ALREADY_EXISTS"
    # The original handler is untouched: versions are immutable.
    assert registry.resolve("echo", Target.version("v1")).handler(1) == 1


def test_same_version_on_other_function_is_fine(
    registry: FunctionRegistry,
) -> None:
    registry.register("a", "v1", lambda p: p)
    registry.register("b", "v1", lambda p: p)
    assert registry.list_versions("a") == ["v1"]
    assert registry.list_versions("b") == ["v1"]


def test_unpublished_version_rejected_distinctly(
    registry: FunctionRegistry,
) -> None:
    registry.register("echo", "v1", lambda p: p)
    with pytest.raises(VersionNotFoundError) as exc:
        registry.resolve("echo", Target.version("v9"))
    assert exc.value.code == "VERSION_NOT_FOUND"


def test_unknown_function_rejected_distinctly(
    registry: FunctionRegistry,
) -> None:
    with pytest.raises(FunctionNotFoundError) as exc:
        registry.resolve("ghost", Target.version("v1"))
    assert exc.value.code == "FUNCTION_NOT_FOUND"


def test_alias_requires_published_version(registry: FunctionRegistry) -> None:
    registry.register("echo", "v1", lambda p: p)
    with pytest.raises(VersionNotFoundError) as exc:
        registry.set_alias("echo", "live", "v404")
    assert exc.value.code == "VERSION_NOT_FOUND"


def test_alias_switch_is_atomic_and_resolves_to_new_version(
    registry: FunctionRegistry,
) -> None:
    registry.register("echo", "v1", lambda p: "one")
    registry.register("echo", "v2", lambda p: "two")
    registry.set_alias("echo", "live", "v1")
    assert registry.resolve("echo", Target.alias("live")).version == "v1"
    registry.set_alias("echo", "live", "v2")
    assert registry.resolve("echo", Target.alias("live")).version == "v2"
    assert registry.get_alias("echo", "live") == "v2"


def test_unknown_alias_rejected_distinctly(registry: FunctionRegistry) -> None:
    registry.register("echo", "v1", lambda p: p)
    with pytest.raises(AliasNotFoundError) as exc:
        registry.resolve("echo", Target.alias("nope"))
    assert exc.value.code == "ALIAS_NOT_FOUND"


def test_disable_rejects_new_but_enable_restores(
    registry: FunctionRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    registry.register("echo", "v1", lambda p: p)
    registry.set_enabled("echo", False)
    with caplog.at_level(logging.INFO, logger="gateway.registry"):
        with pytest.raises(FunctionDisabledError) as exc:
            registry.resolve("echo", Target.version("v1"))
    assert exc.value.code == "FUNCTION_DISABLED"
    assert "reason=function_disabled" in caplog.text
    registry.set_enabled("echo", True)
    assert registry.resolve("echo", Target.version("v1")).version == "v1"


def test_invalid_names_and_config_rejected(registry: FunctionRegistry) -> None:
    with pytest.raises(InvalidRequestError):
        registry.register("", "v1", lambda p: p)
    with pytest.raises(InvalidRequestError):
        registry.register("echo", "", lambda p: p)
    with pytest.raises(InvalidRequestError):
        registry.register("echo", "v1", "not-callable")  # type: ignore[arg-type]
    with pytest.raises(InvalidRequestError):
        registry.register(
            "echo", "v1", lambda p: p, VersionConfig(timeout_seconds=0)
        )
    with pytest.raises(InvalidRequestError):
        registry.register(
            "echo", "v1", lambda p: p, VersionConfig(max_concurrency=0)
        )
