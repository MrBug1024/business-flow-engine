"""鉴权接口 /api/auth/*。"""

from __future__ import annotations

import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse

from ..config import settings
from . import db, oauth
from .deps import get_current_user
from .models import (
    LoginRequest,
    PublicUser,
    RegisterRequest,
    TokenResponse,
    to_public_user,
)
from .security import hash_password, issue_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/providers")
def providers() -> dict:
    """返回可用的登录方式（前端据此显示 OAuth 按钮）。"""
    return {"password": True, **settings.oauth_providers}


@router.post("/register", response_model=TokenResponse, status_code=201)
def register(req: RegisterRequest) -> TokenResponse:
    if db.get_user_by_email(req.email):
        raise HTTPException(status_code=409, detail="该邮箱已注册")
    row = db.create_user(
        email=str(req.email), password_hash=hash_password(req.password),
        name=req.name, provider="password",
    )
    return TokenResponse(token=issue_token(row["id"]), user=to_public_user(row))


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest) -> TokenResponse:
    row = db.get_user_by_email(req.email)
    if not row or not verify_password(req.password, row.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="邮箱或密码错误")
    return TokenResponse(token=issue_token(row["id"]), user=to_public_user(row))


@router.get("/me", response_model=PublicUser)
def me(user: PublicUser = Depends(get_current_user)) -> PublicUser:
    return user


@router.post("/logout")
def logout() -> dict:
    # JWT 无状态：登出由前端丢弃 token 完成。此端点仅为语义完整。
    return {"message": "已登出"}


# ------------------------------------------------------------------- OAuth
# state 为一次性防 CSRF 值；单机场景用进程内集合暂存
_oauth_states: set[str] = set()


@router.get("/oauth/{provider}/login")
def oauth_login(provider: str) -> RedirectResponse:
    if provider not in ("google", "github"):
        raise HTTPException(status_code=404, detail="未知的 OAuth 提供方")
    if not oauth.is_enabled(provider):
        raise HTTPException(status_code=501, detail=f"{provider} 登录未配置（请在 .env 填写 client_id/secret）")
    state = secrets.token_urlsafe(16)
    _oauth_states.add(state)
    return RedirectResponse(oauth.authorize_url(provider, state))


@router.get("/oauth/{provider}/callback")
async def oauth_callback(provider: str, code: str = "", state: str = "") -> RedirectResponse:
    front = settings.frontend_base_url.rstrip("/")
    if provider not in ("google", "github") or not oauth.is_enabled(provider):
        return RedirectResponse(f"{front}/login?error=oauth_disabled")
    if not code or state not in _oauth_states:
        return RedirectResponse(f"{front}/login?error=oauth_state")
    _oauth_states.discard(state)

    info = await oauth.exchange_user(provider, code)
    if not info or not info.get("email"):
        return RedirectResponse(f"{front}/login?error=oauth_failed")

    # 先按 OAuth 身份找，再按邮箱找，最后新建
    row = db.get_user_by_oauth(provider, info["uid"])
    if not row:
        row = db.get_user_by_email(info["email"])
        if not row:
            row = db.create_user(
                email=info["email"], name=info.get("name", ""),
                avatar=info.get("avatar", ""), provider=provider,
            )
        db.link_oauth(provider, info["uid"], row["id"])

    token = issue_token(row["id"])
    # 带 token 重定向回前端的 OAuth 回调页
    return RedirectResponse(f"{front}/oauth/callback?{urlencode({'token': token})}")
