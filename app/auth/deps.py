"""FastAPI 鉴权依赖：从 Authorization: Bearer 解析当前用户。"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import db
from .models import PublicUser, to_public_user
from .security import decode_token

# auto_error=False：让"可选登录"场景也能复用同一个方案
_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> PublicUser:
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=401, detail="未登录：缺少访问令牌")
    user_id = decode_token(creds.credentials)
    if not user_id:
        raise HTTPException(status_code=401, detail="登录已过期或令牌无效，请重新登录")
    row = db.get_user(user_id)
    if not row:
        raise HTTPException(status_code=401, detail="用户不存在")
    return to_public_user(row)


def get_optional_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Optional[PublicUser]:
    if creds is None or not creds.credentials:
        return None
    user_id = decode_token(creds.credentials)
    if not user_id:
        return None
    row = db.get_user(user_id)
    return to_public_user(row) if row else None
