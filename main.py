"""Entry point: run the HTTP gateway locally."""

from __future__ import annotations

import uvicorn

from app.demo import build_app

app = build_app()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
