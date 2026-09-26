"""Smoke test: the package imports and a gateway can be constructed."""

from app import Gateway


def test_package_smoke() -> None:
    gateway = Gateway()
    assert gateway.registry is not None
    assert gateway.engine is not None
