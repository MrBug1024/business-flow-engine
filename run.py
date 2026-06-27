"""开发服务器入口。

用法：
    conda activate counter_flow_envs
    python run.py

或：
    uvicorn app.main:app --reload
"""

from __future__ import annotations

import uvicorn

from app.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
