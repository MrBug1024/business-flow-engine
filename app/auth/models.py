"""鉴权相关的 Pydantic 模型。"""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class PublicUser(BaseModel):
    """对外返回的用户信息（不含口令散列）。"""
    id: str
    email: str
    name: str = ""
    avatar: str = ""
    provider: str = "password"


def to_public_user(row: dict) -> PublicUser:
    return PublicUser(
        id=row["id"], email=row["email"], name=row.get("name", ""),
        avatar=row.get("avatar", ""), provider=row.get("provider", "password"),
    )


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=6, max_length=128)
    name: str = Field("", max_length=60)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)


class TokenResponse(BaseModel):
    token: str
    user: PublicUser
