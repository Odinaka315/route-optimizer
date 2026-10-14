from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

from ..models import User


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=100)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int  # seconds


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None
    email_verified: bool
    created_at: datetime

    @classmethod
    def from_user(cls, user: User) -> "UserOut":
        return cls(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            email_verified=user.email_verified_at is not None,
            created_at=user.created_at,
        )


class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=1)


class EmailRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=128)


class MessageResponse(BaseModel):
    message: str
