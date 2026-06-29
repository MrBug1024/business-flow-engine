"""开发服务器入口。

用法：
    python run.py
或：
    uvicorn backend.app:app --reload
"""

from __future__ import annotations

import uvicorn

from backend.config import settings

if __name__ == "__main__":
    uvicorn.run("backend.app:app", host=settings.host, port=settings.port, reload=True)
