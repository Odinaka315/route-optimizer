"""Email + password accounts: signup, email verification, login, password reset."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..deps import get_current_user, get_email_sender, get_session, get_settings_dep
from ..emailer import EmailMessage, EmailSender, password_reset_email, verification_email
from ..models import User
from ..schemas.auth import (
    EmailRequest,
    LoginRequest,
    MessageResponse,
    ResetPasswordRequest,
    SignupRequest,
    TokenResponse,
    UserOut,
    VerifyEmailRequest,
)
from ..security import (
    DUMMY_HASH,
    TokenError,
    create_token,
    decode_token,
    hash_password,
    password_fingerprint,
    verify_password,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

_BAD_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Incorrect email or password",
    headers={"WWW-Authenticate": "Bearer"},
)


def _normalize_email(email: str) -> str:
    return email.strip().lower()


async def _deliver(sender: EmailSender, message: EmailMessage) -> None:
    """Send in a worker thread; a mail failure is logged, never raised to the user."""
    try:
        await run_in_threadpool(sender.send, message)
    except Exception:  # noqa: BLE001 - any provider error must not break the request
        logger.exception("Failed to send email to %s", message.to)


def _verification_message(user: User, settings: Settings) -> EmailMessage:
    token = create_token(
        settings.secret_key,
        subject=str(user.id),
        purpose="verify_email",
        expires_in=timedelta(hours=settings.verify_token_expire_hours),
        extra={"email": user.email},
    )
    link = f"{settings.app_base_url.rstrip('/')}/verify-email?token={token}"
    return verification_email(user.email, link, settings.verify_token_expire_hours)


def _reset_message(user: User, settings: Settings) -> EmailMessage:
    token = create_token(
        settings.secret_key,
        subject=str(user.id),
        purpose="reset_password",
        expires_in=timedelta(minutes=settings.reset_token_expire_minutes),
        extra={"fp": password_fingerprint(user.password_hash)},
    )
    link = f"{settings.app_base_url.rstrip('/')}/reset-password?token={token}"
    return password_reset_email(user.email, link, settings.reset_token_expire_minutes)


def _access_token(user: User, settings: Settings) -> TokenResponse:
    expires = timedelta(minutes=settings.access_token_expire_minutes)
    token = create_token(
        settings.secret_key, subject=str(user.id), purpose="access", expires_in=expires
    )
    return TokenResponse(access_token=token, expires_in=int(expires.total_seconds()))


async def _authenticate(
    session: AsyncSession, settings: Settings, email: str, password: str
) -> User:
    user = await session.scalar(select(User).where(User.email == _normalize_email(email)))
    # Always run one hash verification so timing doesn't reveal whether the email exists.
    password_ok = await run_in_threadpool(
        verify_password, user.password_hash if user else DUMMY_HASH, password
    )
    if user is None or not password_ok or not user.is_active:
        raise _BAD_CREDENTIALS
    if settings.require_email_verification and user.email_verified_at is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email not verified. Check your inbox for the confirmation link.",
        )
    return user


@router.post("/signup", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def signup(
    body: SignupRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    sender: EmailSender = Depends(get_email_sender),
) -> UserOut:
    email = _normalize_email(body.email)
    if await session.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists")

    password_hash = await run_in_threadpool(hash_password, body.password)
    user = User(email=email, password_hash=password_hash, display_name=body.display_name)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:  # two signups for the same email raced each other
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "An account with this email already exists"
        ) from None

    if settings.require_email_verification:
        background_tasks.add_task(_deliver, sender, _verification_message(user, settings))
    return UserOut.from_user(user)


@router.post("/verify-email", response_model=MessageResponse)
async def verify_email(
    body: VerifyEmailRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> MessageResponse:
    invalid = HTTPException(status.HTTP_400_BAD_REQUEST, "This verification link is invalid or has expired")
    try:
        payload = decode_token(settings.secret_key, body.token, purpose="verify_email")
        user = await session.get(User, uuid.UUID(payload["sub"]))
    except (TokenError, ValueError):
        raise invalid from None
    if user is None or payload.get("email") != user.email:
        raise invalid
    if user.email_verified_at is None:
        user.email_verified_at = datetime.now(timezone.utc)
        await session.commit()
    return MessageResponse(message="Email verified. You can now log in.")


@router.post("/resend-verification", response_model=MessageResponse, status_code=status.HTTP_202_ACCEPTED)
async def resend_verification(
    body: EmailRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    sender: EmailSender = Depends(get_email_sender),
) -> MessageResponse:
    user = await session.scalar(select(User).where(User.email == _normalize_email(body.email)))
    if user and user.is_active and user.email_verified_at is None:
        background_tasks.add_task(_deliver, sender, _verification_message(user, settings))
    # Same answer whether or not the account exists, so this can't be used to probe emails.
    return MessageResponse(message="If that account needs verification, a new link is on its way.")


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> TokenResponse:
    user = await _authenticate(session, settings, body.email, body.password)
    return _access_token(user, settings)


@router.post("/token", response_model=TokenResponse, include_in_schema=True)
async def token_form(
    form: OAuth2PasswordRequestForm = Depends(),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> TokenResponse:
    """Same as /auth/login but takes form fields (``username`` = email). Exists so the
    Authorize button in the interactive docs page works."""
    user = await _authenticate(session, settings, form.username, form.password)
    return _access_token(user, settings)


@router.post("/forgot-password", response_model=MessageResponse, status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(
    body: EmailRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    sender: EmailSender = Depends(get_email_sender),
) -> MessageResponse:
    user = await session.scalar(select(User).where(User.email == _normalize_email(body.email)))
    if user and user.is_active:
        background_tasks.add_task(_deliver, sender, _reset_message(user, settings))
    return MessageResponse(message="If that email has an account, a reset link is on its way.")


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(
    body: ResetPasswordRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> MessageResponse:
    invalid = HTTPException(status.HTTP_400_BAD_REQUEST, "This reset link is invalid or has expired")
    try:
        payload = decode_token(settings.secret_key, body.token, purpose="reset_password")
        user = await session.get(User, uuid.UUID(payload["sub"]))
    except (TokenError, ValueError):
        raise invalid from None
    # The fingerprint changes once the password does, so each link works only once.
    if user is None or not user.is_active or payload.get("fp") != password_fingerprint(user.password_hash):
        raise invalid

    user.password_hash = await run_in_threadpool(hash_password, body.new_password)
    if user.email_verified_at is None:  # following an emailed link proves ownership
        user.email_verified_at = datetime.now(timezone.utc)
    await session.commit()
    return MessageResponse(message="Password updated. You can now log in.")


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.from_user(user)
