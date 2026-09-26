"""Test-wide logging configuration.

The engine and registry log every state transition together with the
request id and the *reason* for the transition. Pytest captures these log
records; they are printed for failing tests, and can be streamed live with
``--log-cli-level=INFO`` (see README, "验证方法").
"""

from __future__ import annotations

import logging

GATEWAY_LOGGERS = ("gateway", "gateway.engine", "gateway.registry")


def _configure() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    for name in GATEWAY_LOGGERS:
        log = logging.getLogger(name)
        log.setLevel(logging.INFO)
        if not any(getattr(h, "_gateway_test", False) for h in log.handlers):
            handler._gateway_test = True  # type: ignore[attr-defined]
            log.addHandler(handler)
        log.propagate = True


_configure()
