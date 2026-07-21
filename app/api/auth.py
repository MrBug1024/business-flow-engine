"""Email registration and login endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.auth.dependencies import request_session_token, require_account
from app.auth.mailer import MailConfigurationError
from app.auth.models import (
    Account,
    AuthResponse,
    LoginRequest,
    RegisterRequest,
    RegistrationCodeRequest,
)
from app.auth.service import (
    AuthenticationError,
    InvalidEmailError,
    InvalidPasswordError,
    auth_service,
)
from app.auth.store import AccountExistsError, VerificationCodeError, VerificationRateLimitError
from app.core.config import settings
from app.studio.storage import store


router = APIRouter(prefix="/auth", tags=["authentication"])


@router.post("/register/code", status_code=status.HTTP_202_ACCEPTED)
def send_registration_code(req: RegistrationCodeRequest) -> dict[str, int | str]:
    try:
        retry_after = auth_service.request_registration_code(req.email)
    except InvalidEmailError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AccountExistsError as exc:
        raise HTTPException(status_code=409, detail="该邮箱已经注册，请直接登录。") from exc
    except VerificationRateLimitError as exc:
        raise HTTPException(
            status_code=429,
            detail=f"验证码发送过于频繁，请在 {exc.retry_after} 秒后重试。",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except (MailConfigurationError, OSError) as exc:
        raise HTTPException(status_code=503, detail="验证码邮件发送失败，请稍后重试。") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="邮箱服务暂时不可用，请稍后重试。") from exc
    return {"message": "验证码已发送。", "retry_after": retry_after}


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def register(req: RegisterRequest, response: Response) -> AuthResponse:
    try:
        account, is_first_account = auth_service.register(req.email, req.password, req.code)
    except (InvalidEmailError, InvalidPasswordError, AuthenticationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AccountExistsError as exc:
        raise HTTPException(status_code=409, detail="该邮箱已经注册，请直接登录。") from exc
    except VerificationCodeError as exc:
        detail = "验证码已过期，请重新获取。" if str(exc) == "expired" else "验证码不正确或尝试次数过多。"
        raise HTTPException(status_code=422, detail=detail) from exc
    if is_first_account:
        store.claim_unowned(account.id)
    token, expires_at = auth_service.create_session(account)
    _set_session_cookie(response, token)
    return AuthResponse(account=account, expires_at=expires_at)


@router.post("/login", response_model=AuthResponse)
def login(req: LoginRequest, response: Response) -> AuthResponse:
    try:
        account = auth_service.login(req.email, req.password)
    except (InvalidEmailError, AuthenticationError) as exc:
        raise HTTPException(status_code=401, detail="邮箱或密码不正确。") from exc
    token, expires_at = auth_service.create_session(account)
    _set_session_cookie(response, token)
    return AuthResponse(account=account, expires_at=expires_at)


@router.get("/me", response_model=Account)
def me(account: Account = Depends(require_account)) -> Account:
    return account


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response) -> Response:
    auth_service.revoke_token(request_session_token(request))
    response.status_code = status.HTTP_204_NO_CONTENT
    response.delete_cookie(
        settings.auth_cookie_name,
        path="/",
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        settings.auth_cookie_name,
        token,
        max_age=max(3600, settings.jwt_expire_hours * 3600),
        path="/",
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite="lax",
    )
