"""本地无服务器函数调用平台入口。

运行：uv run uvicorn main:app --reload
"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from gateway.api import build_router, create_gateway


def create_app() -> FastAPI:
    app = FastAPI(title="Local Serverless Function Gateway")
    gateway = create_gateway()
    app.state.gateway = gateway
    app.include_router(build_router(gateway))
    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
