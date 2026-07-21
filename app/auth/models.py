"""Public authentication models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Account(BaseModel):
    id: str
    email: str
    created_at: float
    verified_at: float


class RegistrationCodeRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)
    code: str = Field(min_length=6, max_length=6)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class AuthResponse(BaseModel):
    account: Account
    expires_at: float
