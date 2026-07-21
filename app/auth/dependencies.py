"""FastAPI account dependencies and request-local identity."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextvars import ContextVar

from fastapi import HTTPException, Request, status

from app.core.config import settings

from .models import Account
from .service import auth_service


_current_account: ContextVar[Account | None] = ContextVar("studio_current_account", default=None)


def request_session_token(request: Request) -> str:
    authorization = request.headers.get("Authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.cookies.get(settings.auth_cookie_name, "").strip()


async def require_account(request: Request) -> AsyncIterator[Account]:
    account = auth_service.account_for_token(request_session_token(request))
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请先登录。",
            headers={"WWW-Authenticate": "Bearer"},
        )
    context_token = _current_account.set(account)
    try:
        yield account
    finally:
        _current_account.reset(context_token)


def current_account() -> Account:
    account = _current_account.get()
    if account is None:
        raise RuntimeError("No authenticated account is active for this request.")
    return account
