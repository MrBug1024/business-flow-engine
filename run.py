"""开发服务器入口。

用法：
    python run.py
或：
    uvicorn app.main:app --reload
"""

from __future__ import annotations

import uvicorn

from app.core.config import settings

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=settings.host, port=settings.port)
