"""可插拔 OAuth（Google / GitHub），用 httpx 手写授权码流程——零额外依赖。

仅当 `.env` 配置了对应 client_id/secret 时启用；否则相关端点返回 501、前端隐藏按钮。
授权码换 token、再拉用户信息，建/取本地用户并签发本平台 JWT，最后带 token 重定向回前端。
"""

from __future__ import annotations

from typing import Optional

import httpx

from ..config import settings

# 每个 provider 的端点与 scope
_PROVIDERS = {
    "google": {
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "userinfo": "https://openidconnect.googleapis.com/v1/userinfo",
        "scope": "openid email profile",
    },
    "github": {
        "authorize": "https://github.com/login/oauth/authorize",
        "token": "https://github.com/login/oauth/access_token",
        "userinfo": "https://api.github.com/user",
        "scope": "read:user user:email",
    },
}


def is_enabled(provider: str) -> bool:
    return settings.oauth_providers.get(provider, False)


def _creds(provider: str) -> tuple[str, str]:
    if provider == "google":
        return settings.google_client_id, settings.google_client_secret
    if provider == "github":
        return settings.github_client_id, settings.github_client_secret
    return "", ""


def redirect_uri(provider: str) -> str:
    return f"{settings.oauth_redirect_base.rstrip('/')}/api/auth/oauth/{provider}/callback"


def authorize_url(provider: str, state: str) -> str:
    cfg = _PROVIDERS[provider]
    client_id, _ = _creds(provider)
    from urllib.parse import urlencode
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(provider),
        "scope": cfg["scope"],
        "state": state,
        "response_type": "code",
    }
    if provider == "google":
        params["access_type"] = "online"
    return f"{cfg['authorize']}?{urlencode(params)}"


async def exchange_user(provider: str, code: str) -> Optional[dict]:
    """用授权码换取用户信息，归一化为 {uid, email, name, avatar}。失败返回 None。"""
    cfg = _PROVIDERS[provider]
    client_id, client_secret = _creds(provider)
    async with httpx.AsyncClient(timeout=15) as client:
        token_res = await client.post(
            cfg["token"],
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri(provider),
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
        )
        token_json = token_res.json()
        access_token = token_json.get("access_token")
        if not access_token:
            return None

        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        info = (await client.get(cfg["userinfo"], headers=headers)).json()

        if provider == "google":
            return {
                "uid": info.get("sub"),
                "email": (info.get("email") or "").lower(),
                "name": info.get("name", ""),
                "avatar": info.get("picture", ""),
            }
        # github：邮箱可能私密，需额外拉一次
        email = info.get("email")
        if not email:
            emails = (await client.get("https://api.github.com/user/emails", headers=headers)).json()
            if isinstance(emails, list):
                primary = next((e for e in emails if e.get("primary")), None) or (emails[0] if emails else {})
                email = primary.get("email")
        return {
            "uid": str(info.get("id")),
            "email": (email or f"{info.get('login','user')}@users.noreply.github.com").lower(),
            "name": info.get("name") or info.get("login", ""),
            "avatar": info.get("avatar_url", ""),
        }
