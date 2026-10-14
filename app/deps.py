"""FastAPI dependencies shared by the routers."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .emailer import EmailSender
from .models import User
from .security import TokenError, decode_token

# tokenUrl powers the "Authorize" button in the interactive /docs page.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_email_sender(request: Request) -> EmailSender:
    return request.app.state.email_sender


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> User:
    try:
        payload = decode_token(settings.secret_key, token, purpose="access")
        user_id = uuid.UUID(payload["sub"])
    except (TokenError, ValueError, KeyError):
        raise _UNAUTHORIZED from None
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise _UNAUTHORIZED
    return user
